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
  - si el bot esta vivo pero el log no se actualiza en 10 min, lo trata
    como colgado y lo reinicia.
  - escribe un heartbeat en data/abot_heartbeat.txt cada ciclo.
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
LOG_STALE_SECONDS = 600
HEARTBEAT_FILE = DATA_DIR / "abot_heartbeat.txt"

log = make_logger("abot_watchdog")


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


def start_bot(state: dict) -> bool:
    slot = _slot(state, "bot")
    now = time.time()
    if now - slot.get("last_start", 0) < _retry_delay(slot):
        log("bot caido; backoff activo (%d s)" % int(_retry_delay(slot) - (now - slot['last_start'])))
        return False
    slot["last_start"] = now
    slot["failures"] = slot.get("failures", 0) + 1
    log("bot caido -> lanzo abot_run_bot.py (intento %d)" % slot["failures"])
    try:
        subprocess.Popen(
            [bot_pythonw(), str(BASE_DIR / "abot_run_bot.py")],
            cwd=str(BASE_DIR), creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        log("[ERROR] no pude lanzar abot_run_bot.py: %s" % e)
        return False
    if slot["failures"] == 1:
        telegram("[ABOT] Hummingbot estaba caido. Relanzando.")
    elif slot["failures"] >= 3 and not slot.get("alerted"):
        slot["alerted"] = True
        telegram("[ABOT] Hummingbot lleva %d intentos sin levantar. Revisa logs/abot_bot.log" % slot["failures"])
    return True


def start_supervisor(state: dict) -> bool:
    slot = _slot(state, "supervisor")
    now = time.time()
    if now - slot.get("last_start", 0) < _retry_delay(slot):
        return False
    slot["last_start"] = now
    slot["failures"] = slot.get("failures", 0) + 1
    log("supervisor caido -> lo lanzo (intento %d)" % slot["failures"])
    try:
        subprocess.Popen(
            [bot_python(), str(BASE_DIR / "supervisor_abot.py")],
            cwd=str(BASE_DIR), creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        log("[ERROR] no pude lanzar supervisor_abot.py: %s" % e)
        return False
    if slot["failures"] >= 3 and not slot.get("alerted"):
        slot["alerted"] = True
        telegram("[ABOT] El supervisor lleva 3 intentos sin levantar. Revisa logs/supervisor_abot.log")
    return True


def _mark_stable(state: dict, name: str, running: bool):
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


def _write_heartbeat():
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(time.strftime('%Y-%m-%d %H:%M:%S'), encoding="utf-8")
    except Exception:
        pass


def ensure_once(logger=log) -> dict:
    state = load_state()
    result = {"bot": None, "supervisor": None, "maintenance": MAINTENANCE_FLAG.exists()}

    # Primera ejecucion: damos por "ya vistos" los drawdowns historicos del log.
    if "kill_switch_ts" not in state:
        state["kill_switch_ts"] = last_drawdown_ts() or 0
        save_state(state)

    if result["maintenance"]:
        _throttled("maint", "modo mantenimiento: no relanzo nada", logger)
        _write_heartbeat()
        return result

    # Si el kill switch acaba de dispararse, NO relanzamos.
    dd = last_drawdown_ts()
    if dd and dd > state.get("kill_switch_ts", 0):
        state["kill_switch_ts"] = dd
        save_state(state)
        MAINTENANCE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        MAINTENANCE_FLAG.write_text(
            "kill switch %s\n" % time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(dd)),
            encoding="utf-8")
        msg = ("[ABOT] KILL SWITCH disparado (%s).\n"
               "He activado modo mantenimiento y NO relanzo el bot para no "
               "cerrar posiciones en bucle.\nRevisa y luego: abot_watchdog.py --resume" %
               time.strftime('%d/%m %H:%M', time.localtime(dd)))
        logger("kill switch detectado -> modo mantenimiento activado")
        telegram(msg)
        result["maintenance"] = True
        _write_heartbeat()
        return result

    bot_up = bot_is_running()
    bot_stuck = False

    # Deteccion de bot "colgado": proceso vivo pero log sin actualizar en 10 min
    # O proceso vivo pero sin actividad de trading en 15 min.
    if bot_up:
        try:
            log_path = BASE_DIR / "logs" / "logs_conf_v6_global.log"
            if log_path.exists():
                stale = time.time() - log_path.stat().st_mtime
                if stale > LOG_STALE_SECONDS:
                    logger("bot vivo pero log sin actualizar hace %.0f s -> COLGADO" % stale)
                    bot_stuck = True
                    bot_up = False
                else:
                    # Health check de trading: verificar que el bot esta operando,
                    # no solo que el proceso esta vivo. Si en los ultimos 15 min
                    # no hay ninguna linea de orden creada/llenada, esta stuck.
                    # Tambien detectar loops de "Not enough budget" (>20 en 10 min).
                    try:
                        size = log_path.stat().st_size
                        with open(log_path, "rb") as f:
                            f.seek(max(0, size - 200000))
                            tail = f.read().decode("utf-8", "replace")
                        trading_keywords = ("Created", "filled", "Filled", "OrderCreated", "OrderFilled", "completely filled")
                        trading_lines = [l for l in tail.splitlines() if any(k in l for k in trading_keywords)]
                        if trading_lines:
                            last_trading_ts = None
                            for l in reversed(trading_lines):
                                m = __import__("re").match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", l)
                                if m:
                                    import datetime
                                    last_trading_ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                                    break
                            if last_trading_ts and (time.time() - last_trading_ts) > 900:
                                logger("bot vivo pero sin actividad de trading en %.0f min -> COLGADO" % ((time.time()-last_trading_ts)/60))
                                bot_stuck = True
                                bot_up = False
                        # Detectar loop de "Not enough budget"
                        budget_errors = [l for l in tail.splitlines() if "Not enough budget" in l]
                        if len(budget_errors) > 20:
                            last_success = None
                            for l in reversed(tail.splitlines()):
                                if "Created" in l or "completely filled" in l:
                                    m = __import__("re").match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", l)
                                    if m:
                                        import datetime
                                        last_success = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                                        break
                            last_error = None
                            for l in reversed(budget_errors):
                                m = __import__("re").match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", l)
                                if m:
                                    import datetime
                                    last_error = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                                    break
                            # Si hay >20 errores de budget en 10 min, esta stuck
                            # sin importar si hay otras ordenes exitosas.
                            if len(budget_errors) > 20:
                                logger("bot vivo pero en loop de 'Not enough budget' (%d errores) -> COLGADO" % len(budget_errors))
                                bot_stuck = True
                                bot_up = False
                    except Exception:
                        pass
        except Exception:
            pass

    # Si el bot esta colgado: matarlo, cancelar ordenes huérfanas, resetear backoff.
    if bot_stuck:
        pids = bot_pids()
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30)
                logger("bot colgado (PID %d) terminado" % pid)
            except Exception as e:
                logger("[WARN] no se pudo matar PID %d: %s" % (pid, e))
        time.sleep(3)
        # Cancelar ordenes PMM huérfanas (coste $0 si no hay posiciones)
        try:
            sys.path.insert(0, str(BASE_DIR))
            from abot_common import cancel_open_orders
            n = cancel_open_orders(logger)
            if n:
                logger("canceladas %d ordenes PMM huérfanas" % n)
        except Exception as e:
            logger("[WARN] cancel_open_orders fallo: %s" % e)
        # Resetear backoff
        slot = _slot(state, "bot")
        slot["last_start"] = 0
        slot["failures"] = 0
        slot["alerted"] = False
        save_state(state)
        telegram("[ABOT] Bot colgado detectado y reiniciado. Ordenes PMM canceladas (coste $0).")

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
        _throttled("ok", "todo OK: bot PID %s | supervisor PID %s" % (bot_pids(), supervisor_pids()), logger)

    _write_heartbeat()
    save_state(state)
    return result


def print_status():
    print("directorio   : %s" % BASE_DIR)
    print("python bot   : %s" % bot_python())
    print("password     : %s" % ('OK (data/hb_password.dat)' if PASSWORD_FILE.exists() else 'FALTA'))
    if PASSWORD_FILE.exists():
        print("password valido: %s" % ('si' if validate_hb_password(load_password() or '') else 'NO'))
    print("mantenimiento: %s" % ('SI' if MAINTENANCE_FLAG.exists() else 'no'))
    print("bot          : %s  pids=%s" % ('corriendo' if bot_is_running() else 'PARADO', bot_pids()))
    print("supervisor   : %s  pids=%s" % ('corriendo' if supervisor_is_running() else 'PARADO', supervisor_pids()))
    dd = last_drawdown_ts()
    print("kill switch  : %s" % ('ultimo %s' % time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(dd)) if dd else 'sin registro'))
    st = load_state()
    for name in ("bot", "supervisor"):
        s = st.get(name, {})
        if s:
            print("  %s: intentos=%d ultimo_arranque=%s" % (
                name, s.get('failures', 0),
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.get('last_start', 0))) if s.get('last_start') else '-'))
    try:
        hb = HEARTBEAT_FILE.read_text(encoding="utf-8").strip() if HEARTBEAT_FILE.exists() else "nunca"
        print("heartbeat    : %s" % hb)
    except Exception:
        pass


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

    if args.once:
        print(ensure_once())
        return 0

    from abot_common import InstanceLock
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
                log("[ERROR] pasada fallida: %s" % e)
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
