"""Watchdog de ABOT: mantiene vivos Hummingbot y el supervisor.

Uso:
    python abot_watchdog.py            # bucle permanente (lo normal)
    python abot_watchdog.py --once     # una pasada y sale
    python abot_watchdog.py --status   # diagnostico
    python abot_watchdog.py --pause    # modo mantenimiento (no relanza nada)
    python abot_watchdog.py --resume   # sale de mantenimiento

Reglas:
  - nunca mata procesos: solo arranca lo que falta.
  - si el bot muere, antes de relanzarlo se cancelan las ordenes PEPE-USDT
    huerfanas (lo hace abot_run_bot.py) para no duplicar escaleras.
  - backoff exponencial (60s -> 15 min) si algo no levanta.
  - si falta el password del keystore, no intenta arrancar y avisa una vez.
"""

import argparse
import json
import os
import subprocess
import sys
import time

from abot_common import (
    BASE_DIR,
    CREATE_NO_WINDOW,
    DATA_DIR,
    MAINTENANCE_FLAG,
    PASSWORD_FILE,
    WATCHDOG_LOCK,
    bot_is_running,
    bot_pids,
    bot_python,
    bot_pythonw,
    last_drawdown_ts,
    list_python_processes,
    load_password,
    make_logger,
    supervisor_is_running,
    supervisor_pids,
    telegram,
    validate_hb_password,
)

STATE_FILE = DATA_DIR / "abot_watchdog_state.json"
LOOP_SECONDS = 30
MIN_RETRY_SECONDS = 60
MAX_RETRY_SECONDS = 900
STABLE_SECONDS = 300

log = make_logger("abot_watchdog")


# ---------------------------------------------------------------------------
# estado persistente (backoff sobrevive a reinicios del watchdog)
# ---------------------------------------------------------------------------


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _slot(state: dict, name: str) -> dict:
    return state.setdefault(name, {"last_start": 0, "failures": 0, "alerted": False})


def _retry_delay(slot: dict) -> int:
    return min(MIN_RETRY_SECONDS * (2 ** max(0, slot.get("failures", 0) - 1)), MAX_RETRY_SECONDS)


# ---------------------------------------------------------------------------
# arranques
# ---------------------------------------------------------------------------


def start_bot(state: dict) -> bool:
    slot = _slot(state, "bot")
    now = time.time()
    if now - slot.get("last_start", 0) < _retry_delay(slot):
        log(f"bot caido; backoff activo ({int(_retry_delay(slot) - (now - slot['last_start']))}s)")
        return False
    slot["last_start"] = now
    slot["failures"] = slot.get("failures", 0) + 1
    log(f"bot caido -> lanzo abot_run_bot.py (intento {slot['failures']})")
    try:
        subprocess.Popen(
            [bot_pythonw(), str(BASE_DIR / "abot_run_bot.py")],
            cwd=str(BASE_DIR), creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        log(f"[ERROR] no pude lanzar abot_run_bot.py: {e}")
        return False
    if slot["failures"] == 1:
        telegram("[ABOT] Hummingbot estaba caido. Relanzando.")
    elif slot["failures"] >= 3 and not slot.get("alerted"):
        slot["alerted"] = True
        telegram(f"[ABOT] Hummingbot lleva {slot['failures']} intentos sin levantar. "
                 f"Revisa logs/abot_bot.log")
    return True


def start_supervisor(state: dict) -> bool:
    slot = _slot(state, "supervisor")
    now = time.time()
    if now - slot.get("last_start", 0) < _retry_delay(slot):
        return False
    slot["last_start"] = now
    slot["failures"] = slot.get("failures", 0) + 1
    log(f"supervisor caido -> lo lanzo (intento {slot['failures']})")
    try:
        subprocess.Popen(
            [bot_python(), str(BASE_DIR / "supervisor_abot.py")],
            cwd=str(BASE_DIR), creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        log(f"[ERROR] no pude lanzar supervisor_abot.py: {e}")
        return False
    if slot["failures"] >= 3 and not slot.get("alerted"):
        slot["alerted"] = True
        telegram("[ABOT] El supervisor lleva 3 intentos sin levantar. Revisa logs/supervisor_abot.log")
    return True


def _mark_stable(state: dict, name: str, running: bool):
    """Si lleva >5 min arriba, resetea el contador de fallos."""
    slot = _slot(state, name)
    if running and slot.get("failures", 0) and time.time() - slot.get("last_start", 0) > STABLE_SECONDS:
        slot["failures"] = 0
        slot["alerted"] = False


_last_msg = {}


def _throttled(key: str, msg: str, logger, every: int = 600):
    now = time.time()
    if now - _last_msg.get(key, 0) >= every:
        _last_msg[key] = now
        logger(msg)


# ---------------------------------------------------------------------------
# pasada
# ---------------------------------------------------------------------------


def ensure_once(logger=log) -> dict:
    state = load_state()
    result = {"bot": None, "supervisor": None, "maintenance": MAINTENANCE_FLAG.exists()}

    # Primera ejecucion: damos por "ya vistos" los drawdowns historicos del log.
    if "kill_switch_ts" not in state:
        state["kill_switch_ts"] = last_drawdown_ts() or 0
        save_state(state)

    if result["maintenance"]:
        _throttled("maint", "modo mantenimiento: no relanzo nada", logger)
        return result

    # Si el kill switch acaba de dispararse, NO relanzamos: cerrar executors a
    # mercado y volver a entrar en bucle es justo la fuga que causo -4.20 USDT.
    dd = last_drawdown_ts()
    if dd and dd > state.get("kill_switch_ts", 0):
        state["kill_switch_ts"] = dd
        save_state(state)
        MAINTENANCE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        MAINTENANCE_FLAG.write_text(
            f"kill switch {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(dd))}\n",
            encoding="utf-8")
        msg = ("[ABOT] KILL SWITCH disparado "
               f"({time.strftime('%d/%m %H:%M', time.localtime(dd))}).\n"
               "He activado modo mantenimiento y NO relanzo el bot para no "
               "cerrar posiciones en bucle.\nRevisa y luego: abot_watchdog.py --resume")
        logger("kill switch detectado -> modo mantenimiento activado")
        telegram(msg)
        result["maintenance"] = True
        return result

    bot_up = bot_is_running()
    _mark_stable(state, "bot", bot_up)
    if bot_up:
        result["bot"] = "up"
    else:
        if not PASSWORD_FILE.exists():
            _throttled("nopw", "falta data/hb_password.dat: no puedo arrancar el bot", logger)
            slot = _slot(state, "bot")
            if not slot.get("alerted"):
                slot["alerted"] = True
                telegram("[ABOT] Falta el password del keystore. El bot NO arrancara solo.\n"
                         "Ejecuta: python abot_set_password.py")
        elif start_bot(state):
            result["bot"] = "starting"
        else:
            result["bot"] = "down"

    sup_up = supervisor_is_running()
    _mark_stable(state, "supervisor", sup_up)
    if sup_up:
        result["supervisor"] = "up"
    else:
        start_supervisor(state)
        result["supervisor"] = "starting"

    if result["bot"] == "up" and result["supervisor"] == "up":
        _throttled("ok", f"todo OK: bot PID {bot_pids()} | supervisor PID {supervisor_pids()}", logger)

    save_state(state)
    return result


# ---------------------------------------------------------------------------
# diagnostico
# ---------------------------------------------------------------------------


def print_status():
    print(f"directorio   : {BASE_DIR}")
    print(f"python bot   : {bot_python()}")
    print(f"password     : {'OK (data/hb_password.dat)' if PASSWORD_FILE.exists() else 'FALTA'}")
    if PASSWORD_FILE.exists():
        print(f"password valido: {'si' if validate_hb_password(load_password() or '') else 'NO'}")
    print(f"mantenimiento: {'SI' if MAINTENANCE_FLAG.exists() else 'no'}")
    print(f"bot          : {'corriendo' if bot_is_running() else 'PARADO'}  pids={bot_pids()}")
    print(f"supervisor   : {'corriendo' if supervisor_is_running() else 'PARADO'}  pids={supervisor_pids()}")
    dd = last_drawdown_ts()
    print(f"kill switch  : {'ultimo ' + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(dd)) if dd else 'sin registro'}")
    st = load_state()
    for name in ("bot", "supervisor"):
        s = st.get(name, {})
        if s:
            print(f"  {name}: intentos={s.get('failures', 0)} "
                  f"ultimo_arranque={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.get('last_start', 0))) if s.get('last_start') else '-'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Watchdog ABOT")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--pause", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    if args.status:
        print_status()
        return 0

    if args.pause:
        MAINTENANCE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        MAINTENANCE_FLAG.write_text("pausa manual\n", encoding="utf-8")
        log("modo mantenimiento ACTIVADO (el watchdog no relanzara nada)")
        return 0

    if args.resume:
        if MAINTENANCE_FLAG.exists():
            MAINTENANCE_FLAG.unlink()
        log("modo mantenimiento DESACTIVADO")
        return 0

    from abot_common import InstanceLock

    if args.once:
        # Idempotente y seguro sin el lock del watchdog: los locks de bot y
        # supervisor impiden duplicados aunque corran dos pasadas a la vez.
        print(ensure_once())
        return 0

    lock = InstanceLock(WATCHDOG_LOCK)
    if not lock.acquire():
        log("ya hay otro watchdog activo: salgo")
        return 0

    try:
        log("=" * 70)
        log("watchdog ABOT iniciado")
        while True:
            try:
                ensure_once()
            except Exception as e:
                log(f"[ERROR] pasada fallida: {e}")
                import traceback
                log(traceback.format_exc())
            time.sleep(LOOP_SECONDS)
    except KeyboardInterrupt:
        log("detenido por el usuario")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
