import datetime
from decimal import Decimal
from typing import Tuple

from tradeexecutor.state.state import State, TradingPairIdentifier, TradeType, TradeExecution
from tradingstrategy.analysis.tradeanalyzer import TradePosition


class TestTrader:
    """Helper class to generate trades for tests.

    A helper class that simulates trades with 99% slippage.
    Execution is always worse than wished price.
    """

    def __init__(self, state: State):
        self.state = state
        self.nonce = 1
        self.ts = datetime.datetime(2022, 1, 1, tzinfo=datetime.timezone.utc)

        self.lp_fees = 2.50  # $2.5
        self.gas_units_consumed = 150_000  # 150k gas units per swap
        self.gas_price = 15 * 10**9  # 15 Gwei/gas unit

        self.native_token_price = 1

    def create_and_execute(self, pair: TradingPairIdentifier, quantity: Decimal, price: float, price_impact=0.99) -> Tuple[TradePosition, TradeExecution]:

        # 1. Plan
        position, trade = self.state.create_trade(
            ts=self.ts,
            pair=pair,
            quantity=quantity,
            assumed_price=price,
            trade_type=TradeType.rebalance,
            reserve_currency=pair.quote,
            reserve_currency_price=1.0)

        self.ts += datetime.timedelta(seconds=1)

        # 2. Capital allocation
        txid = hex(self.nonce)
        nonce = self.nonce
        self.state.start_execution(self.ts, trade, txid, nonce)

        # 3. broadcast
        self.nonce += 1
        self.ts += datetime.timedelta(seconds=1)

        self.state.mark_broadcasted(self.ts, trade)
        self.ts += datetime.timedelta(seconds=1)

        # 4. executed
        executed_price = price * price_impact
        if trade.is_buy():
            executed_quantity = quantity * Decimal(price_impact)
            executed_reserve = Decimal(0)
        else:
            executed_quantity = quantity
            executed_reserve = abs(quantity * Decimal(executed_price))

        self.state.mark_trade_success(self.ts, trade, executed_price, executed_quantity, executed_reserve, self.lp_fees, self.gas_price, self.gas_units_consumed, self.native_token_price)
        return position, trade

    def buy(self, pair, quantity, price) -> Tuple[TradePosition, TradeExecution]:
        return self.create_and_execute(pair, quantity, price)

    def sell(self, pair, quantity, price) -> Tuple[TradePosition, TradeExecution]:
        return self.create_and_execute(pair, -quantity, price)


