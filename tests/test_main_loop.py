"""Sets up the main loop and a strategy in a Ethereum Tester environment."""

import logging
import os
import datetime
import secrets
from decimal import Decimal
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
from eth_account import Account
from eth_typing import HexAddress
from hexbytes import HexBytes
from typer.testing import CliRunner
from web3 import EthereumTesterProvider, Web3
from web3.contract import Contract

from eth_hentai.hotwallet import HotWallet
from eth_hentai.balances import fetch_erc20_balances_decimal
from eth_hentai.token import create_token
from eth_hentai.uniswap_v2 import UniswapV2Deployment, deploy_trading_pair, deploy_uniswap_v2_like
from tradeexecutor.cli.main import app
from tradeexecutor.ethereum.hot_wallet_sync import EthereumHotWalletReserveSyncer
from tradeexecutor.ethereum.uniswap_v2_execution import UniswapV2ExecutionModel
from tradeexecutor.ethereum.uniswap_v2_live_pricing import UniswapV2LivePricing
from tradeexecutor.ethereum.uniswap_v2_revaluation import UniswapV2PoolRevaluator
from tradeexecutor.ethereum.universe import create_exchange_universe, create_pair_universe
from tradeexecutor.state.state import State, AssetIdentifier, TradingPairIdentifier, Portfolio, TradeExecution
from tradeexecutor.strategy.approval import UncheckedApprovalModel

from tradeexecutor.strategy.bootstrap import import_strategy_file
from tradeexecutor.strategy.runner import StrategyRunner
from tradeexecutor.utils.log import setup_pytest_logging
from tradeexecutor.utils.timer import timed_task
from tradingstrategy.candle import GroupedCandleUniverse
from tradingstrategy.chain import ChainId
from tradingstrategy.exchange import ExchangeUniverse
from tradingstrategy.liquidity import GroupedLiquidityUniverse
from tradingstrategy.pair import PairUniverse, PandasPairUniverse
from tradingstrategy.timebucket import TimeBucket
from tradingstrategy.universe import Universe


@pytest.fixture(scope="module")
def logger(request):
    """Setup test logger."""
    return setup_pytest_logging(request)


@pytest.fixture
def tester_provider():
    # https://web3py.readthedocs.io/en/stable/examples.html#contract-unit-tests-in-python
    return EthereumTesterProvider()


@pytest.fixture
def eth_tester(tester_provider):
    # https://web3py.readthedocs.io/en/stable/examples.html#contract-unit-tests-in-python
    return tester_provider.ethereum_tester


@pytest.fixture
def web3(tester_provider):
    """Set up a local unit testing blockchain."""
    # https://web3py.readthedocs.io/en/stable/examples.html#contract-unit-tests-in-python
    return Web3(tester_provider)


@pytest.fixture
def chain_id(web3) -> int:
    """The test chain id (67)."""
    return web3.eth.chain_id


@pytest.fixture()
def deployer(web3) -> HexAddress:
    """Deploy account.

    Do some account allocation for tests.
    """
    return web3.eth.accounts[0]


@pytest.fixture()
def hot_wallet_private_key(web3) -> HexBytes:
    """Generate a private key"""
    return HexBytes(secrets.token_bytes(32))


@pytest.fixture
def usdc_token(web3, deployer: HexAddress) -> Contract:
    """Create USDC with 10M supply."""
    token = create_token(web3, deployer, "Fake USDC coin", "USDC", 10_000_000 * 10**6, 6)
    return token


@pytest.fixture
def aave_token(web3, deployer: HexAddress) -> Contract:
    """Create AAVE with 10M supply."""
    token = create_token(web3, deployer, "Fake Aave coin", "AAVE", 10_000_000 * 10**18, 18)
    return token


@pytest.fixture()
def uniswap_v2(web3, deployer) -> UniswapV2Deployment:
    """Uniswap v2 deployment."""
    deployment = deploy_uniswap_v2_like(web3, deployer)
    return deployment


@pytest.fixture
def weth_token(uniswap_v2: UniswapV2Deployment) -> Contract:
    """Mock some assets"""
    return uniswap_v2.weth


@pytest.fixture
def asset_usdc(usdc_token, chain_id) -> AssetIdentifier:
    """Mock some assets"""
    return AssetIdentifier(chain_id, usdc_token.address, usdc_token.functions.symbol().call(), usdc_token.functions.decimals().call())


@pytest.fixture
def asset_weth(weth_token, chain_id) -> AssetIdentifier:
    """Mock some assets"""
    return AssetIdentifier(chain_id, weth_token.address, weth_token.functions.symbol().call(), weth_token.functions.decimals().call())


@pytest.fixture
def asset_aave(aave_token, chain_id) -> AssetIdentifier:
    """Mock some assets"""
    return AssetIdentifier(chain_id, aave_token.address, aave_token.functions.symbol().call(), aave_token.functions.decimals().call())


@pytest.fixture
def aave_usdc_uniswap_trading_pair(web3, deployer, uniswap_v2, aave_token, usdc_token) -> HexAddress:
    """AAVE-USDC pool with 200k liquidity."""
    pair_address = deploy_trading_pair(
        web3,
        deployer,
        uniswap_v2,
        aave_token,
        usdc_token,
        1000 * 10**18,  # 1000 AAVE liquidity
        200_000 * 10**6,  # 200k USDC liquidity
    )
    return pair_address


@pytest.fixture
def weth_usdc_uniswap_trading_pair(web3, deployer, uniswap_v2, weth_token, usdc_token) -> HexAddress:
    """AAVE-USDC pool with 1.7M liquidity."""
    pair_address = deploy_trading_pair(
        web3,
        deployer,
        uniswap_v2,
        weth_token,
        usdc_token,
        1000 * 10**18,  # 1000 ETH liquidity
        1_700_000 * 10**6,  # 1.7M USDC liquidity
    )
    return pair_address


@pytest.fixture
def weth_usdc_pair(weth_usdc_uniswap_trading_pair, asset_usdc, asset_weth) -> TradingPairIdentifier:
    """WETH-USDC pair representation in the trade executor domain."""
    return TradingPairIdentifier(asset_weth, asset_usdc, weth_usdc_uniswap_trading_pair, internal_id=int(weth_usdc_uniswap_trading_pair, 16))


@pytest.fixture
def aave_usdc_pair(aave_usdc_uniswap_trading_pair, asset_usdc, asset_aave) -> TradingPairIdentifier:
    """AAVE-USDC pair representation in the trade executor domain."""
    return TradingPairIdentifier(asset_aave, asset_usdc, aave_usdc_uniswap_trading_pair, internal_id=int(aave_usdc_uniswap_trading_pair, 16))


@pytest.fixture
def supported_reserves(usdc) -> List[AssetIdentifier]:
    """What reserve currencies we support for the strategy."""
    return [usdc]


@pytest.fixture()
def hot_wallet(web3: Web3, usdc_token: Contract, hot_wallet_private_key: HexBytes, deployer: HexAddress) -> HotWallet:
    """Our trading Ethereum account.

    Start with 10,000 USDC cash and 2 ETH.
    """
    account = Account.from_key(hot_wallet_private_key)
    web3.eth.send_transaction({"from": deployer, "to": account.address, "value": 2*10**18})
    usdc_token.functions.transfer(account.address, 10_000 * 10**6).transact({"from": deployer})
    wallet = HotWallet(account)
    wallet.sync_nonce(web3)
    return wallet


@pytest.fixture
def supported_reserves(asset_usdc) -> List[AssetIdentifier]:
    """The reserve currencies we support."""
    return [asset_usdc]


@pytest.fixture()
def portfolio() -> Portfolio:
    """A portfolio loaded with the initial cash.

    We start with 10,000 USDC.
    """
    portfolio = Portfolio({}, {}, {})
    return portfolio


@pytest.fixture()
def state(portfolio) -> State:
    return State(portfolio=portfolio)


@pytest.fixture()
def exchange_universe(web3, uniswap_v2: UniswapV2Deployment) -> ExchangeUniverse:
    """We trade on one Uniswap v2 deployment on tester."""
    return create_exchange_universe(web3, [uniswap_v2])


@pytest.fixture()
def pair_universe(web3, exchange_universe: ExchangeUniverse, weth_usdc_pair, aave_usdc_pair) -> PandasPairUniverse:
    """We trade on two trading pairs."""
    exchange = next(iter(exchange_universe.exchanges.values()))  # Get the first exchange from the universe
    return create_pair_universe(web3, exchange, [weth_usdc_pair, aave_usdc_pair])


@pytest.fixture()
def universe(web3, exchange_universe: ExchangeUniverse, pair_universe: PandasPairUniverse) -> Universe:
    """Get our trading universe."""
    return Universe(
        time_frame=TimeBucket.d1,
        chains=[ChainId(web3.eth.chain_id)],
        exchanges=list(exchange_universe.exchanges.values()),
        pairs=pair_universe,
        candles=GroupedCandleUniverse.create_empty_qstrader(),
        liquidity=GroupedLiquidityUniverse.create_empty(),
    )


@pytest.fixture()
def strategy_path() -> Path:
    """Where do we load our strategy file."""
    return Path(os.path.join(os.path.dirname(__file__), "strategies", "simulated_uniswap.py"))


def test_main_loop(
        logger: logging.Logger,
        strategy_path: Path,
        tester_provider,
        hot_wallet: HotWallet,
        uniswap_v2: UniswapV2Deployment,
        universe: Universe,
        state: State,
        supported_reserves,
        weth_usdc_pair,
        weth_token,
        usdc_token,
    ):
    """Run the main loop 2 times.

    Sets up the whole trade executor live trading application in local Ethereum Tester environment
    and then executed one trade.
    """

    import ipdb ; ipdb.set_trace()

    environment = {
        "PRIVATE_KEY": hot_wallet.account.privateKey,
        "HTTP_ENABLED": "false",
        "JSON_RPC": "false",
    }

    # https://typer.tiangolo.com/tutorial/testing/
    runner = CliRunner()
    result = runner.run(app, env=environment)
    assert result.exit_code == 0


