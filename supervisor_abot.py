#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ABOT Supervisor v2 - Estrategia Adaptativa Automática
=====================================================

Lee el mercado (velas reales de Binance), lee los saldos reales de Binance y
OKX con las credenciales guardadas en creds_abot.json y ajusta EN CALIENTE los
controllers PMM de Hummingbot.

IMPORTANTE - qué puede cambiar en caliente y qué no:
  * Hummingbot V2 relee los YAML cada 10 s y aplica SOLO los campos marcados
    como "is_updatable". En los PMM eso incluye: total_amount_quote,
    buy_spreads, sell_spreads, buy_amounts_pct, sell_amounts_pct,
    executor_refresh_time y cooldown_time.
  * Los "levels" de las escaleras (ladder_b) NO son actualizables en caliente:
    si este script los reescribiera, el bot seguiría usando los de memoria y
    solo cambiarían para el PRÓXIMO arranque. Por eso el supervisor NO toca
    las escaleras; solo registra una recomendación en el log.

Estrategia adaptativa:
  * Detecta régimen: SIDEWAYS / BULLISH / BEARISH / CRASH con velas de 5m.
  * Ajusta spreads según régimen y volatilidad.
  * Ajusta tamaño según el USDT LIBRE real de cada exchange (nunca lo excede).
  * Si el inventario en PEPE supera MAX_INVENTORY_PCT del portafolio, PAUSA las
    compras (buy_spreads: []) y deja solo ventas. Así deja de comprar en caída.
  * Si el USDT libre no alcanza el mínimo operable, PAUSA las compras.

Uso:
    python supervisor_abot.py            # bucle continuo
    python supervisor_abot.py --once     # una sola pasada (diagnóstico)
    python supervisor_abot.py --dry-run  # no escribe YAML ni envía Telegram

Requisitos: pip install requests pyyaml
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests
import yaml

try:  # consola Windows: evitar UnicodeEncodeError con acentos
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ============================================================================
# RUTAS Y PARÁMETROS
# ============================================================================

BASE_DIR = Path(r"C:\Users\rober\.config\opencode\hummingbot")
CONF_DIR = BASE_DIR / "conf" / "controllers"
CREDENTIALS_FILE = BASE_DIR / "creds_abot.json"
STATE_FILE = BASE_DIR / "data" / "supervisor_abot_state.json"
LOG_FILE = BASE_DIR / "logs" / "supervisor_abot.log"
LOCK_FILE = BASE_DIR / "data" / "supervisor_abot.lock"

_lock_handle = None


def acquire_single_instance_lock() -> bool:
    """Impide dos supervisores escribiendo los mismos YAML a la vez.

    El lock se libera solo cuando el proceso muere (aunque sea por crash),
    así que no deja bloqueos fantasma.
    """
    global _lock_handle
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_handle = handle
    return True

CHECK_INTERVAL_SECONDS = 180          # cada 3 min (el PMM refresca cada 120 s)
HTTP_TIMEOUT = 15

# --- Retry con backoff para todas las llamadas HTTP ---
_original_requests_get = requests.get
_original_requests_post = requests.post
_original_requests_delete = requests.delete


def _http_with_retry(func):
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(3):
            try:
                r = func(*args, **kwargs)
                if r.status_code < 500:
                    return r
            except requests.exceptions.RequestException as e:
                last_exc = e
            if attempt < 2:
                time.sleep(2 ** attempt)
        if last_exc:
            raise last_exc
        return func(*args, **kwargs)
    return wrapper

requests.get = _http_with_retry(_original_requests_get)
requests.post = _http_with_retry(_original_requests_post)
requests.delete = _http_with_retry(_original_requests_delete)

PAIR = "PEPE-USDT"
BINANCE_SYMBOL = "PEPEUSDT"
OKX_INST = "PEPE-USDT"

# --- Gestión de capital ------------------------------------------------------
FREE_USE_PCT = 0.80        # usamos como máximo el 80% del USDT libre
MAX_QUOTE = 12.0           # tope por controller (USDT) - subido de 8.0
SELL_QUOTE_TARGET = 12.0   # notional de venta cuando las compras estan pausadas - subido a 12
AGGRESSIVE_SELL_QUOTE = 12.0  # notional de venta cuando inventario crítico (>90% PEPE) - subido de 8.0
MIN_BUY_BINANCE = 1.20     # minNotional Binance = 1.00 USDT (+ margen)
MIN_BUY_OKX = 0.60         # minSz OKX = 100.000 PEPE (~0.46 USDT)
MAX_INVENTORY_PCT = 0.85   # si PEPE > 85% del portafolio -> pausar compras
CRITICAL_INVENTORY_PCT = 0.90  # si PEPE > 90% -> vender agresivamente
LOW_INVENTORY_PCT = 0.70   # si PEPE < 70% -> acumular (más compra)

# --- Detección de régimen ----------------------------------------------------
CANDLE_INTERVAL = "5m"
CANDLE_LIMIT = 30           # 30 velas de 5m = 2h 30m
EMA_FAST = 7
EMA_SLOW = 21
TREND_THRESHOLD_PCT = 0.008     # 0.8% en 2h para llamarlo tendencia
CRASH_THRESHOLD_PCT = -0.040    # -4% en 1h = caída libre
VOL_BASE_5M = 0.005             # volatilidad "normal" por vela de 5m
VOL_MULT_MAX = 2.5

# --- Telegram (se lee del controller notificador) ----------------------------
TELEGRAM_CONF = CONF_DIR / "conf_telegram_notifier.yml"
ALERT_DEDUP_SECONDS = 3600

# ============================================================================
# LOG
# ============================================================================


def log(msg: str):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} | {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE.with_suffix(".log.1"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # el log nunca debe tumbar al supervisor


# ============================================================================
# ESTADO PERSISTENTE (para no repetir alertas ni cambios tras reiniciar)
# ============================================================================


def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: Dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"[WARN] no se pudo guardar estado: {e}")


# ============================================================================
# TELEGRAM
# ============================================================================


def load_telegram() -> Optional[Tuple[str, str]]:
    try:
        with open(TELEGRAM_CONF, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        token, chat = cfg.get("bot_token"), cfg.get("chat_id")
        if token and chat:
            return str(token), str(chat)
    except Exception:
        pass
    return None


def telegram_alert(state: Dict, key: str, text: str, dry_run: bool = False):
    now = time.time()
    last = state.get("alerts", {}).get(key, 0)
    if now - last < ALERT_DEDUP_SECONDS:
        return
    log(f"[ALERTA] {text}")
    if dry_run:
        return  # en dry-run no se registra ni se envía
    state.setdefault("alerts", {})[key] = now
    tg = load_telegram()
    if not tg:
        return
    token, chat = tg
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": f"🤖 ABOT\n{text}", "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        log(f"[WARN] Telegram falló: {e}")


# ============================================================================
# CREDENCIALES Y EXCHANGES
# ============================================================================


def load_credentials() -> Optional[Dict]:
    if not CREDENTIALS_FILE.exists():
        log("[ERROR] Falta creds_abot.json. Ejecuta el supervisor en modo interactivo una vez.")
        return None
    try:
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"[ERROR] creds_abot.json inválido: {e}")
        return None


def binance_mid() -> Optional[float]:
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/bookTicker",
            params={"symbol": BINANCE_SYMBOL}, timeout=HTTP_TIMEOUT,
        )
        d = r.json()
        return (float(d["bidPrice"]) + float(d["askPrice"])) / 2
    except Exception as e:
        log(f"[WARN] precio Binance: {e}")
        return None


def okx_mid() -> Optional[float]:
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": OKX_INST}, timeout=HTTP_TIMEOUT,
        )
        d = r.json()["data"][0]
        return (float(d["bidPx"]) + float(d["askPx"])) / 2
    except Exception as e:
        log(f"[WARN] precio OKX: {e}")
        return None


def binance_balances(creds: Dict) -> Optional[Dict]:
    try:
        key = creds["binance"]["api_key"]
        secret = creds["binance"]["secret_key"]
        ts = int(time.time() * 1000)
        query = f"timestamp={ts}&recvWindow=5000"
        sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        r = requests.get(
            "https://api.binance.com/api/v3/account",
            params={"timestamp": ts, "recvWindow": 5000, "signature": sig},
            headers={"X-MBX-APIKEY": key}, timeout=HTTP_TIMEOUT,
        )
        data = r.json()
        if "balances" not in data:
            log(f"[WARN] Binance account: {data.get('msg', data)}")
            return None
        out = {"USDT": [0.0, 0.0], "PEPE": [0.0, 0.0]}  # [free, locked]
        for b in data["balances"]:
            if b["asset"] in out:
                out[b["asset"]] = [float(b["free"]), float(b["locked"])]
        return {
            "usdt_free": out["USDT"][0], "usdt_locked": out["USDT"][1],
            "pepe_free": out["PEPE"][0], "pepe_locked": out["PEPE"][1],
        }
    except Exception as e:
        log(f"[WARN] balance Binance: {e}")
        return None


def _okx_headers(creds: Dict, method: str, path: str) -> Dict[str, str]:
    key = creds["okx"]["api_key"]
    secret = creds["okx"]["secret_key"]
    passphrase = creds["okx"]["passphrase"]
    # OKX exige ISO-8601 con milisegundos (no epoch ms): mismo formato que
    # usa el conector oficial de Hummingbot en okx_auth.py.
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
    ts = ts.replace("+00:00", "Z")
    sign = base64.b64encode(
        hmac.new(secret.encode(), (ts + method + path).encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }


def okx_balances(creds: Dict) -> Optional[Dict]:
    try:
        path = "/api/v5/account/balance"
        r = requests.get(
            "https://www.okx.com" + path,
            headers=_okx_headers(creds, "GET", path), timeout=HTTP_TIMEOUT,
        )
        data = r.json()
        if data.get("code") != "0":
            log(f"[WARN] OKX balance: {data.get('msg')} ({data.get('code')})")
            return None
        out = {"usdt_free": 0.0, "usdt_locked": 0.0, "pepe_free": 0.0, "pepe_locked": 0.0}
        for acc in data.get("data", []):
            for det in acc.get("details", []):
                ccy = det.get("ccy")
                if ccy == "USDT":
                    out["usdt_free"] = float(det.get("availBal") or 0)
                    out["usdt_locked"] = float(det.get("frozenBal") or 0)
                elif ccy == "PEPE":
                    out["pepe_free"] = float(det.get("availBal") or 0)
                    out["pepe_locked"] = float(det.get("frozenBal") or 0)
        return out
    except Exception as e:
        log(f"[WARN] balance OKX: {e}")
        return None


# ============================================================================
# MERCADO: VELAS Y RÉGIMEN
# ============================================================================


def binance_closes() -> Optional[list]:
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": BINANCE_SYMBOL, "interval": CANDLE_INTERVAL, "limit": CANDLE_LIMIT},
            timeout=HTTP_TIMEOUT,
        )
        rows = r.json()
        return [float(row[4]) for row in rows]
    except Exception as e:
        log(f"[WARN] velas Binance: {e}")
        return None


def ema(values: list, period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def pct_change(values: list, bars: int) -> float:
    if len(values) <= bars or values[-bars - 1] == 0:
        return 0.0
    return (values[-1] - values[-bars - 1]) / values[-bars - 1]


def stdev_returns(values: list) -> float:
    if len(values) < 3:
        return 0.0
    rets = [(values[i] - values[i - 1]) / values[i - 1] for i in range(1, len(values))]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    return var ** 0.5


def detect_regime(closes: list) -> Dict:
    """Devuelve régimen, cambio 1h/2h y volatilidad por vela."""
    if not closes or len(closes) < 10:
        return {"regime": "SIDEWAYS", "chg1h": 0.0, "chg2h": 0.0, "vol": 0.0,
                "ema_fast": 0.0, "ema_slow": 0.0}

    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)
    chg1h = pct_change(closes, 12)   # 12 velas de 5m = 1h
    chg2h = pct_change(closes, 24)   # 24 velas de 5m = 2h
    vol = stdev_returns(closes)

    if chg1h <= CRASH_THRESHOLD_PCT or (vol > 0.02 and closes[-1] < slow * 0.97):
        regime = "CRASH"
    elif fast > slow * (1 + TREND_THRESHOLD_PCT) and chg2h > TREND_THRESHOLD_PCT:
        regime = "BULLISH"
    elif fast < slow * (1 - TREND_THRESHOLD_PCT) and chg2h < -TREND_THRESHOLD_PCT:
        regime = "BEARISH"
    else:
        regime = "SIDEWAYS"

    return {"regime": regime, "chg1h": chg1h, "chg2h": chg2h, "vol": vol,
            "ema_fast": fast, "ema_slow": slow}


def regime_spreads(regime: str, vol: float) -> Tuple[float, float]:
    """Spreads base (buy, sell) según régimen, escalados por volatilidad."""
    base = {
        "SIDEWAYS": (0.005, 0.005),
        "BULLISH": (0.003, 0.012),   # comprar rápido, vender caro
        "BEARISH": (0.010, 0.004),   # comprar lejos, salir rápido
        "CRASH": (0.020, 0.006),
    }[regime]
    mult = max(1.0, min(VOL_MULT_MAX, vol / VOL_BASE_5M if VOL_BASE_5M else 1.0))
    return round(base[0] * mult, 4), round(base[1] * mult, 4)


# ============================================================================
# ESCRITURA ATÓMICA DE YAML
# ============================================================================


def read_yaml(path: Path) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        log(f"[ERROR] leyendo {path.name}: {e}")
        return None


def write_yaml_atomic(path: Path, cfg: Dict, expected_id: str) -> bool:
    """
    Escribe el YAML de forma atómica (tmp + os.replace).
    Hummingbot relee estos archivos cada 10 s; una escritura parcial podría
    hacerle leer un YAML roto y lanzar una excepción en pleno tick.
    """
    if cfg.get("id") != expected_id:
        log(f"[ERROR] {path.name}: id inesperado, no se escribe")
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
        # validar antes de reemplazar
        with open(tmp, "r", encoding="utf-8") as f:
            check = yaml.safe_load(f)
        if check.get("id") != expected_id:
            raise ValueError("YAML resultante inválido")
        os.replace(tmp, path)
        return True
    except Exception as e:
        log(f"[ERROR] escribiendo {path.name}: {e}")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


def apply_pmm_settings(path: Path, settings: Dict, dry_run: bool) -> bool:
    cfg = read_yaml(path)
    if cfg is None:
        return False
    expected_id = str(cfg.get("id", ""))
    changed = {k: v for k, v in settings.items() if cfg.get(k) != v}
    if not changed:
        return False
    if dry_run:
        log(f"[DRY-RUN] {path.name}: {changed}")
        return False
    cfg.update(changed)
    ok = write_yaml_atomic(path, cfg, expected_id)
    if ok:
        log(f"[OK] {path.name}: {changed}")
    return ok


# ============================================================================
# LÓGICA PRINCIPAL
# ============================================================================


def compute_settings(exchange: str, bal: Dict, regime: str, vol: float,
                     price: float, global_inv_pct: float,
                     exchange_inv_pct: float = None) -> Tuple[Dict, str]:
    """
    Calcula los campos a escribir en el YAML del PMM de un exchange.
    Devuelve (settings, motivo_pausa|"").

    global_inv_pct = % del portafolio TOTAL que esta en PEPE (no por exchange).
    vol = volatilidad 5m para escalar notional por volatilidad.
    """
    if exchange_inv_pct is None:
        exchange_inv_pct = global_inv_pct

    buy_spread, sell_spread = regime_spreads(regime, vol)

    usdt_free = bal["usdt_free"]
    min_buy = MIN_BUY_BINANCE if exchange == "binance" else MIN_BUY_OKX
    # El PMM necesita al menos 3.0 USDT libres para abrir un executor buy.
    # El minimo del exchange (1.20/0.60) no es suficiente para el PMM.
    # El PMM necesita al menos total_amount_quote de USDT libre para buys.
    # 3.0 no es suficiente: el PMM intenta abrir executors de 5+ USDT.
    min_buy = max(min_buy, 15.0)  # PMM necesita >=15 USDT libres para evitar Not enough budget

    # Escalar notional por volatilidad: en alta vol, vender/comprar mas.
    vol_mult = max(1.0, min(1.5, vol / VOL_BASE_5M if VOL_BASE_5M else 1.0))

    pause_reason = ""
    rebalance_mode = "normal"
    if regime == "CRASH":
        pause_reason = "CRASH: no comprar en caida libre"
    elif exchange_inv_pct > CRITICAL_INVENTORY_PCT:
        pause_reason = "inventario PEPE CRITICO %.1f%% > %.0f%%" % (global_inv_pct*100, CRITICAL_INVENTORY_PCT*100)
        rebalance_mode = "aggressive_sell"
    elif exchange_inv_pct > MAX_INVENTORY_PCT:
        pause_reason = "inventario PEPE alto %.1f%% > %.0f%%" % (global_inv_pct*100, MAX_INVENTORY_PCT*100)
        rebalance_mode = "sell_only"
    elif usdt_free < min_buy:
        pause_reason = "USDT libre %.2f < minimo %.2f" % (usdt_free, min_buy)

    if pause_reason:
        if rebalance_mode == "aggressive_sell":
            sell_target = min(12.0, AGGRESSIVE_SELL_QUOTE * vol_mult)
            sell_sp = max(0.002, round(sell_spread * 0.5, 4))
        else:
            sell_target = min(12.0, SELL_QUOTE_TARGET * vol_mult)
            sell_sp = sell_spread
        settings = {
            "total_amount_quote": round(sell_target, 2),
            "buy_spreads": [],
            "buy_amounts_pct": [],
            "sell_spreads": [sell_sp],
            "sell_amounts_pct": [1.0],
            "skip_rebalance": True,
        }
        return settings, pause_reason

    # Con compras: ajustar split segun inventario.
    if exchange_inv_pct <= LOW_INVENTORY_PCT:
        buy_pct = 0.7
        sell_pct = 0.3
        total = min(MAX_QUOTE, max(2.0, (usdt_free * FREE_USE_PCT) / buy_pct))
    else:
        buy_pct = 0.5
        sell_pct = 0.5
        total = min(MAX_QUOTE, max(2.0, (usdt_free * FREE_USE_PCT) / 0.5))
    total = round(total * vol_mult, 2)
    total = max(2.0, min(MAX_QUOTE, total))
    settings = {
        "total_amount_quote": total,
        "buy_spreads": [buy_spread],
        "buy_amounts_pct": [buy_pct],
        "sell_spreads": [sell_spread],
        "sell_amounts_pct": [sell_pct],
        "skip_rebalance": False,
    }
    return settings, ""



def ladder_recommendation(price: float, regime: str) -> str:
    """
    Las escaleras NO se pueden cambiar en caliente. Solo dejamos la
    recomendación en el log para aplicarla en el próximo arranque seguro.
    """
    if regime == "CRASH":
        return "ESCALERAS: considera desactivar compras (mercado en caída libre)."
    if regime == "BEARISH":
        return (f"ESCALERAS (próximo arranque): compras más abajo y más pequeñas "
                f"(p.ej. {price*0.95:.8f} / {price*0.90:.8f} / {price*0.85:.8f}).")
    if regime == "BULLISH":
        return (f"ESCALERAS (próximo arranque): ventas más cerca "
                f"(p.ej. {price*1.03:.8f} / {price*1.06:.8f} / {price*1.10:.8f}).")
    return "ESCALERAS: sin cambios."


def rebalance_inventory(creds: Dict, global_inv_pct: float,
                      price: float, state: Dict, dry_run: bool) -> bool:
    """
    Rebalanceo real: si el inventario en PEPE supera el 95%, vende un chunk
    de PEPE por USDT a mercado en el exchange con mas PEPE libre.
    """
    if global_inv_pct < 0.95:
        return False
    if dry_run:
        log("[DRY-RUN] rebalance: venderia PEPE (inventario %.1f%%)" % (global_inv_pct*100))
        return False

    last = state.get("last_rebalance", 0)
    if time.time() - last < 1800:
        return False

    bal_b = binance_balances(creds)
    bal_o = okx_balances(creds)
    if not bal_b or not bal_o:
        return False

    pepe_b = bal_b["pepe_free"]
    pepe_o = bal_o["pepe_free"]
    if pepe_b >= pepe_o and pepe_b * price >= 5.0:
        exchange = "binance"
        amount_pepe = min(pepe_b * 0.02, 5.0 / price)
        amount_pepe = max(100000, round(amount_pepe))
    elif pepe_o * price >= 5.0:
        exchange = "okx"
        amount_pepe = min(pepe_o * 0.02, 5.0 / price)
        amount_pepe = max(100000, round(amount_pepe))
    else:
        return False

    if exchange == "binance":
        amount_pepe = round(amount_pepe)
    else:
        amount_pepe = (amount_pepe // 100000) * 100000
    if amount_pepe < 100000:
        return False

    try:
        if exchange == "binance":
            key = creds["binance"]["api_key"]
            secret = creds["binance"]["secret_key"]
            ts = int(time.time() * 1000)
            params = {
                "symbol": BINANCE_SYMBOL,
                "side": "SELL",
                "type": "MARKET",
                "quantity": str(amount_pepe),
                "timestamp": ts,
                "recvWindow": 5000,
            }
            query = "&".join("%s=%s" % (k, v) for k, v in sorted(params.items()))
            sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            params["signature"] = sig
            r = requests.post(
                "https://api.binance.com/api/v3/order",
                params=params,
                headers={"X-MBX-APIKEY": key},
                timeout=HTTP_TIMEOUT,
            )
            data = r.json()
            if "orderId" in data:
                log("[REBALANCE] vendido %d PEPE a mercado en Binance (~%.2f USDT)" % (amount_pepe, amount_pepe*price))
                telegram_alert(state, "rebalance_sell",
                               "Rebalanceo: vendido %d PEPE (~%.2f USDT) a mercado en Binance. Inventario era %.1f%%." % (amount_pepe, amount_pepe*price, global_inv_pct*100), dry_run)
                state["last_rebalance"] = time.time()
                return True
            else:
                log("[WARN] rebalance Binance fallo: %s" % data.get('msg', data))
                return False
        else:
            path = "/api/v5/trade/order"
            body = {
                "instId": OKX_INST,
                "tdMode": "cash",
                "side": "sell",
                "ordType": "market",
                "sz": str(amount_pepe),
            }
            body_str = json.dumps(body, separators=(",", ":"))
            r = requests.post(
                "https://www.okx.com" + path,
                data=body_str,
                headers=_okx_headers(creds, "POST", path, body_str),
                timeout=HTTP_TIMEOUT,
            )
            data = r.json()
            if data.get("code") == "0":
                log("[REBALANCE] vendido %d PEPE a mercado en OKX (~%.2f USDT)" % (amount_pepe, amount_pepe*price))
                telegram_alert(state, "rebalance_sell",
                               "Rebalanceo: vendido %d PEPE (~%.2f USDT) a mercado en OKX. Inventario era %.1f%%." % (amount_pepe, amount_pepe*price, global_inv_pct*100), dry_run)
                state["last_rebalance"] = time.time()
                return True
            else:
                log("[WARN] rebalance OKX fallo: %s (%s)" % (data.get('msg'), data.get('code')))
                return False
    except Exception as e:
        log("[WARN] rebalance error: %s" % e)
        return False

def run_once(creds: Dict, state: Dict, dry_run: bool) -> bool:
    price_b = binance_mid()
    price_o = okx_mid()
    closes = binance_closes()

    if not closes:
        log("[WARN] sin velas; no se toca nada este ciclo")
        return False

    market = detect_regime(closes)
    regime = market["regime"]
    vol = market["vol"]

    prices = [p for p in (price_b, price_o) if p]
    if not prices:
        log("[WARN] sin precio; no se toca nada este ciclo")
        return False
    price = sum(prices) / len(prices)

    log("=" * 78)
    log(f"PEPE {price:.8f} (B {price_b} / OKX {price_o}) | régimen={regime} "
        f"| 1h={market['chg1h']:+.2%} 2h={market['chg2h']:+.2%} vol5m={vol:.2%}")
    log(f"EMA{EMA_FAST}={market['ema_fast']:.8f} EMA{EMA_SLOW}={market['ema_slow']:.8f} "
        f"| spreads -> {regime_spreads(regime, vol)}")

    bal_b = binance_balances(creds)
    bal_o = okx_balances(creds)

    if bal_b:
        log(f"BINANCE  USDT libre {bal_b['usdt_free']:.2f} / bloqueado {bal_b['usdt_locked']:.2f} "
            f"| PEPE {bal_b['pepe_free']:,.0f} + {bal_b['pepe_locked']:,.0f} bloqueado")
    if bal_o:
        log(f"OKX      USDT libre {bal_o['usdt_free']:.2f} / bloqueado {bal_o['usdt_locked']:.2f} "
            f"| PEPE {bal_o['pepe_free']:,.0f} + {bal_o['pepe_locked']:,.0f} bloqueado")

    portfolio = 0.0
    pepe_value_total = 0.0
    for bal in (bal_b, bal_o):
        if bal:
            portfolio += bal["usdt_free"] + bal["usdt_locked"] + \
                (bal["pepe_free"] + bal["pepe_locked"]) * price
            pepe_value_total += (bal["pepe_free"] + bal["pepe_locked"]) * price
    global_inv_pct = (pepe_value_total / portfolio) if portfolio > 0 else 1.0
    if portfolio > 0:
        log(f"PORTAFOLIO ~{portfolio:.2f} USDT | PEPE {global_inv_pct:.1%} | "
            f"USDT {1 - global_inv_pct:.1%}")

    did_change = False
    pause_notes = []

    for exchange, bal, filename in (
        ("binance", bal_b, "conf_pmm_binance_v6.yml"),
        ("okx", bal_o, "conf_pmm_okx_v6.yml"),
    ):
        if not bal:
            log(f"[WARN] {exchange}: sin balance, no se modifica {filename}")
            continue
        ex_inv = (bal["pepe_free"] + bal["pepe_locked"]) * price / (bal["usdt_free"] + bal["usdt_locked"] + (bal["pepe_free"] + bal["pepe_locked"]) * price) if (bal["usdt_free"] + bal["usdt_locked"] + (bal["pepe_free"] + bal["pepe_locked"]) * price) > 0 else 1.0
        settings, pause = compute_settings(exchange, bal, regime, vol, price, global_inv_pct, ex_inv)
        if pause:
            pause_notes.append(f"{exchange}: {pause}")
        if apply_pmm_settings(CONF_DIR / filename, settings, dry_run):
            did_change = True

    log(ladder_recommendation(price, regime))

    # Alertas (con dedup) ------------------------------------------------
    prev_regime = state.get("regime")
    if prev_regime != regime:
        telegram_alert(state, f"regime_{regime}",
                       f"Régimen -> {regime} (1h {market['chg1h']:+.1%}, 2h {market['chg2h']:+.1%}). "
                       f"PEPE {price:.8f}.", dry_run)
        state["regime"] = regime
    for note in pause_notes:
        telegram_alert(state, f"pause_{note}", f"Compras PAUSADAS: {note}.", dry_run)

    if global_inv_pct > CRITICAL_INVENTORY_PCT:
        telegram_alert(state, "inventory_critical",
                       "Inventario PEPE CRITICO: %.1f%%. Venta agresiva activada (spread tighter, 8 USDT). USDT libre total %.2f." % (global_inv_pct*100, sum(b['usdt_free'] for b in (bal_b, bal_o) if b)), dry_run)
    elif global_inv_pct > MAX_INVENTORY_PCT:
        telegram_alert(state, "inventory_high",
                       "Inventario PEPE %.1f%% del portafolio. USDT libre total %.2f." % (global_inv_pct*100, sum(b['usdt_free'] for b in (bal_b, bal_o) if b)), dry_run)

    # Rebalanceo real: vender PEPE a mercado si inventario critico
    if global_inv_pct > 0.95:
        rebalance_inventory(creds, global_inv_pct, price, state, dry_run)

    # Alerta de PnL diario
    try:
        import datetime as _dt
        import sqlite3 as _sq
        today = _dt.date.today().isoformat()
        _con = _sq.connect(str(BASE_DIR / "data" / "conf_v6_global.sqlite"))
        cur_pnl = _con.execute("select coalesce(sum(net_pnl_quote),0) from Executors where is_active=0").fetchone()[0]
        _con.close()
        prev = state.get("daily_pnl", {})
        if prev.get("date") != today:
            state["daily_pnl"] = {"date": today, "start_pnl": cur_pnl, "last_pnl": cur_pnl}
        else:
            last_pnl = prev.get("last_pnl", cur_pnl)
            drop = last_pnl - cur_pnl
            if drop > 2.0:
                telegram_alert(state, "daily_pnl_drop",
                               "PnL diario cayo %.2f USDT (de %+.2f a %+.2f). Revisar." % (drop, last_pnl, cur_pnl), dry_run)
            state["daily_pnl"]["last_pnl"] = cur_pnl
    except Exception:
        pass

    state["last_run"] = time.time()
    state["last_price"] = price
    state["last_settings_changed"] = did_change
    return did_change


def main():
    parser = argparse.ArgumentParser(description="ABOT Supervisor v2")
    parser.add_argument("--once", action="store_true", help="una sola pasada")
    parser.add_argument("--dry-run", action="store_true", help="no escribe YAML ni envía Telegram")
    args = parser.parse_args()

    log("=" * 78)
    log("ABOT Supervisor v2 iniciado" + (" (DRY-RUN)" if args.dry_run else ""))
    log(f"intervalo={CHECK_INTERVAL_SECONDS}s | par={PAIR} | tope/controller={MAX_QUOTE} USDT")
    log(f"inventario máx={MAX_INVENTORY_PCT:.0%} | uso USDT libre={FREE_USE_PCT:.0%}")

    if not args.dry_run and not acquire_single_instance_lock():
        log("[ABORT] Ya hay otro supervisor_abot.py activo. Cierro este para no duplicar escrituras.")
        return 2

    creds = load_credentials()
    if not creds:
        return 1
    state = load_state()

    if args.once:
        run_once(creds, state, args.dry_run)
        save_state(state)
        return 0

    while True:
        try:
            run_once(creds, state, args.dry_run)
            save_state(state)
        except KeyboardInterrupt:
            log("Detenido por el usuario.")
            save_state(state)
            return 0
        except Exception as e:
            log(f"[ERROR] ciclo fallido: {e}")
            import traceback
            log(traceback.format_exc())
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
