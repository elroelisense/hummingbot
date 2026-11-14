from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field

from hummingbot.core.data_type.common import MarketDict, PositionMode, PriceType, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction


class LadderLevel(BaseModel):
    level_id: str
    spread: Decimal          # relativo al mid: +0.01 = 1% arriba, -0.02 = 2% abajo
    amount: Decimal          # cantidad en base asset (PEPE)


class LadderConfig(ControllerConfigBase):
    """
    Escalera dinamica: coloca ordenes LIMIT_MAKER a spreads relativos al mid.
    Cada `refresh_interval` segundos cancela los niveles anteriores y los
    recrea al precio actual, adaptandose al mercado. Pensada para correr
    junto a otro controller (ej. PMM) en el mismo script v2_with_controllers.
    """
    controller_name: str = "ladder_b"
    controller_type: str = "generic"
    connector_name: str = "binance"
    trading_pair: str = "PEPE-USDT"
    side: TradeType = TradeType.SELL
    leverage: int = 1
    position_mode: PositionMode = PositionMode.HEDGE
    levels: List[LadderLevel] = Field(default_factory=list)
    refresh_interval: int = 300   # segundos entre re-colocaciones

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class LadderB(ControllerBase):
    def __init__(self, config: LadderConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._last_refresh_ts: float = 0.0

    async def update_processed_data(self):
        mid = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice
        )
        self.processed_data = {"mid_price": mid}

    def determine_executor_actions(self) -> List[ExecutorAction]:
        mid = self.processed_data.get("mid_price")
        if mid is None or mid <= 0:
            return []

        now = self.market_data_provider.time()
        if self._last_refresh_ts > 0 and (now - self._last_refresh_ts) < self.config.refresh_interval:
            return []

        actions: List[ExecutorAction] = []

        # 1. Cancelar niveles anteriores (solo los activos)
        level_ids = {lvl.level_id for lvl in self.config.levels}
        for ex in self.executors_info:
            lid = getattr(ex.config, "level_id", None)
            if lid in level_ids and getattr(ex, "is_active", False):
                actions.append(StopExecutorAction(
                    controller_id=self.config.id, executor_id=ex.id
                ))

        # 2. Colocar nuevos niveles al precio actual
        for lvl in self.config.levels:
            price = mid * (Decimal(1) + lvl.spread)
            price = self.market_data_provider.quantize_order_price(
                self.config.connector_name, self.config.trading_pair, price
            )
            amount = self.market_data_provider.quantize_order_amount(
                self.config.connector_name, self.config.trading_pair, lvl.amount
            )
            if amount <= 0 or price <= 0:
                self.logger().warning(
                    f"Ladder level {lvl.level_id}: amount/price quantized to 0, skipping."
                )
                continue
            cfg = OrderExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=self.config.side,
                amount=amount,
                price=price,
                execution_strategy=ExecutionStrategy.LIMIT_MAKER,
                leverage=self.config.leverage,
                level_id=lvl.level_id,
            )
            actions.append(CreateExecutorAction(controller_id=self.config.id, executor_config=cfg))
            self.logger().info(
                f"Ladder {self.config.side.name} placing {amount} {self.config.trading_pair} "
                f"@ {price} (spread {lvl.spread:+.2%}, level {lvl.level_id})"
            )

        self._last_refresh_ts = now
        return actions

    def to_format_status(self) -> List[str]:
        lines = ["Ladder (dinamica, refresh cada %ds):" % self.config.refresh_interval]
        placed = {getattr(ex.config, "level_id", None) for ex in self.executors_info if getattr(ex, "is_active", False)}
        mid = self.processed_data.get("mid_price") if self.processed_data else None
        for lvl in self.config.levels:
            state = "COLOCADA" if lvl.level_id in placed else "pendiente"
            if mid:
                price = mid * (Decimal(1) + lvl.spread)
                lines.append(f"  {lvl.level_id}: {lvl.amount} @ ~{price:.8f} (spread {lvl.spread:+.2%}) -> {state}")
            else:
                lines.append(f"  {lvl.level_id}: {lvl.amount} (spread {lvl.spread:+.2%}) -> {state}")
        return lines