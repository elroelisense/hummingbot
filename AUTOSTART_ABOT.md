# Arranque automatico de ABOT (Windows)

Objetivo: que tras un reinicio del PC (o si un proceso se cae) vuelvan solos:

- **Hummingbot** con la estrategia v2 ya iniciada (`conf_v6_global.yml`)
- **supervisor_abot.py**

Sin escribir nada en la consola y sin dejar dos instancias corriendo.

---

## 1. Instalacion (una sola vez)

### a) Guardar el password del keystore

El arranque automatico necesita abrir el keystore de Hummingbot sin que
nadie escriba el password. Se guarda **cifrado con DPAPI** (solo tu usuario de
Windows en este equipo puede descifrarlo):

```powershell
C:\Users\rober\.conda\envs\hummingbot\python.exe abot_set_password.py
```

Pide el password dos veces y lo valida contra `conf/.password_verification`
antes de guardarlo. No se escribe en ningun log ni en la linea de comandos.

Comprobar:

```powershell
C:\Users\rober\.conda\envs\hummingbot\python.exe abot_set_password.py --check
```

### b) Registrar la tarea programada

```powershell
powershell -ExecutionPolicy Bypass -File install_autostart.ps1
```

Crea la tarea **ABOT-Autostart**:

- se dispara **al iniciar sesion**
- y **cada 5 minutos** (red de seguridad si el watchdog muere)

La tarea lanza `abot_watchdog.py` con `pythonw.exe` (sin ventana).

---

## 2. Que hace cada pieza

| Fichero | Papel |
|---|---|
| `abot_watchdog.py` | proceso permanente. Cada 30 s comprueba bot y supervisor; arranca lo que falte. Nunca mata nada. |
| `abot_run_bot.py` | arranca **una** instancia del bot. Toma `data/hbot.lock`, cancela ordenes PEPE huerfanas y abre la consola normal con `hummingbot_quickstart.py --v2 conf_v6_global.yml`. |
| `abot_common.py` | locks, DPAPI, deteccion de procesos, cancelacion de ordenes, Telegram. |
| `abot_set_password.py` | guarda / comprueba / borra el password cifrado. |
| `install_autostart.ps1` | crea o borra la tarea programada. |

### Por que cancela ordenes antes de arrancar

Las escaleras (`ladder_b`, `ladder_buy_*`) colocan sus niveles **una vez por
proceso**. Si el bot muere con niveles vivos en el exchange y vuelve a arrancar,
los volveria a colocar y duplicaria el tramo. `abot_run_bot.py` cancela las
ordenes PEPE-USDT abiertas en Binance y OKX **solo cuando no hay ningun
Hummingbot vivo**, y solo entonces arranca. El PMM recrea sus ordenes solo.

---

## 3. Uso diario

```powershell
# estado
C:\Users\rober\.conda\envs\hummingbot\python.exe abot_watchdog.py --status

# parar los relanzamientos (mantenimiento: editar YAML, revisar, etc.)
C:\Users\rober\.conda\envs\hummingbot\python.exe abot_watchdog.py --pause

# volver a activarlos
C:\Users\rober\.conda\envs\hummingbot\python.exe abot_watchdog.py --resume
```

`--pause` **no** detiene el bot que ya esta corriendo: solo evita que el
watchdog lo relance. Para pararlo, usa la consola del bot (`stop` o `exit`)
sabiendo que si hay un executor en `trading` se cerrara a mercado.

Logs:

- `logs/abot_watchdog.log`
- `logs/abot_bot.log`
- `logs/logs_conf_v6_global.log` (el del bot)

---

## 4. Limitaciones honestas

1. **Hace falta sesion iniciada.** La tarea corre como tu usuario
   (`Interactive`) porque DPAPI esta atado a tu perfil. Si el PC reinicia y
   nadie inicia sesion, no arranca nada. Para que arranque sin tocar el equipo:
   activa el inicio de sesion automatico de Windows, o cambia la tarea a
   "ejecutar tanto si el usuario inicio sesion como si no" (eso exige guardar
   la contrasena de la cuenta de Windows en la tarea).
2. **El password del keystore queda en `data/hb_password.dat`** cifrado con
   DPAPI. Quien entre en tu sesion de Windows puede descifrarlo. Es el precio
   de arrancar sin intervencion.
3. **Un crash no cierra posiciones a mercado** (mejor para el PnL), pero deja
   la fila del executor como activa en SQLite y el inventario en spot. Al
   rearrancar, el PMM recoloca sus ordenes y las escaleras se recrean.
4. **El watchdog no sustituye al kill switch.** Si salta
   `max_global_drawdown_quote`, Hummingbot se para y el watchdog **lo vuelve a
   arrancar**. Si no quieres ese comportamiento, sube el umbral o usa
   `--pause` antes de operar en un mercado que no quieras.
5. **Cerrar la ventana del bot con la X** equivale a matarlo: el watchdog lo
   relanzara en ~1 minuto. Para pararlo de verdad, `--pause` primero.

---

## 5. Desinstalar

```powershell
powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Uninstall
C:\Users\rober\.conda\envs\hummingbot\python.exe abot_set_password.py --clear
```
