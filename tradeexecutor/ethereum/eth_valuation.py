"""Value model based on Uniswap v3 market price.

Value positions based on their "dump" price on Uniswap,
assuming we get the worst possible single trade execution.
"""
import datetime
from typing import Tuple

from tradeexecutor.ethereum.eth_pricing_model import EthereumPricingModel
from tradeexecutor.state.position import TradingPosition
from tradeexecutor.state.types import USDollarAmount
from tradeexecutor.state.valuation import ValuationUpdate
from tradeexecutor.strategy.valuation import ValuationModel


class EthereumPoolRevaluator(ValuationModel):
    """Re-value spot assets based on their on-chain price.

    Does directly JSON-RPC call to get the latest price in the Uniswap pools.

    Only uses direct route - mostly useful for testing, may not give a realistic price in real
    world with multiple order routing options.

    .. warning ::

        This valuation metohd always uses the latest price. It
        cannot be used for backtesting.
    """

    def __init__(self, pricing_model: EthereumPricingModel):
        self.pricing_model = pricing_model
        

    def __call__(self,
                 ts: datetime.datetime,
                 position: TradingPosition) -> ValuationUpdate:
        """

        :param ts:
            When to revalue. Used in backesting. Live strategies may ignore.
        :param position:
            Open position
        :return:
            (revaluation date, price) tuple.
            Note that revaluation date may differ from the wantead timestamp if
            there is no data available.

        """
        assert isinstance(ts, datetime.datetime)
        pair = position.pair

        assert position.is_long(), "Short not supported"

        quantity = position.get_quantity()
        # Cannot do pricing for zero quantity
        assert quantity > 0, f"Trying to value position with zero quantity: {position}, {ts}, {self.__class__.__name__}"

        old_price = position.last_token_price
        price_structure = self.pricing_model.get_sell_price(ts, pair, quantity)

        new_price = price_structure.price

        old_value = position.get_value()
        new_value = position.revalue_base_asset(ts, float(new_price))

        evt = ValuationUpdate(
            created_at=ts,
            position_id=position.position_id,
            valued_at=ts,
            old_value=old_value,
            new_value=new_value,
            old_price=old_price,
            new_price=new_price,
        )

        position.valuation_updates.append(evt)

        return evt
