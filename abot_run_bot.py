"""Arranca UNA instancia de Hummingbot con la estrategia v2 ya iniciada.

Lo lanza el watchdog (o a mano). Hace, en orden:
  1. toma el lock data/hbot.lock (si ya hay otra instancia, sale)
  2. comprueba que no haya un Hummingbot externo vivo
  3. descifra el password del keystore (DPAPI) y lo valida
  4. cancela ordenes PEPE-USDT huerfanas del proceso anterior (evita escaleras duplicadas)
  5. abre una consola normal con `hummingbot_quickstart.py --v2 conf_v6_global.yml`
     (la consola queda usable: status, stop, exit, ...)
  6. espera a que el bot termine y libera el lock

El password viaja al hijo por la variable de entorno CONFIG_PASSWORD,
nunca por la linea de comandos.
"""

import os
import subprocess
import sys
import time

from abot_common import (
    BASE_DIR,
    BOT_LOCK,
    CREATE_NEW_CONSOLE,
    MAINTENANCE_FLAG,
    V2_CONF,
    InstanceLock,
    bot_pids,
    bot_python,
    cancel_open_orders,
    load_password,
    make_logger,
    telegram,
    validate_hb_password,
)

log = make_logger("abot_bot")


def main() -> int:
    if MAINTENANCE_FLAG.exists():
        log("modo mantenimiento activo: no arranco el bot")
        return 0

    lock = InstanceLock(BOT_LOCK)
    if not lock.acquire():
        log("ya hay un bot ABOT activo (lock tomado): no arranco otro")
        return 0

    try:
        externos = bot_pids()
        if externos:
            log(f"Hummingbot ya corriendo fuera del watchdog (PID {externos}): no arranco otro")
            return 0

        password = load_password()
        if not password:
            log("FALTA data/hb_password.dat. Ejecuta: python abot_set_password.py")
            telegram("[ABOT] No pude arrancar: falta el password del keystore.\n"
                      "Ejecuta: python abot_set_password.py")
            return 2

        if not validate_hb_password(password):
            log("el password guardado NO abre el keystore (conf/.password_verification)")
            telegram("[ABOT] No pude arrancar: el password guardado no abre el keystore.")
            return 2

        n = cancel_open_orders(log)
        if n:
            log(f"{n} ordenes huerfanas canceladas antes de arrancar")

        env = os.environ.copy()
        env["CONFIG_PASSWORD"] = password
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # prompt_toolkit falla si TERM=xterm-256color (Git Bash/Cygwin en PATH)
        env.pop("TERM", None)
        env["TERM"] = ""

        cmd = [bot_python(), str(BASE_DIR / "bin" / "hummingbot_quickstart.py"), "--v2", V2_CONF]
        log(f"lanzando: {' '.join(cmd)}")
        started = time.time()
        proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), env=env, creationflags=CREATE_NEW_CONSOLE)
        log(f"Hummingbot PID {proc.pid} (consola abierta, estrategia {V2_CONF})")
        telegram(f"[ABOT] Hummingbot arrancado (PID {proc.pid}) con {V2_CONF}")

        rc = proc.wait()
        dur = time.time() - started
        log(f"Hummingbot termino rc={rc} tras {dur / 60:.1f} min")
        telegram(f"[ABOT] Hummingbot se detuvo (rc={rc}, {dur / 60:.1f} min). "
                  f"El watchdog lo relanza salvo que este en mantenimiento.")
        return rc
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
