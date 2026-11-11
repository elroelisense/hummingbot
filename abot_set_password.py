"""Guarda el password del keystore de Hummingbot cifrado con DPAPI.

Uso (una sola vez, en tu terminal):

    C:\\Users\\rober\\.conda\\envs\\hummingbot\\python.exe abot_set_password.py

El password nunca se escribe en texto plano: se cifra con DPAPI y solo lo
puede descifrar tu usuario de Windows en este equipo. El arranque automatico
lo lee de data/hb_password.dat.

Opciones:
    --check   comprueba que existe y que abre el keystore
    --clear   borra el password guardado
"""

import argparse
import getpass
import os
import subprocess
import sys

from abot_common import (
    BASE_DIR,
    PASSWORD_FILE,
    bot_python,
    load_password,
    save_password,
    validate_hb_password,
)

_VALIDATE_CODE = (
    "import os, sys;"
    "from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger, validate_password;"
    "sys.exit(0 if validate_password(ETHKeyFileSecretManger(os.environ['ABOT_CHECK_PW'])) else 1)"
)


def _validate_out_of_process(password: str) -> bool:
    """Valida con el python del bot (conda), aunque este script corra en otro."""
    env = os.environ.copy()
    env["ABOT_CHECK_PW"] = password
    try:
        r = subprocess.run([bot_python(), "-c", _VALIDATE_CODE], cwd=str(BASE_DIR),
                           env=env, timeout=120)
        return r.returncode == 0
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Password del keystore para el autostart ABOT")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()

    if args.clear:
        if PASSWORD_FILE.exists():
            PASSWORD_FILE.unlink()
            print(f"Borrado {PASSWORD_FILE}")
        else:
            print("No habia password guardado.")
        return 0

    if args.check:
        pw = load_password()
        if not pw:
            print("NO hay password guardado (o DPAPI no pudo descifrarlo).")
            return 1
        ok = validate_hb_password(pw) or _validate_out_of_process(pw)
        print(f"Password guardado y {'VALIDA el keystore' if ok else 'NO valida el keystore'}.")
        return 0 if ok else 2

    pw = getpass.getpass("Password del keystore de Hummingbot: ")
    if not pw:
        print("Vacio, no guardo nada.")
        return 1
    if getpass.getpass("Repite el password: ") != pw:
        print("No coinciden.")
        return 1

    print("Validando contra conf/.password_verification ...")
    if not (validate_hb_password(pw) or _validate_out_of_process(pw)):
        print("Password INCORRECTA: no abre el keystore. No guardo nada.")
        return 2

    save_password(pw)
    print(f"OK. Password cifrado con DPAPI en {PASSWORD_FILE}")
    print("Solo tu usuario de Windows puede descifrarlo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
