"""Lendindg protocol leveraged.

- Various helpers related to lending protocol leverage
"""

import datetime
from _decimal import Decimal
from typing import TypeAlias, Tuple, Literal

from tradeexecutor.state.identifier import (
    AssetIdentifier, AssetWithTrackedValue, TradingPairIdentifier, 
    TradingPairKind, AssetType,
)
from tradeexecutor.state.interest import Interest
from tradeexecutor.state.loan import Loan, LiquidationRisked
from tradeexecutor.state.trade import TradeExecution
from tradeexecutor.state.types import USDollarAmount, LeverageMultiplier
from tradeexecutor.utils.accuracy import COLLATERAL_EPSILON, CLOSE_POSITION_COLLATERAL_EPSILON


def create_credit_supply_loan(
    position: "tradeexecutor.state.position.TradingPosition",
    trade: TradeExecution,
    timestamp: datetime.datetime,
):
    """Create a loan that supplies credit to a lending protocol.

    This is a loan with

    - Collateral only

    - Borrowed is ``None``
    """

    assert trade.is_credit_supply()
    assert not position.loan

    pair = position.pair
    assert pair.is_credit_supply()

    # aToken

    #
    # The expected collateral
    # is our collateral allocation (reserve)
    # and whatever more collateral we get for selling the shorted token
    #

    collateral = AssetWithTrackedValue(
        asset=pair.base,  # aUSDC token is the base pair for credit supply positions
        last_usd_price=trade.reserve_currency_exchange_rate,
        last_pricing_at=datetime.datetime.utcnow(),
        quantity=trade.planned_reserve,
    )

    loan = Loan(
        pair=trade.pair,
        collateral=collateral,
        collateral_interest=Interest.open_new(trade.planned_reserve, timestamp),
        borrowed=None,
        borrowed_interest=None,
    )

    # Sanity check
    loan.check_health()

    return loan


def update_credit_supply_loan(
    position: "tradeexecutor.state.position.TradingPosition",
    trade: TradeExecution,
    timestamp: datetime.datetime,
):
    """Close/increase/reduce credit supply loan.

    """

    assert trade.is_credit_supply()

    pair = position.pair
    assert pair.is_credit_supply()

    loan = position.loan
    assert loan

    loan.collateral.change_quantity_and_value(
        trade.planned_quantity,
        trade.reserve_currency_exchange_rate,
        trade.opened_at,
        allow_negative=True,
    )

    # Sanity check
    loan.check_health()

    return loan


def create_short_loan(
    position: "tradeexecutor.state.position.TradingPosition",
    trade: TradeExecution,
    timestamp: datetime.datetime,
    mode: Literal["plan", "execute"] = "plan",
) -> Loan:
    """Create the loan data tracking for short position.

    - Check that the information looks correct for a short position.

    - Populates :py:class:`Loan` data structure.

    - We use assumed prices. The actual execution prices may differ
      and must be populated to `trade.executed_loan`.
    """

    assert trade.is_short()
    assert len(position.trades) == 1, "Can be only called when position is opening"

    assert not position.loan, f"loan already set"

    pair = trade.pair

    assert pair.base.underlying, "Base token lacks underlying asset"
    assert pair.quote.underlying, "Quote token lacks underlying asset"

    assert pair.base.type == AssetType.borrowed, f"Trading pair base asset is not borrowed: {pair.base}, {pair.base.type}"
    assert pair.quote.type == AssetType.collateral, f"Trading pair quote asset is not collateral: {pair.quote}, {pair.quote.type}"

    assert pair.quote.underlying.is_stablecoin(), f"Only stablecoin collateral supported for shorts: {pair.quote}"

    if mode == "plan":
        # Extra checks when position is opened
        assert trade.planned_quantity < 0, f"Short position must open with a sell with negative quantity, got: {trade.planned_quantity}"

        if not trade.planned_collateral_allocation:
            assert trade.planned_reserve > 0, f"Collateral must be positive: {trade.planned_reserve}"
    
        borrowed_quantity = abs(trade.planned_quantity)
        collateral_quantity = trade.planned_reserve + trade.planned_collateral_allocation + trade.planned_collateral_consumption
    else:
        # Extra checks when position is closed
        assert trade.executed_quantity < 0, f"Short position open with a sell with negative quantity, got: {trade.executed_quantity}"

        if not trade.executed_collateral_allocation:
            assert trade.executed_reserve > 0, f"Collateral must be positive: {trade.executed_reserve}"

        borrowed_quantity = abs(trade.executed_quantity)
        collateral_quantity = trade.executed_reserve + trade.executed_collateral_allocation + trade.executed_collateral_consumption

    # vToken
    borrowed = AssetWithTrackedValue(
        asset=pair.base,
        last_usd_price=trade.planned_price,
        last_pricing_at=datetime.datetime.utcnow(),
        quantity=borrowed_quantity,
        created_strategy_cycle_at=trade.strategy_cycle_at,
    )

    # aToken
    #
    # The expected collateral
    # is our collateral allocation (reserve)
    # and whatever more collateral we get for selling the shorted token
    #

    collateral = AssetWithTrackedValue(
        asset=pair.quote,
        last_usd_price=trade.reserve_currency_exchange_rate,
        last_pricing_at=datetime.datetime.utcnow(),
        quantity=collateral_quantity,
    )

    loan = Loan(
        pair=trade.pair,
        collateral=collateral,
        borrowed=borrowed,
        collateral_interest=Interest.open_new(collateral.quantity, timestamp),
        borrowed_interest=Interest.open_new(borrowed.quantity, timestamp),
    )

    # Sanity check
    loan.check_health()

    return loan


def update_short_loan(
    loan: Loan,
    position: "tradeexecutor.state.position.TradingPosition",
    trade: TradeExecution,
    mode: Literal["plan", "execute"] = "plan",
    close_position=False,
):
    """Update the loan data tracking for short position.

    - Check that the information looks correct for a short position.

    :param loan:
        Loan which is about to change.

        Clone the existing loan, will be mutated in place.

    :param position:
        Associated trading position

    :param trade:
        The trade that is changing this loan

    :param close_position:
        Is this loan update for a position close.

        For closing position, we hack a special tolerance for the collateral epsilon.

        This is due to slippage collateral spilling to the next position
        with the same collateral in Aave.
    """
    assert trade.is_short()
    assert len(position.trades) > 1, "Can be only called when closing/reducing/increasing/position"

    # TODO: How planned_collateral_consumption + planned_collateral_allocation
    # might not be the best way to do this, see test_short_decrease_size

    if mode == "plan":
        collateral_consumption = trade.planned_collateral_consumption or Decimal(0)
        collateral_allocation = trade.planned_collateral_allocation or Decimal(0)
        reserve_adjust = trade.planned_reserve or Decimal(0)

        borrow_change = -trade.planned_quantity
    else:
        collateral_consumption = trade.executed_collateral_consumption or Decimal(0)
        collateral_allocation = trade.executed_collateral_allocation or Decimal(0)
        reserve_adjust = trade.executed_reserve or Decimal(0)

        borrow_change = -trade.executed_quantity

    collateral_change = collateral_consumption + collateral_allocation + reserve_adjust
    borrow_change = -trade.planned_quantity

    available_collateral_interest = loan.collateral_interest.get_remaining_interest()

    loan.collateral.change_quantity_and_value(
        collateral_change,
        trade.reserve_currency_exchange_rate,
        trade.opened_at,
        available_accrued_interest=available_collateral_interest,
        epsilon=CLOSE_POSITION_COLLATERAL_EPSILON if close_position else COLLATERAL_EPSILON,
        close_position=close_position,
    )

    # In short position, positive value reduces the borrowed amount
    loan.borrowed.change_quantity_and_value(
        borrow_change,
        trade.planned_price,
        trade.opened_at,
        # Because of interest events, and the fact that we need
        # to pay the interest back on closing the loan,
        # the tracked underlying amount can go negative when closing a short
        # position
        allow_negative=True,
    )

    # Interest object has the cached last_token_amount decimal
    # which we also need to fxi
    loan.borrowed_interest.adjust(borrow_change)
    loan.collateral_interest.adjust(collateral_change, epsilon=abs(CLOSE_POSITION_COLLATERAL_EPSILON * collateral_change) if close_position else COLLATERAL_EPSILON)

    # Sanity check
    if loan.borrowed.quantity > 0:
        try:
            loan.check_health()
        except LiquidationRisked as e:
            raise LiquidationRisked(f"If the planned loan leveraged trade for {position} would go through, the position would be immediately liquidated") from e

    return loan
