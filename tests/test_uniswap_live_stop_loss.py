"""Stop loss using live uniswap price feed."""
import datetime
import secrets
from decimal import Decimal

import pytest
from eth_defi.uniswap_v2.swap import swap_with_slippage_protection
from eth_typing import HexAddress
from hexbytes import HexBytes
from web3 import EthereumTesterProvider, Web3
from web3.contract import Contract
from eth_defi.token import create_token
from eth_defi.uniswap_v2.deployment import UniswapV2Deployment, deploy_trading_pair, deploy_uniswap_v2_like
from tradeexecutor.ethereum.uniswap_v2_routing import UniswapV2SimpleRoutingModel
from tradeexecutor.strategy.trading_strategy_universe import translate_trading_pair
from tradingstrategy.exchange import ExchangeUniverse
from tradingstrategy.pair import PandasPairUniverse

from tradeexecutor.ethereum.uniswap_v2_live_pricing import UniswapV2LivePricing
from tradeexecutor.ethereum.universe import create_exchange_universe, create_pair_universe
from tradeexecutor.state.identifier import AssetIdentifier, TradingPairIdentifier


#: How much values we allow to drift.
#: A hack fix receiving different decimal values on Github CI than on a local
APPROX_REL = 0.01
APPROX_REL_DECIMAL = Decimal("0.1")



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
def aave_weth_uniswap_trading_pair(web3, deployer, uniswap_v2, aave_token, weth_token) -> HexAddress:
    """AAVE-ETH pool.

    Price is 1:5 AAVE:ETH
    """
    pair_address = deploy_trading_pair(
        web3,
        deployer,
        uniswap_v2,
        weth_token,
        aave_token,
        1000 * 10**18,  # 1000 ETH liquidity
        5000 * 10**18,  # 5000 AAVE liquidity
    )
    return pair_address


@pytest.fixture()
def exchange_universe(web3, uniswap_v2: UniswapV2Deployment) -> ExchangeUniverse:
    """We trade on one Uniswap v2 deployment on tester."""
    return create_exchange_universe(web3, [uniswap_v2])


@pytest.fixture
def weth_usdc_pair(uniswap_v2, weth_usdc_uniswap_trading_pair, asset_usdc, asset_weth) -> TradingPairIdentifier:
    return TradingPairIdentifier(
        asset_weth,
        asset_usdc,
        weth_usdc_uniswap_trading_pair,
        uniswap_v2.factory.address,
    )


@pytest.fixture
def aave_weth_pair(uniswap_v2, aave_weth_uniswap_trading_pair, asset_aave, asset_weth) -> TradingPairIdentifier:
    return TradingPairIdentifier(
        asset_aave,
        asset_weth,
        aave_weth_uniswap_trading_pair,
        uniswap_v2.factory.address,
    )


@pytest.fixture()
def pair_universe(web3, exchange_universe: ExchangeUniverse, weth_usdc_pair, aave_weth_pair) -> PandasPairUniverse:
    exchange = next(iter(exchange_universe.exchanges.values()))  # Get the first exchange from the universe
    return create_pair_universe(web3, exchange, [weth_usdc_pair, aave_weth_pair])


@pytest.fixture()
def routing_model(uniswap_v2, asset_usdc, asset_weth, weth_usdc_pair) -> UniswapV2SimpleRoutingModel:

    # Allowed exchanges as factory -> router pairs
    factory_router_map = {
        uniswap_v2.factory.address: (uniswap_v2.router.address, uniswap_v2.init_code_hash),
    }

    # Three way ETH quoted trades are routed thru WETH/USDC pool
    allowed_intermediary_pairs = {
        asset_weth.address: weth_usdc_pair.pool_address
    }

    return UniswapV2SimpleRoutingModel(
        factory_router_map,
        allowed_intermediary_pairs,
        reserve_token_address=asset_usdc.address,
    )


@pytest.fixture()
def trader(web3, deployer, usdc_token) -> HexAddress:
    """Trader account.

    - Start with piles of ETH

    - Start with 9,000 USDC token

    Trades against deployer
    """
    trader = web3.eth.accounts[1]

    assert web3.eth.get_balance(trader) > 0

    usdc_token.functions.transfer(
        trader,
        9_000 * 10**6,
    )

    return trader


def test_live_stop_loss(
        web3: Web3,
        deployer: HexAddress,
        exchange_universe,
        pair_universe: PandasPairUniverse,
        routing_model: UniswapV2SimpleRoutingModel,
        uniswap_v2: UniswapV2Deployment,
        usdc_token: Contract,
        weth_token: Contract,

):
    """Two-leg buy trade."""
    pricing_method = UniswapV2LivePricing(web3, pair_universe, routing_model)

    exchange = next(iter(exchange_universe.exchanges.values()))  # Get the first exchange from the universe
    weth_usdc = pair_universe.get_one_pair_from_pandas_universe(exchange.exchange_id, "WETH", "USDC")
    pair = translate_trading_pair(weth_usdc)

    # Get price for "infinite" small trade amount
    price = pricing_method.get_buy_price(datetime.datetime.utcnow(), pair, None)
    assert price == pytest.approx(1705.12, rel=APPROX_REL)

    # Sell to change the price more than 10%.
    # The pool is 1000 ETH / 1.7M USDC.
    # Dump 300 ETH
    weth_token.functions.approve(uniswap_v2.router.address, 1000 * 10**18).transact({"from": deployer})
    prepared_swap_call = swap_with_slippage_protection(
        uniswap_v2_deployment=uniswap_v2,
        recipient_address=deployer,
        base_token=usdc_token,
        quote_token=weth_token,
        amount_in=300 * 10**18,
        max_slippage=10_000,
    )

    tx_hash = prepared_swap_call.transact({"from": deployer})
    assert tx_hash

    # Price is down $700
    price = pricing_method.get_buy_price(datetime.datetime.utcnow(), pair, None)
    assert price == pytest.approx(1009.6430522606291 , rel=APPROX_REL)
