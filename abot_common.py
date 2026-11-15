"""Utilidades compartidas del arranque automatico ABOT (Windows).

Contiene:
  - rutas y logger con rotacion
  - lock de instancia unica (msvcrt) para bot / supervisor / watchdog
  - cifrado DPAPI del password del keystore (nunca en texto plano)
  - deteccion de procesos python (sin psutil)
  - cancelacion de ordenes huerfanas antes de rearrancar el bot
  - envio a Telegram reutilizando el token del notificador
"""

import base64
import ctypes
import datetime
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import yaml

# ============================================================================
# RUTAS
# ============================================================================

BASE_DIR = Path(r"C:\Users\rober\.config\opencode\hummingbot")
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
CONF_CONTROLLERS = BASE_DIR / "conf" / "controllers"

CREDS_FILE = BASE_DIR / "creds_abot.json"
TELEGRAM_CONF = CONF_CONTROLLERS / "conf_telegram_notifier.yml"
PASSWORD_FILE = DATA_DIR / "hb_password.dat"
MAINTENANCE_FLAG = DATA_DIR / "ABOT_MAINTENANCE"

BOT_LOCK = DATA_DIR / "hbot.lock"
SUPERVISOR_LOCK = DATA_DIR / "supervisor_abot.lock"
WATCHDOG_LOCK = DATA_DIR / "abot_watchdog.lock"

# El bot corre en el entorno conda, no en el Python del sistema.
CONDA_PYTHON = Path(r"C:\Users\rober\.conda\envs\hummingbot\python.exe")
CONDA_PYTHONW = Path(r"C:\Users\rober\.conda\envs\hummingbot\pythonw.exe")

V2_CONF = "conf_v6_global.yml"
PAIR = "PEPE-USDT"
BINANCE_SYMBOL = "PEPEUSDT"
OKX_INST = "PEPE-USDT"
HTTP_TIMEOUT = 15

DPAPI_ENTROPY = b"abot-autostart-v1"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def bot_python() -> str:
    return str(CONDA_PYTHON if CONDA_PYTHON.exists() else Path(sys.executable))


def bot_pythonw() -> str:
    if CONDA_PYTHONW.exists():
        return str(CONDA_PYTHONW)
    alt = Path(bot_python()).with_name("pythonw.exe")
    return str(alt) if alt.exists() else bot_python()


# ============================================================================
# LOG
# ============================================================================


def make_logger(name: str, max_bytes: int = 5 * 1024 * 1024):
    path = LOG_DIR / f"{name}.log"

    def log(msg: str):
        line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} | {msg}"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > max_bytes:
                os.replace(path, path.with_suffix(".log.1"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        # En consola solo si hay una (pythonw no la tiene).
        try:
            print(line, flush=True)
        except Exception:
            pass

    return log


# ============================================================================
# LOCK DE INSTANCIA UNICA
# ============================================================================


class InstanceLock:
    """Lock exclusivo por fichero. Se libera solo si el proceso muere."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
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
        self._handle = handle
        return True

    def release(self):
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            self._handle.close()
        finally:
            self._handle = None


def lock_is_held(path: Path) -> bool:
    """True si otro proceso mantiene el lock (no lo adquirimos)."""
    lock = InstanceLock(path)
    if lock.acquire():
        lock.release()
        return False
    return True


# ============================================================================
# PASSWORD DEL KEYSTORE (DPAPI, ambito del usuario actual)
# ============================================================================


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> Tuple[_DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def _crypt32():
    lib = ctypes.WinDLL("crypt32", use_last_error=True)
    lib.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    lib.CryptProtectData.restype = wintypes.BOOL
    lib.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    lib.CryptUnprotectData.restype = wintypes.BOOL
    return lib


def dpapi_protect(data: bytes) -> bytes:
    lib = _crypt32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    in_blob, _b1 = _blob(data)
    ent_blob, _b2 = _blob(DPAPI_ENTROPY)
    out = _DATA_BLOB()
    if not lib.CryptProtectData(ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
                                None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    lib = _crypt32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    in_blob, _b1 = _blob(data)
    ent_blob, _b2 = _blob(DPAPI_ENTROPY)
    out = _DATA_BLOB()
    if not lib.CryptUnprotectData(ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
                                  None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def save_password(password: str):
    PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.write_bytes(dpapi_protect(password.encode("utf-8")))
    try:
        os.chmod(PASSWORD_FILE, 0o600)
    except Exception:
        pass


def load_password() -> Optional[str]:
    try:
        if not PASSWORD_FILE.exists():
            return None
        return dpapi_unprotect(PASSWORD_FILE.read_bytes()).decode("utf-8")
    except Exception:
        return None


def validate_hb_password(password: str) -> bool:
    """Comprueba el password contra conf/.password_verification sin arrancar el bot."""
    try:
        from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger, validate_password
        return bool(validate_password(ETHKeyFileSecretManger(password)))
    except Exception:
        return False


# ============================================================================
# PROCESOS
# ============================================================================

_PS_LIST = (
    "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\"; "
    "$p | ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"
)


def list_python_processes() -> List[Tuple[int, str]]:
    """[(pid, commandline)] de todos los python/pythonw vivos."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_LIST],
            capture_output=True, text=True, timeout=25, creationflags=CREATE_NO_WINDOW,
        )
        rows = []
        for line in out.stdout.splitlines():
            pid, _, cmd = line.partition("|")
            if pid.strip().isdigit():
                rows.append((int(pid), cmd))
        return rows
    except Exception:
        return []


def bot_pids() -> List[int]:
    pat = re.compile(r"bin[\\/]hummingbot(_quickstart)?\.py", re.IGNORECASE)
    me = os.getpid()
    return [pid for pid, cmd in list_python_processes() if pid != me and pat.search(cmd or "")]


def supervisor_pids() -> List[int]:
    pat = re.compile(r"supervisor_abot\.py", re.IGNORECASE)
    me = os.getpid()
    return [pid for pid, cmd in list_python_processes() if pid != me and pat.search(cmd or "")]


def bot_is_running() -> bool:
    return lock_is_held(BOT_LOCK) or bool(bot_pids())


def supervisor_is_running() -> bool:
    return lock_is_held(SUPERVISOR_LOCK) or bool(supervisor_pids())


# ============================================================================
# KILL SWITCH (max_global_drawdown_quote)
# ============================================================================

BOT_LOG = LOG_DIR / "logs_conf_v6_global.log"
DRAWDOWN_MARKER = "Global drawdown reached"


def last_drawdown_ts() -> Optional[float]:
    """Timestamp del ultimo 'Global drawdown reached' en el log del bot.

    El mensaje puede venir en la misma linea que el timestamp o en la
    siguiente (Hummingbot parte el texto), asi que se arrastra el ultimo
    timestamp visto.
    """
    try:
        if not BOT_LOG.exists():
            return None
        size = BOT_LOG.stat().st_size
        with open(BOT_LOG, "rb") as f:
            f.seek(max(0, size - 5_000_000))
            data = f.read().decode("utf-8", "replace")
    except Exception:
        return None

    last_ts = None
    hit = None
    for line in data.splitlines():
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if m:
            try:
                last_ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                pass
        if DRAWDOWN_MARKER in line and last_ts is not None:
            hit = last_ts
    return hit


# ============================================================================
# TELEGRAM
# ============================================================================


def telegram(text: str) -> bool:
    try:
        with open(TELEGRAM_CONF, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        token, chat = cfg.get("bot_token"), cfg.get("chat_id")
        if not token or not chat:
            return False
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": str(chat), "text": text, "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
        return bool(r.ok)
    except Exception:
        return False


# ============================================================================
# CANCELACION DE ORDENES HUERFANAS
# ============================================================================


def load_credentials() -> Optional[Dict]:
    try:
        with open(CREDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _binance_cancel_all(creds: Dict, log) -> int:
    key = creds["binance"]["api_key"]
    secret = creds["binance"]["secret_key"]
    ts = int(time.time() * 1000)
    query = f"symbol={BINANCE_SYMBOL}&timestamp={ts}&recvWindow=5000"
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    r = requests.delete(
        "https://api.binance.com/api/v3/openOrders",
        params={"symbol": BINANCE_SYMBOL, "timestamp": ts, "recvWindow": 5000, "signature": sig},
        headers={"X-MBX-APIKEY": key}, timeout=HTTP_TIMEOUT,
    )
    data = r.json()
    if isinstance(data, list):
        return len(data)
    log(f"[WARN] Binance cancel: {data.get('msg', data)}")
    return 0


def _okx_headers(creds: Dict, method: str, path: str, body: Optional[str] = None) -> Dict[str, str]:
    key = creds["okx"]["api_key"]
    secret = creds["okx"]["secret_key"]
    passphrase = creds["okx"]["passphrase"]
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    sign_str = ts + method + path
    if body is not None:
        sign_str += body
    sign = base64.b64encode(
        hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }


def _okx_cancel_all(creds: Dict, log) -> int:
    path = f"/api/v5/trade/orders-pending?instType=SPOT&instId={OKX_INST}"
    r = requests.get("https://www.okx.com" + path,
                     headers=_okx_headers(creds, "GET", path), timeout=HTTP_TIMEOUT)
    data = r.json()
    if data.get("code") != "0":
        log(f"[WARN] OKX pending: {data.get('msg')} ({data.get('code')})")
        return 0
    orders = [o["ordId"] for o in data.get("data", []) if o.get("ordId")]
    cancelled = 0
    for i in range(0, len(orders), 20):
        chunk = orders[i:i + 20]
        body = [{"instId": OKX_INST, "ordId": oid} for oid in chunk]
        body_str = json.dumps(body, separators=(",", ":"))
        path2 = "/api/v5/trade/cancel-batch-orders"
        r2 = requests.post("https://www.okx.com" + path2, data=body_str,
                           headers=_okx_headers(creds, "POST", path2, body_str), timeout=HTTP_TIMEOUT)
        d2 = r2.json()
        if d2.get("code") != "0":
            log(f"[WARN] OKX cancel: {d2.get('msg')} ({d2.get('code')})")
            continue
        cancelled += sum(1 for x in d2.get("data", []) if x.get("sCode") in ("0", 0))
    return cancelled


def cancel_open_orders(log) -> int:
    """Cancela ordenes PEPE-USDT abiertas en Binance y OKX.

    Se llama solo cuando NO hay ningun Hummingbot vivo, para que el bot que
    arranca no duplique las escaleras que quedaron colgadas del proceso anterior.
    """
    creds = load_credentials()
    if not creds:
        log("[WARN] sin creds_abot.json: no puedo cancelar ordenes huerfanas")
        return 0
    total = 0
    for name, fn in (("Binance", _binance_cancel_all), ("OKX", _okx_cancel_all)):
        try:
            n = fn(creds, log)
            total += n
            log(f"ordenes huerfanas canceladas en {name}: {n}")
        except Exception as e:
            log(f"[WARN] cancelar en {name}: {e}")
    return total


# ============================================================================
# SELF-TEST
# ============================================================================


def _self_test() -> int:
    ok = True
    sample = b"prueba-dpapi-abot"
    try:
        assert dpapi_unprotect(dpapi_protect(sample)) == sample
        print("DPAPI protect/unprotect: OK")
    except Exception as e:
        ok = False
        print(f"DPAPI: FALLO ({e})")

    probe = DATA_DIR / "_lock_probe.tmp"
    l1, l2 = InstanceLock(probe), InstanceLock(probe)
    try:
        assert l1.acquire() is True
        assert l2.acquire() is False
        assert lock_is_held(probe) is True
        l1.release()
        assert lock_is_held(probe) is False
        print("Lock de instancia unica: OK")
    except Exception as e:
        ok = False
        print(f"Lock: FALLO ({e})")
    finally:
        l1.release()
        l2.release()
        try:
            probe.unlink()
        except Exception:
            pass

    procs = list_python_processes()
    print(f"Deteccion de procesos: {'OK' if procs else 'sin resultados'} ({len(procs)} python vivos)")
    print(f"bot_pids()={bot_pids()} supervisor_pids()={supervisor_pids()}")
    print(f"password guardado: {'si' if PASSWORD_FILE.exists() else 'NO'}")
    print(f"python del bot: {bot_python()}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_self_test())
