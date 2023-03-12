"""Maanually repair broken states.

- Recover from failed trades

Trade failure modes may include

- Broadcasted but not confirmed

- Broadcasted, confirmed, but not marked as executed

- Executed, failed

"""
import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal
from itertools import chain
from typing import List, TypedDict

from tradeexecutor.state.portfolio import Portfolio
from tradeexecutor.state.position import TradingPosition
from tradeexecutor.state.state import State
from tradeexecutor.state.trade import TradeExecution, TradeType, TradeStatus

logger = logging.getLogger(__name__)


class RepairAborted(Exception):
    """User chose no"""


@dataclass(slots=True)
class RepairResult:
    """The report of the repair results.

    Note that repair might not have done anything - every list is empty.
    """

    #: How many frozen positions we encountered
    frozen_positions: List[TradingPosition]

    #: What positions we managed to unfreeze
    unfrozen_positions: List[TradingPosition]

    #: How many individual trades we repaired
    trades_needing_repair: List[TradeExecution]

    #: New trades we made to fix the accounting
    new_trades: List[TradeExecution]


def make_counter_trade(portfolio: Portfolio, p: TradingPosition, t: TradeExecution) -> TradeExecution:
    """Make a virtual trade that fixes the total balances of a position and unwinds the broken trade."""

    # Note: we do not negate the values of the original trade,
    # because get_quantity() and others will return 0 to repaired spot trades for now.
    # This behavior may change in the future for more complex trades.
    position, counter_trade, created = portfolio.create_trade(
        strategy_cycle_at=t.strategy_cycle_at,
        pair=t.pair,
        quantity=-t.planned_quantity,
        assumed_price=t.planned_price,
        trade_type=TradeType.repair,
        reserve_currency=t.reserve_currency,
        planned_mid_price=t.planned_mid_price,
        price_structure=t.price_structure,
        reserve=None,
        reserve_currency_price=t.get_reserve_currency_exchange_rate(),
        position=p,
    )
    assert created is False
    assert position == p

    counter_trade.mark_success(
        datetime.datetime.utcnow(),
        t.planned_price,
        Decimal(0),
        Decimal(0),
        0,
        t.native_token_price,
        force=True,
    )
    assert counter_trade.is_success()
    assert counter_trade.get_value() == 0
    assert counter_trade.get_position_quantity() == 0
    assert counter_trade.trade_type == TradeType.repair
    return counter_trade


def repair_trade(portfolio: Portfolio, t: TradeExecution) -> TradeExecution:
    """Repair a trade.

    - Make a counter trade for bookkeeping

    - Set the original trade to repaired state (instead of failed state)
    """
    p = portfolio.get_position_by_id(t.position_id)
    c = make_counter_trade(portfolio, p, t)
    t.repaired_at = datetime.datetime.utcnow()
    c.repaired_trade_id = t.trade_id
    assert t.get_status() == TradeStatus.repaired
    assert t.get_value() == 0
    assert t.get_position_quantity() == 0
    assert t.planned_quantity != 0
    return c


def find_trades_to_be_repaired(state: State) -> List[TradeExecution]:
    trades_to_be_repaired = []
    # Closed trades do not need attention
    for p in chain(state.portfolio.open_positions.values(), state.portfolio.frozen_positions.values()):
        t: TradeExecution
        for t in p.trades.values():
            if t.is_repair_needed():
                logger.info("Found a trade needing repair: %s", t)
                trades_to_be_repaired.append(t)

    return trades_to_be_repaired


def reconfirm_trade(reconfirming_needed_trades: List[TradeExecution]):

    raise NotImplementedError("Unfinished")

    for t in reconfirming_needed_trades:
        assert t.get_status() == TradeStatus.broadcasted

        receipt_data = wait_trades_to_complete(
            self.web3,
            [t],
            max_timeout=self.confirmation_timeout,
            confirmation_block_count=self.confirmation_block_count,
        )

        assert len(receipt_data) > 0, f"Got bad receipts: {receipt_data}"

        tx_data = {tx.tx_hash: (t, tx) for tx in t.blockchain_transactions}

        self.resolve_trades(
            datetime.datetime.now(),
            state,
            tx_data,
            receipt_data,
            stop_on_execution_failure=True)

        t.repaired_at = datetime.datetime.utcnow()
        if not t.notes:
            # Add human readable note,
            # but don't override any other notes
            t.notes = "Failed broadcast repaired"

        repaired.append(t)


def unfreeze_position(position: TradingPosition) -> bool:
    """Attempt to unfreeze positions.

    - All failed trades on a position must be cleared

    :return:
        if we managed to unfreeze the position
    """
    pass


def repair_trades(
        state: State,
        attempt_repair=True,
        interactive=True) -> RepairResult:
    """Repair trade.

    - Find frozen positions and trades in them

    - Mark trades invalidated

    - Make the necessary counter trades to fix the total balances

    - Does not actually broadcast any transactions - only fixes the internal accounting

    :param attempt_repair:
        If not set, only list broken trades and frozen positions.

        Do not attempt to repair them.

    :param interactive:
        Command line interactive user experience.

        Allows press `n` for abort.

    :raise RepairAborted:
        User chose no
    """

    logger.info("Repairing trades")

    frozen_positions = list(state.portfolio.frozen_positions.values())

    logger.info("Strategy has %d frozen positions", len(frozen_positions))

    trades_to_be_repaired = find_trades_to_be_repaired(state)

    logger.info("Found %d trades to be repaired", len(trades_to_be_repaired))

    if len(trades_to_be_repaired) == 0 or not attempt_repair:
        return RepairResult(
            frozen_positions,
            [],
            trades_to_be_repaired,
            [],
        )

    if interactive:

        for t in trades_to_be_repaired:
            print("Needs repair:", t)

        confirmation = input("Attempt to repair [y/n]").lower()
        if confirmation != "y":
            raise RepairAborted()

    new_trades = []
    for t in trades_to_be_repaired:
        new_trades.append(repair_trade(state.portfolio, t))

    unfrozen_positions = []
    for p in frozen_positions:
        if unfreeze_position(p):
            unfrozen_positions.append(p)
            logger.info("Position unfrozen: %s", p)

    for t in new_trades:
        logger.info("Correction trade made: %s", t)

    return RepairResult(
        frozen_positions,
        unfrozen_positions,
        trades_to_be_repaired,
        new_trades,
    )

