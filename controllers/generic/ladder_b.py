from decimal import Decimal
from typing import List

from pydantic import BaseModel, Field

from hummingbot.core.data_type.common import MarketDict, PositionMode, PriceType, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction


class LadderLevel(BaseModel):
    level_id: str
    price: Decimal
    amount: Decimal  # base asset amount (PEPE)


class LadderConfig(ControllerConfigBase):
    """
    Escalera B: coloca una vez una serie de ordenes LIMIT_MAKER de venta
    (GTC) sobre un tramo apartado del inventario. Cada nivel se coloca
    una sola vez por proceso: si el executor existe (activo o terminado),
    el nivel nunca se recrea. Pensada para correr junto a otro controller
    (ej. PMM) en el mismo script v2_with_controllers.
    """
    controller_name: str = "ladder_b"
    controller_type: str = "generic"
    connector_name: str = "binance"
    trading_pair: str = "PEPE-USDT"
    side: TradeType = TradeType.SELL
    leverage: int = 1
    position_mode: PositionMode = PositionMode.HEDGE
    levels: List[LadderLevel] = Field(default_factory=list)

    def update_markets(self, markets: MarketDict) -> MarketDict:
        return markets.add_or_update(self.connector_name, self.trading_pair)


class LadderB(ControllerBase):
    def __init__(self, config: LadderConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._placed_level_ids = set()

    async def update_processed_data(self):
        self.processed_data = {
            "mid_price": self.market_data_provider.get_price_by_type(
                self.config.connector_name, self.config.trading_pair, PriceType.MidPrice
            )
        }

    def determine_executor_actions(self) -> List[ExecutorAction]:
        # Niveles ya colocados en este proceso (activos o terminados):
        # nunca se recrean, asi un fill jamas dispara una re-venta.
        seen = set(self._placed_level_ids)
        for ex in self.executors_info:
            lid = getattr(ex.config, "level_id", None)
            if lid:
                seen.add(lid)

        actions: List[ExecutorAction] = []
        for lvl in self.config.levels:
            if lvl.level_id in seen:
                continue
            amount = self.market_data_provider.quantize_order_amount(
                self.config.connector_name, self.config.trading_pair, lvl.amount
            )
            if amount <= 0:
                self.logger().warning(f"Ladder level {lvl.level_id}: amount quantized to 0, skipping.")
                continue
            cfg = OrderExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair,
                side=self.config.side,
                amount=amount,
                price=lvl.price,
                execution_strategy=ExecutionStrategy.LIMIT_MAKER,
                leverage=self.config.leverage,
                level_id=lvl.level_id,
            )
            actions.append(CreateExecutorAction(controller_id=self.config.id, executor_config=cfg))
            self._placed_level_ids.add(lvl.level_id)
            self.logger().info(
                f"Ladder B placing {self.config.side.name} {amount} {self.config.trading_pair} "
                f"@ {lvl.price} (level {lvl.level_id})"
            )
        return actions

    def to_format_status(self) -> List[str]:
        lines = ["Ladder B (escalera de salidas):"]
        placed = {getattr(ex.config, "level_id", None) for ex in self.executors_info}
        for lvl in self.config.levels:
            state = "COLOCADA" if lvl.level_id in placed else "pendiente"
            lines.append(f"  {lvl.level_id}: {lvl.amount} @ {lvl.price} -> {state}")
        return lines
