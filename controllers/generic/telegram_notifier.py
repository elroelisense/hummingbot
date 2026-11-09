import glob
import os
import sqlite3
import time
from typing import List

import requests
from pydantic import Field

from hummingbot.core.data_type.common import PriceType

from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction


class TelegramNotifierConfig(ControllerConfigBase):
    """
    Notificador Telegram integrado: corre DENTRO del proceso Hummingbot
    (sin terminal ni proceso extra). Cada 15s revisa las DBs de trades
    (data/conf_v6*.sqlite) y avisa por Telegram cada fill nuevo.
    Solo lectura de DB + POST a Telegram. Nunca coloca ordenes.
    """
    controller_name: str = "telegram_notifier"
    controller_type: str = "generic"
    bot_token: str = Field(default="")
    chat_id: str = Field(default="")
    check_interval_seconds: int = Field(default=15)

    def update_markets(self, markets) -> dict:
        return markets


class TelegramNotifier(ControllerBase):
    def __init__(self, config: TelegramNotifierConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._last_check = 0
        self._seen = set()
        self._started = False
        self._tg_offset = 0
        self._alert_price = None
        self._init_seen()

    def _db_paths(self):
        return sorted(glob.glob(os.path.join("data", "conf_v6*.sqlite")))

    def _init_seen(self):
        # Marca como vistos los fills ya existentes, para no spamear historial.
        for db in self._db_paths():
            try:
                con = sqlite3.connect(db)
                for (ts, oid) in con.execute("SELECT timestamp, order_id FROM TradeFill"):
                    self._seen.add((os.path.basename(db), ts, oid))
                con.close()
            except Exception:
                pass

    def _send(self, text: str):
        if not self.config.bot_token or not self.config.chat_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage",
                json={"chat_id": self.config.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=8,
            )
        except Exception as e:
            self.logger().warning(f"Telegram notifier: send failed: {e}")

    # ---------- Comandos Telegram (polling getUpdates, solo tu chat_id) ----------

    def _history_text(self, limit=8):
        rows_all = []
        for db in self._db_paths():
            try:
                con = sqlite3.connect(db)
                rows = con.execute(
                    "SELECT timestamp, trade_type, market, symbol FROM TradeFill"
                ).fetchall()
                con.close()
                rows_all.extend(rows)
            except Exception:
                pass
        if not rows_all:
            return "ℹ️ Aún no hay fills registrados."
        rows_all.sort(key=lambda r: r[0], reverse=True)
        msg = "📊 <b>ÚLTIMOS FILLS:</b>\n\n"
        for ts, side, market, symbol in rows_all[:limit]:
            fecha = time.strftime('%m-%d %H:%M', time.localtime(ts / 1000 if ts > 1e11 else ts))
            icono = "🟢" if "BUY" in str(side).upper() else "🔴"
            msg += f"{icono} <b>{side}</b> {symbol} en {market} — {fecha}\n"
        return msg

    def _balance_text(self):
        lines = ["💰 <b>BALANCES:</b>"]
        total_usd = 0.0
        for conn in ("binance", "okx"):
            try:
                pepe = float(self.market_data_provider.get_balance(conn, "PEPE") or 0)
                usdt = float(self.market_data_provider.get_balance(conn, "USDT") or 0)
                lines.append(f"\n<b>{conn.upper()}</b> PEPE {pepe:,.0f} | USDT {usdt:.2f}")
                total_usd += usdt
            except Exception as e:
                lines.append(f"\n<b>{conn.upper()}</b> (sin datos: {e})")
        # Valor PEPE total a mid Binance
        try:
            mid = float(self.market_data_provider.get_price_by_type("binance", "PEPE-USDT", PriceType.MidPrice) or 0)
            pepe_total = 0.0
            for conn in ("binance", "okx"):
                try:
                    pepe_total += float(self.market_data_provider.get_balance(conn, "PEPE") or 0)
                except Exception:
                    pass
            pepe_usd = pepe_total * mid
            total_usd += pepe_usd
            lines.append(f"\nPEPE total: {pepe_total:,.0f} ≈ ${pepe_usd:.2f} @ {mid:.8f}")
        except Exception:
            pass
        lines.append(f"\n<b>Total ≈ ${total_usd:.2f}</b> (sin BNB)")
        return "\n".join(lines)

    def _status_text(self):
        lines = ["📡 <b>STATUS:</b>"]
        for conn in ("binance", "okx"):
            try:
                mid = self.market_data_provider.get_price_by_type(conn, "PEPE-USDT", PriceType.MidPrice)
                lines.append(f"{conn}: PEPE-USDT {mid}")
            except Exception as e:
                lines.append(f"{conn}: (sin precio: {e})")
        lines.append(f"\nFills vistos: {len(self._seen)}")
        return "\n".join(lines)

    def _pnl_text(self):
        per_ctrl = {}
        for db in self._db_paths():
            try:
                con = sqlite3.connect(db)
                for cid, pnl, fees, filled in con.execute(
                    "SELECT controller_id, net_pnl_quote, cum_fees_quote, filled_amount_quote FROM Executors"
                ):
                    cid = cid or "desconocido"
                    a = per_ctrl.setdefault(cid, [0.0, 0.0, 0.0, 0])
                    a[0] += pnl or 0
                    a[1] += fees or 0
                    a[2] += filled or 0
                    a[3] += 1
                con.close()
            except Exception:
                continue
        if not per_ctrl:
            return "ℹ️ Sin executors registrados aún."
        lines = ["📈 <b>PnL POR CONTROLLER:</b>"]
        tot = 0.0
        for cid, (pnl, fees, filled, n) in sorted(per_ctrl.items()):
            tot += pnl
            lines.append(f"{cid}: PnL ${pnl:.4f} | fees ${fees:.4f} | fills ${filled:.2f} ({n} exec)")
        lines.append(f"\n<b>Total PnL: ${tot:.4f}</b>")
        return "\n".join(lines)

    def _orders_text(self):
        lines = ["🧾 <b>ÓRDENES ABIERTAS (recientes):</b>"]
        total = 0
        for db in self._db_paths():
            try:
                con = sqlite3.connect(db)
                rows = con.execute(
                    "SELECT market, symbol, last_status, creation_timestamp FROM \"Order\" "
                    "WHERE last_status LIKE '%Created' ORDER BY creation_timestamp DESC LIMIT 15"
                ).fetchall()
                con.close()
            except Exception:
                continue
            for market, symbol, status, ts in rows:
                fecha = time.strftime('%H:%M', time.localtime(ts / 1000 if ts > 1e11 else ts))
                lines.append(f"{market} {symbol} {status} — {fecha}")
                total += 1
        if total == 0:
            return "ℹ️ Sin órdenes abiertas registradas en DB (ver status en Hummingbot)."
        lines.append(f"\nTotal: {total}")
        return "\n".join(lines)

    def _check_alert(self):
        if self._alert_price is None:
            return
        try:
            mid = float(self.market_data_provider.get_price_by_type("binance", "PEPE-USDT", PriceType.MidPrice) or 0)
        except Exception:
            return
        if mid and mid >= self._alert_price:
            self._send(f"🚨 <b>ALERTA DE PRECIO:</b> PEPE-USDT tocó {mid:.8f} (alerta: {self._alert_price})")
            self._alert_price = None

    def _poll_commands(self):
        if not self.config.bot_token or not self.config.chat_id:
            return
        try:
            res = requests.get(
                f"https://api.telegram.org/bot{self.config.bot_token}/getUpdates",
                params={"offset": self._tg_offset, "timeout": 1},
                timeout=10,
            ).json()
            if not res.get("ok"):
                return
            for upd in res.get("result", []):
                self._tg_offset = upd["update_id"] + 1
                msg = (upd.get("message") or {}).get("text", "").strip().lower()
                chat = str((upd.get("message") or {}).get("chat", {}).get("id", ""))
                if chat != str(self.config.chat_id):
                    continue  # seguridad: solo responde a tu chat
                if msg in ("/start", "/help"):
                    self._send("🤖 Comandos:\n/status — precios ambos exchanges\n/balance — saldos + total\n/history — últimos fills\n/pnl — PnL por controller\n/orders — órdenes abiertas\n/alerta 0.00000550 — aviso al tocar precio (/alerta off para cancelar)\n/ping — conexión")
                elif msg == "/status":
                    self._send(self._status_text())
                elif msg == "/balance":
                    self._send(self._balance_text())
                elif msg == "/history":
                    self._send(self._history_text())
                elif msg == "/pnl":
                    self._send(self._pnl_text())
                elif msg == "/orders":
                    self._send(self._orders_text())
                elif msg.startswith("/alerta"):
                    parts = msg.split()
                    if len(parts) < 2:
                        cur = self._alert_price if self._alert_price else "ninguna"
                        self._send(f"🚨 Alerta actual: {cur}\nUso: /alerta 0.00000550")
                    elif parts[1] == "off":
                        self._alert_price = None
                        self._send("🚨 Alerta de precio cancelada.")
                    else:
                        try:
                            self._alert_price = float(parts[1])
                            self._send(f"🚨 Alerta creada: avisaré si PEPE-USDT ≥ {self._alert_price}")
                        except ValueError:
                            self._send("⚠️ Precio inválido. Ej: /alerta 0.00000550")
                elif msg == "/ping":
                    self._send("🏓 Pong! Notificador integrado activo.")
        except Exception as e:
            self.logger().warning(f"Telegram notifier: poll failed: {e}")

    async def update_processed_data(self):
        pass

    def determine_executor_actions(self) -> List[ExecutorAction]:
        now = self.market_data_provider.time()
        if not self._started:
            self._started = True
            self._send("🤖 <b>Notificador integrado activo</b>. Comandos: /status /balance /history /ping. Te avisaré cada fill.")
        if now - self._last_check < self.config.check_interval_seconds:
            return []
        self._last_check = now
        self._poll_commands()
        self._check_alert()
        for db in self._db_paths():
            try:
                con = sqlite3.connect(db)
                rows = con.execute(
                    "SELECT timestamp, trade_type, market, symbol, order_id FROM TradeFill ORDER BY timestamp"
                ).fetchall()
                con.close()
            except Exception:
                continue
            for ts, side, market, symbol, oid in rows:
                key = (os.path.basename(db), ts, oid)
                if key in self._seen:
                    continue
                self._seen.add(key)
                icono = "🟢" if "BUY" in str(side).upper() else "🔴"
                self._send(f"{icono} <b>FILL {side}</b> {symbol} en {market}")
        return []

    def to_format_status(self) -> List[str]:
        return [f"Telegram notifier: {len(self._seen)} fills vistos, chequeo cada {self.config.check_interval_seconds}s"]
