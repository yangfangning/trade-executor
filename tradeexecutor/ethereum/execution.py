"""Dealing with Ethereum low level tranasctions."""

import logging
import datetime
from collections import Counter
from decimal import Decimal
from typing import List, Dict, Set, Tuple
from abc import ABC, abstractmethod

from eth_account.datastructures import SignedTransaction
from eth_typing import HexAddress
from hexbytes import HexBytes
from web3 import Web3
from web3.contract import Contract

from eth_defi.abi import get_deployed_contract
from eth_defi.gas import GasPriceSuggestion, apply_gas, estimate_gas_fees
from eth_defi.hotwallet import HotWallet
from eth_defi.revert_reason import fetch_transaction_revert_reason
from eth_defi.token import fetch_erc20_details, TokenDetails
from eth_defi.confirmation import wait_transactions_to_complete, \
    broadcast_and_wait_transactions_to_complete, broadcast_transactions
from eth_defi.uniswap_v2.deployment import UniswapV2Deployment, FOREVER_DEADLINE,  mock_partial_deployment_for_analysis
from eth_defi.uniswap_v2.fees import estimate_sell_price_decimals
from eth_defi.uniswap_v2.analysis import analyse_trade_by_hash, TradeSuccess, analyse_trade_by_receipt
from tradeexecutor.state.state import State
from tradeexecutor.state.trade import TradeExecution
from tradeexecutor.state.blockhain_transaction import BlockchainTransaction
from tradeexecutor.state.identifier import AssetIdentifier, TradingPairIdentifier

logger = logging.getLogger(__name__)


class TradeExecutionFailed(Exception):
    """Our Uniswap trade reverted"""

class ExecutionModel(ABC):
    """Run order execution on a single Uniswap v2 style exchanges."""

    def __init__(self,
                 web3: Web3,
                 hot_wallet: HotWallet,
                 min_balance_threshold=Decimal("0.5"),
                 confirmation_block_count=6,
                 confirmation_timeout=datetime.timedelta(minutes=5),
                 max_slippage: float = 0.01,
                 stop_on_execution_failure=True,
                 swap_gas_fee_limit=2_000_000):
        """
        :param web3:
            Web3 connection used for this instance

        :param hot_wallet:
            Hot wallet instance used for this execution

        :param min_balance_threshold:
            Abort execution if our hot wallet gas fee balance drops below this

        :param confirmation_block_count:
            How many blocks to wait for the receipt confirmations to mitigate unstable chain tip issues

        :param confirmation_timeout:
            How long we wait transactions to clear

        :param stop_on_execution_failure:
            Raise an exception if any of the trades fail top execute

        :param max_slippage:
            Max slippage tolerance per trade. 0.01 is 1%.
        """
        assert isinstance(confirmation_timeout, datetime.timedelta), f"Got {confirmation_timeout} {confirmation_timeout.__class__}"
        self.web3 = web3
        self.hot_wallet = hot_wallet
        self.stop_on_execution_failure = stop_on_execution_failure
        self.min_balance_threshold = min_balance_threshold
        self.confirmation_block_count = confirmation_block_count
        self.confirmation_timeout = confirmation_timeout
        self.swap_gas_fee_limit = swap_gas_fee_limit
        self.max_slippage = max_slippage

    @property
    def chain_id(self) -> int:
        """Which chain the live execution is connected to."""
        return self.web3.eth.chain_id

    def is_live_trading(self) -> bool:
        return True

    def is_stop_loss_supported(self) -> bool:
        # TODO: fix this when we want to use stop loss in real strategy
        return False

    def preflight_check(self):
        """Check that we can connect to the web3 node"""

        # Check JSON-RPC works
        assert self.web3.eth.block_number > 1

        # Check we have money for gas fees
        if self.min_balance_threshold > 0:
            balance = self.hot_wallet.get_native_currency_balance(self.web3)
            assert balance > self.min_balance_threshold, f"At least {self.min_balance_threshold} native currency need, our wallet {self.hot_wallet.address} has {balance:.8f}"

    def initialize(self):
        """Set up the wallet"""
        logger.info("Initialising Uniswap v2 execution model")
        self.hot_wallet.sync_nonce(self.web3)
        balance = self.hot_wallet.get_native_currency_balance(self.web3)
        logger.info("Our hot wallet is %s with nonce %d and balance %s", self.hot_wallet.address, self.hot_wallet.current_nonce, balance)

    def execute_trades(self,
                       ts: datetime.datetime,
                       state: State,
                       trades: List[TradeExecution],
                       routing_model: UniswapV2SimpleRoutingModel | UniswapV3SimpleRoutingModel,
                       routing_state: UniswapV2RoutingState | UniswapV3RoutingState,
                       check_balances=False):
        """Execute the trades determined by the algo on a designed Uniswap v2 instance.

        :return: Tuple List of succeeded trades, List of failed trades
        """
        state.start_trades(datetime.datetime.utcnow(), trades, max_slippage=self.max_slippage)

        # 61 is Ethereum Tester
        if self.web3.eth.chain_id != 61:
            assert self.confirmation_block_count > 0, f"confirmation_block_count set to {self.confirmation_block_count} "

        routing_model.setup_trades(
            routing_state,
            trades,
            check_balances=check_balances)

        broadcast_and_resolve(
            self.web3,
            state,
            trades,
            confirmation_timeout=self.confirmation_timeout,
            confirmation_block_count=self.confirmation_block_count,
        )

        # Clean up failed trades
        freeze_position_on_failed_trade(ts, state, trades)

    
    def get_routing_state_details(self) -> dict:
        return {
            "web3": self.web3,
            "hot_wallet": self.hot_wallet,
        }

    @abstractmethod
    def repair_unconfirmed_trades(self, state: State, resolve_trades: callable) -> List[TradeExecution]:
        """Repair unconfirmed trades.

        Repair trades that failed to properly broadcast or confirm due to
        blockchain node issues.
        """

        repaired = []

        logger.info("Reparing the failed trade confirmation")

        assert self.confirmation_timeout > datetime.timedelta(0), \
            "Make sure you have a good tx confirmation timeout setting before attempting a repair"

        # Check if we are on a live chain, not Ethereum Tester
        if self.web3.eth.chain_id != 61:
            assert self.confirmation_block_count > 0, \
                "Make sure you have a good confirmation_block_count setting before attempting a repair"

        for p in state.portfolio.open_positions.values():
            t: TradeExecution
            for t in p.trades.values():
                if t.is_unfinished():
                    logger.info("Found unconfirmed trade: %s", t)

                    assert t.get_status() == TradeStatus.broadcasted

                    receipt_data = wait_trades_to_complete(
                        self.web3,
                        [t],
                        max_timeout=self.confirmation_timeout,
                        confirmation_block_count=self.confirmation_block_count,
                    )

                    assert len(receipt_data) > 0, f"Got bad receipts: {receipt_data}"

                    tx_data = {}

                    # Build a tx hash -> (trade, tx) map
                    for tx in t.blockchain_transactions:
                        tx_data[tx.tx_hash] = (t, tx)

                    resolve_trades(
                        self.web3,
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

        return repaired

    @staticmethod
    def pre_execute_assertions(
        ts: datetime.datetime, 
        routing_model: 
            UniswapV2SimpleRoutingModel |
            UniswapV3SimpleRoutingModel,
        routing_state: 
            UniswapV2RoutingState |
            UniswapV3RoutingSate
    ):
        assert isinstance(ts, datetime.datetime)

        if isinstance(routing_model, UniswapV2SimpleRoutingModel):
            assert isinstance(routing_state, UniswapV2RoutingState), "Incorrect routing_state specified"
        elif isinstance(routing_model, UniswapV3SimpleRoutingModel):
            assert isinstance(routing_state, UniswapV3RoutingState), "Incorrect routing_state specified"
        else:
            raise ValueError("Incorrect routing model specified")


def translate_to_naive_swap(
        web3: Web3,
        deployment: UniswapV2Deployment,
        hot_wallet: HotWallet,
        t: TradeExecution,
        gas_fees: GasPriceSuggestion,
        base_token_details: TokenDetails,
        quote_token_details: TokenDetails,
    ):
    """Creates an AMM swap tranasction out of buy/sell.

    If buy tries to do the best execution for given `planned_reserve`.

    If sell tries to do the best execution for given `planned_quantity`.

    Route only between two pools - stablecoin reserve and target buy/sell.

    Any gas price is set by `web3` instance gas price strategy.

    :param t:
    :return: Unsigned transaction
    """

    if t.is_buy():
        amount0_in = int(t.planned_reserve * 10**quote_token_details.decimals)
        path = [quote_token_details.address, base_token_details.address]
        t.reserve_currency_allocated = t.planned_reserve
    else:
        # Reverse swap
        amount0_in = int(-t.planned_quantity * 10**base_token_details.decimals)
        path = [base_token_details.address, quote_token_details.address]
        t.reserve_currency_allocated = 0

    args = [
        amount0_in,
        0,
        path,
        hot_wallet.address,
        FOREVER_DEADLINE,
    ]

    # https://docs.uniswap.org/protocol/V2/reference/smart-contracts/router-02#swapexacttokensfortokens
    # https://web3py.readthedocs.io/en/stable/web3.eth.account.html#sign-a-contract-transaction
    tx = deployment.router.functions.swapExactTokensForTokens(
        *args,
    ).build_transaction({
        'chainId': web3.eth.chain_id,
        'gas': 350_000,  # Estimate max 350k gas per swap
        'from': hot_wallet.address,
    })

    apply_gas(tx, gas_fees)

    signed = hot_wallet.sign_transaction_with_new_nonce(tx)
    selector = deployment.router.functions.swapExactTokensForTokens

    # Create record of this transaction
    tx_info = t.tx_info = BlockchainTransaction()
    tx_info.set_target_information(
        web3.eth.chain_id,
        deployment.router.address,
        selector.fn_name,
        args,
        tx,
    )

    tx_info.set_broadcast_information(tx["nonce"], signed.hash.hex(), signed.rawTransaction.hex())


def prepare_swaps(
        web3: Web3,
        hot_wallet: HotWallet,
        uniswap: UniswapV2Deployment,
        ts: datetime.datetime,
        state: State,
        instructions: List[TradeExecution],
        underflow_check=True) -> Dict[HexAddress, int]:
    """Prepare multiple swaps to be breoadcasted parallel from the hot wallet.

    :param underflow_check: Do we check we have enough cash in hand before trying to prepare trades.
        Note that because when executing sell orders first, we will have more cash in hand to make buys.

    :return: Token approvals we need to execute the trades
    """

    # Get our starting nonce
    gas_fees = estimate_gas_fees(web3)

    for idx, t in enumerate(instructions):

        base_token_details = fetch_erc20_details(web3, t.pair.base.checksum_address)
        quote_token_details = fetch_erc20_details(web3, t.pair.quote.checksum_address)

        assert base_token_details.decimals is not None, f"Bad token at {t.pair.base.address}"
        assert quote_token_details.decimals is not None, f"Bad token at {t.pair.quote.address}"

        state.portfolio.check_for_nonce_reuse(hot_wallet.current_nonce)

        translate_to_naive_swap(
            web3,
            uniswap,
            hot_wallet,
            t,
            gas_fees,
            base_token_details,
            quote_token_details,
        )

        if t.is_buy():
            state.portfolio.move_capital_from_reserves_to_trade(t, underflow_check=underflow_check)

        t.started_at = datetime.datetime.utcnow()


def approve_tokens(
        web3: Web3,
        deployment: UniswapV2Deployment,
        hot_wallet: HotWallet,
        instructions: List[TradeExecution],
    ) -> List[SignedTransaction]:
    """Approve multiple ERC-20 token allowances for the trades needed.

    Each token is approved only once. E.g. if you have 4 trades using USDC,
    you will get 1 USDC approval.
    """

    signed = []

    approvals = Counter()

    for idx, t in enumerate(instructions):

        base_token_details = fetch_erc20_details(web3, t.pair.base.checksum_address)
        quote_token_details = fetch_erc20_details(web3, t.pair.quote.checksum_address)

        # Update approval counters for the whole batch
        if t.is_buy():
            approvals[quote_token_details.address] += int(t.planned_reserve * 10**quote_token_details.decimals)
        else:
            approvals[base_token_details.address] += int(-t.planned_quantity * 10**base_token_details.decimals)

    for idx, tpl in enumerate(approvals.items()):
        token_address, amount = tpl

        assert amount > 0, f"Got a non-positive approval {token_address}: {amount}"

        token = get_deployed_contract(web3, "IERC20.json", token_address)
        tx = token.functions.approve(
            deployment.router.address,
            amount,
        ).build_transaction({
            'chainId': web3.eth.chain_id,
            'gas': 100_000,  # Estimate max 100k per approval
            'from': hot_wallet.address,
        })
        signed.append(hot_wallet.sign_transaction_with_new_nonce(tx))

    return signed


def approve_infinity(
        web3: Web3,
        deployment: UniswapV2Deployment,
        hot_wallet: HotWallet,
        instructions: List[TradeExecution],
    ) -> List[SignedTransaction]:
    """Approve multiple ERC-20 token allowances for the trades needed.

    Each token is approved only once. E.g. if you have 4 trades using USDC,
    you will get 1 USDC approval.
    """

    signed = []

    approvals = Counter()

    for idx, t in enumerate(instructions):

        base_token_details = fetch_erc20_details(web3, t.pair.base.checksum_address)
        quote_token_details = fetch_erc20_details(web3, t.pair.quote.checksum_address)

        # Update approval counters for the whole batch
        if t.is_buy():
            approvals[quote_token_details.address] += int(t.planned_reserve * 10**quote_token_details.decimals)
        else:
            approvals[base_token_details.address] += int(-t.planned_quantity * 10**base_token_details.decimals)

    for idx, tpl in enumerate(approvals.items()):
        token_address, amount = tpl

        assert amount > 0, f"Got a non-positive approval {token_address}: {amount}"

        token = get_deployed_contract(web3, "IERC20.json", token_address)
        tx = token.functions.approve(
            deployment.router.address,
            amount,
        ).build_transaction({
            'chainId': web3.eth.chain_id,
            'gas': 100_000,  # Estimate max 100k per approval
            'from': hot_wallet.address,
        })
        signed.append(hot_wallet.sign_transaction_with_new_nonce(tx))

    return signed


def confirm_approvals(
        web3: Web3,
        txs: List[SignedTransaction],
        confirmation_block_count=0,
        max_timeout=datetime.timedelta(minutes=5),
    ):
    """Wait until all transactions are confirmed.

    :param confirmation_block_count: How many blocks to wait for the transaction to settle

    :raise: If any of the transactions fail
    """
    logger.info("Confirming %d approvals, confirmation_block_count is %d", len(txs), confirmation_block_count)
    receipts = broadcast_and_wait_transactions_to_complete(
        web3,
        txs,
        confirmation_block_count=confirmation_block_count,
        max_timeout=max_timeout)
    return receipts


def broadcast(
        web3: Web3,
        ts: datetime.datetime,
        instructions: List[TradeExecution],
        confirmation_block_count: int=0,
        ganache_sleep=0.5,
) -> Dict[HexBytes, Tuple[TradeExecution, BlockchainTransaction]]:
    """Broadcast multiple transations and manage the trade executor state for them.

    :return: Map of transaction hashes to watch
    """

    logger.info("Broadcasting %d trades", len(instructions))

    res = {}
    # Another nonce guard
    nonces: Set[int] = set()

    broadcast_batch: List[SignedTransaction] = []

    for t in instructions:
        assert len(t.blockchain_transactions) > 0, f"Trade {t} does not have any blockchain transactions prepared"
        for tx in t.blockchain_transactions:
            assert isinstance(tx.signed_bytes, str), f"Got signed transaction: {t.tx_info.signed_bytes}"
            assert tx.nonce not in nonces, "Nonce already used"
            nonces.add(tx.nonce)
            tx.broadcasted_at = ts
            res[tx.tx_hash] = (t, tx)
            # Only SignedTransaction.rawTransaction attribute is intresting in this point
            signed_tx = SignedTransaction(rawTransaction=tx.signed_bytes, hash=None, r=0, s=0, v=0)
            broadcast_batch.append(signed_tx)
            logger.info("Broadcasting %s", tx)
        t.mark_broadcasted(datetime.datetime.utcnow())

    try:
        hashes = broadcast_transactions(web3, broadcast_batch, confirmation_block_count=confirmation_block_count)
    except Exception as e:
        # Node error:
        # This happens when Polygon chain is busy.
        # We want to add more error information here
        # ValueError: {'code': -32000, 'message': 'tx fee (6.23 ether) exceeds the configured cap (1.00 ether)'}
        for t in instructions:
            logger.error("Could not broadcast trade: %s", t)
            for tx in t.blockchain_transactions:
                logger.error("Transaction: %s, planned gas price: %s, gas limit: %s", tx, tx.get_planned_gas_price(), tx.get_gas_limit())
        raise e

    assert len(hashes) >= len(instructions), f"We got {len(hashes)} hashes for {len(instructions)} trades"
    return res


def wait_trades_to_complete(
        web3: Web3,
        trades: List[TradeExecution],
        confirmation_block_count=0,
        max_timeout=datetime.timedelta(minutes=5),
        poll_delay=datetime.timedelta(seconds=1)) -> Dict[HexBytes, dict]:
    """Watch multiple transactions executed at parallel.

    :return: Map of transaction hashes -> receipt
    """
    logger.info("Waiting %d trades to confirm, confirm block count %d, timeout %s", len(trades), confirmation_block_count, max_timeout)
    assert isinstance(confirmation_block_count, int)
    tx_hashes = []
    for t in trades:
        for tx in t.blockchain_transactions:
            tx_hashes.append(tx.tx_hash)
    receipts = wait_transactions_to_complete(web3, tx_hashes, confirmation_block_count, max_timeout, poll_delay)
    return receipts


def is_swap_function(name: str):
    return name in ("swapExactTokensForTokens",)


def get_swap_transactions(trade: TradeExecution) -> BlockchainTransaction:
    """Get the swap transaction from multiple transactions associated with the trade"""
    for tx in trade.blockchain_transactions:
        if tx.function_selector in ("swapExactTokensForTokens",):
            return tx

    raise RuntimeError("Should not happen")


def get_current_price(web3: Web3, uniswap: UniswapV2Deployment, pair: TradingPairIdentifier, quantity=Decimal(1)) -> float:
    """Get a price from Uniswap v2 pool, assuming you are selling 1 unit of base token.

    Does decimal adjustment.

    :return: Price in quote token.
    """
    price = estimate_sell_price_decimals(uniswap, pair.base.checksum_address, pair.quote.checksum_address, quantity)
    return float(price)


def get_held_assets(web3: Web3, address: HexAddress, assets: List[AssetIdentifier]) -> Dict[str, Decimal]:
    """Get list of assets hold by the a wallet."""

    result = {}
    for asset in assets:
        token_details = fetch_erc20_details(web3, asset.checksum_address)
        balance = token_details.contract.functions.balanceOf(address).call()
        result[token_details.address.lower()] = Decimal(balance) / Decimal(10 ** token_details.decimals)
    return result


def get_token_for_asset(web3: Web3, asset: AssetIdentifier) -> Contract:
    """Get ERC-20 contract proxy."""
    erc_20 = get_deployed_contract(web3, "ERC20MockDecimals.json", Web3.toChecksumAddress(asset.address))
    return erc_20


def broadcast_and_resolve(
        web3: Web3,
        state: State,
        trades: List[TradeExecution],
        resolve_trades: callable,
        confirmation_timeout: datetime.timedelta = datetime.timedelta(minutes=1),
        confirmation_block_count: int=0,
        stop_on_execution_failure=False,
):
    """Do the live trade execution.

    - Push trades to a live blockchain

    - Wait transactions to be mined

    - Based on the transaction result, update the state of the trade if it was success or not

    :param confirmation_block_count:
        How many blocks to wait until marking transaction as confirmed

    :confirmation_timeout:
        Max time to wait for a confirmation.

        We can use zero or negative values to simulate unconfirmed trades.
        See `test_broadcast_failed_and_repair_state`.

    :param stop_on_execution_failure:
        If any of the transactions fail, then raise an exception.
        Set for unit test.
    """

    assert isinstance(confirmation_timeout, datetime.timedelta)

    broadcasted = broadcast(web3, datetime.datetime.utcnow(), trades)

    if confirmation_timeout > datetime.timedelta(0):

        receipts = wait_trades_to_complete(
            web3,
            trades,
            max_timeout=confirmation_timeout,
            confirmation_block_count=confirmation_block_count,
        )

        resolve_trades(
            web3,
            datetime.datetime.now(),
            state,
            broadcasted,
            receipts,
            stop_on_execution_failure=stop_on_execution_failure)