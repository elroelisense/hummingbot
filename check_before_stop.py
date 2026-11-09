"""PRE-STOP CHECK — ejecutar ANTES de cualquier `stop` en Hummingbot.

Uso (desde la terminal de Hummingbot, NO dentro del bot):
    python check_before_stop.py

Lee la base de datos del script activo y responde:
  - ¿Hay executors en trading (posicion abierta esperando cierre)?
  - ¿Cuanto se llenó y con que PnL/fees hasta ahora?
Si hay posicion abierta, un `stop` la cerrara a MERCADO al precio
que pida el mercado (ver 08-nov: costo ~$0.20-0.40).
Regla: si dice "POSICION ABIERTA", NO parar sin aprobacion del asesor.
"""
import sqlite3, glob, os, sys, datetime

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

def dt(s):
    try:
        return datetime.datetime.utcfromtimestamp(s).strftime('%m-%d %H:%M:%S')
    except Exception:
        return str(s)

def check_db(path):
    print(f"\n===== {os.path.basename(path)} =====")
    con = sqlite3.connect(path)
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if 'Executors' not in tables:
        print("  (sin tabla Executors)")
        con.close(); return False
    rows = list(con.execute(
        "SELECT id, timestamp, net_pnl_quote, cum_fees_quote, filled_amount_quote, is_active, is_trading, close_type "
        "FROM Executors ORDER BY timestamp DESC"))
    if not rows:
        print("  Sin executors registrados aun.")
        con.close(); return False
    active = [r for r in rows if r[5] or r[6]]
    filled = [r for r in rows if (r[4] or 0) != 0 or (r[3] or 0) != 0]
    print(f"  Executors totales: {len(rows)} | con fill/PnL: {len(filled)} | activos/en trading: {len(active)}")
    for r in filled[-3:]:
        print(f"    {dt(r[1])} pnl={r[2]} fees={r[3]} filled_quote={r[4]} active={r[5]} trading={r[6]} close={r[7]}")
    if 'TradeFill' in tables:
        n = con.execute("SELECT COUNT(*) FROM TradeFill").fetchone()[0]
        print(f"  TradeFills: {n}")
    con.close()
    return len(active) > 0

def main():
    print("PRE-STOP CHECK — no ejecutar `stop` si hay POSICION ABIERTA")
    dbs = sorted(glob.glob(os.path.join(BASE, "conf_v6*.sqlite")))
    if not dbs:
        print("  No encontre DBs conf_v6*.sqlite en data/")
        return
    open_position = False
    for db in dbs:
        if check_db(db):
            open_position = True
    print("\n" + "=" * 60)
    if open_position:
        print("RESULTADO: POSICION ABIERTA (algun executor en trading).")
        print("Un `stop` ahora la cerraria a MERCADO y puede costar $0.20-0.40+.")
        print("ACCION: NO PARAR. Consultar al asesor antes.")
    else:
        print("RESULTADO: sin posicion abierta. El `stop` solo cancela ordenes limite.")
        print("ACCION: via libre para parar (con aprobacion del asesor).")

if __name__ == "__main__":
    main()
