import glob
import os
import sqlite3
from typing import List

import requests
from pydantic import Field

from hummingbot.core.data_type.common import MarketDict
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

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets


class TelegramNotifier(ControllerBase):
    def __init__(self, config: TelegramNotifierConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._last_check = 0
        self._seen = set()
        self._started = False
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

    async def update_processed_data(self):
        pass

    def determine_executor_actions(self) -> List[ExecutorAction]:
        now = self.market_data_provider.time()
        if not self._started:
            self._started = True
            self._send("🤖 <b>Notificador integrado activo</b> (dentro de Hummingbot). Te avisaré cada fill.")
        if now - self._last_check < self.config.check_interval_seconds:
            return []
        self._last_check = now
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
