"""Post-trade execution swap helpers."""
import datetime
import logging

from tradeexecutor.state.blockhain_transaction import BlockchainTransaction, BlockchainTransactionType
from tradeexecutor.state.state import State
from tradeexecutor.state.trade import TradeExecution


logger = logging.getLogger(__name__)

class TradeExecutionFailed(Exception):
    """Our Uniswap trade reverted"""


def is_swap_function(name: str):
    return name in {"swapExactTokensForTokens", "exactInput", "multicall"}


def get_swap_transactions(trade: TradeExecution) -> BlockchainTransaction:
    """Get the swap transaction from multiple transactions associated with the trade"""

    for tx in trade.blockchain_transactions:
        if tx.type == BlockchainTransactionType.hot_wallet:
            if is_swap_function(tx.function_selector):
                return tx
        elif tx.type == BlockchainTransactionType.enzyme_vault:
            if is_swap_function(tx.details["function"]):
                return tx

    raise RuntimeError("Should not happen")


def report_failure(
    ts: datetime.datetime,
    state: State,
    trade: TradeExecution,
    stop_on_execution_failure: bool,
) -> None:
    """What to do if trade fails.

    :param ts:
        Wall clock time

    :param state:
        The strategy state

    :param trade:
        Which trade had reverted transactions

    :param stop_on_execution_failure:
        If set, abort with exceptionm instead of trying to keep going.
    """

    logger.error(
        "Trade %s failed and freezing the position: %s",
        trade,
        trade.get_revert_reason(),
    )

    state.mark_trade_failed(
        ts,
        trade,
    )

    if stop_on_execution_failure:
        success_txs = []
        for tx in trade.blockchain_transactions:
            if not tx.is_success():
                raise TradeExecutionFailed(f"Could not execute a trade: {trade}.\n"
                                           f"Transaction failed: {tx}\n"
                                           f"Other succeeded transactions: {success_txs}\n"
                                           f"Stack trace:{tx.stack_trace}")
            else:
                success_txs.append(tx)

