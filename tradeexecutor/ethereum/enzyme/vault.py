"""Enzyme vaults integration."""

import logging
import datetime
from functools import partial
from typing import cast, Collection, List

from web3 import Web3

from eth_defi.enzyme.events import fetch_vault_balance_events, EnzymeBalanceEvent, Deposit, Redemption
from eth_defi.enzyme.vault import Vault
from eth_defi.event_reader.reader import read_events, Web3EventReader, extract_events, extract_timestamps_json_rpc
from eth_defi.event_reader.reorganisation_monitor import ReorganisationMonitor
from tradeexecutor.ethereum.token import translate_token_details
from tradeexecutor.state.portfolio import Portfolio

from tradeexecutor.state.identifier import AssetIdentifier
from tradeexecutor.state.position import TradingPosition
from tradeexecutor.state.reserve import ReservePosition
from tradeexecutor.state.state import State
from tradeexecutor.state.balance_update import BalanceUpdate, BalanceUpdateType, BalanceUpdatePositionType
from tradeexecutor.strategy.sync_model import SyncModel

logger = logging.getLogger(__name__)


class UnknownAsset(Exception):
    """Cannot map redemption asset to any known position"""


class EnzymeVaultSyncModel(SyncModel):
    """Update Enzyme vault balances."""

    def __init__(self,
                 web3: Web3,
                 vault_address: str,
                 reorg_mon: ReorganisationMonitor,
                 only_chain_listener=True,
                 ):
        """

        :param web3:
            Web3

        :param vault_address:
            The address of the vault

        :param reorg_mon:
            How to deal with block updates

        :param only_chain_listerer:
            This is the only adapter using reorg_monn.

            Will call :py:meth:`process_blocks` as the part :py:meth:`sync_treasury`.
        """
        self.web3 = web3
        self.reorg_mon = reorg_mon
        self.vault = Vault.fetch(web3, vault_address)
        self.scan_chunk_size = 10_000
        self.only_chain_listener = only_chain_listener

    def _notify(
            self,
            current_block: int,
            start_block: int,
            end_block: int,
            chunk_size: int,
            total_events: int,
            last_timestamp: int,
            context,
    ):
        """Log notifier used in Enzyme event reading"""
        # Because the code is only run once ever,
        # we show the progress by
        if end_block - start_block > 0:
            done = (current_block - start_block) / (end_block - start_block)
            logger.info(f"EnzymeVaultSyncMode: Scanning blocks {current_block:,} - {current_block + chunk_size:,}, done {done * 100:.1f}%")

    def process_blocks(self):
        """Process the reorgsanisation monitor blocks.

        :raise ChainReorganisationDetected:
            When any if the block data in our internal buffer
            does not match those provided by events.
        """
        self.reorg_mon.figure_reorganisation_and_new_blocks()

    def fetch_vault_reserve_asset(self) -> AssetIdentifier:
        """Read the reserve asset from the vault data."""
        token = self.vault.denomination_token
        address = token.address
        assert type(address) == str
        return translate_token_details(token)

    def get_related_position(self, portfolio: Portfolio, asset: AssetIdentifier) -> ReservePosition | TradingPosition:
        """Map a redemption event asset to an underlying position.

        :raise UnknownAsset:
            If we got a redemption event for an asset that does not belong to any of our positions

        """

        assert len(portfolio.reserves) == 1, "Safety check"
        reserve_position = portfolio.reserves.get(asset.address)
        if reserve_position is not None:
            return reserve_position

        spot_position = portfolio.get_open_position_for_asset(asset)
        if spot_position:
            return spot_position

        position_str = ", ".join([str(p) for p in portfolio.get_open_positions()])
        raise UnknownAsset(f"Asset {asset} does not map to any open position.\n"
                           f"Reserve: {portfolio.get_default_reserve_currency()}.\n"
                           f"Open positions: {position_str}")

    def process_deposit(self, portfolio: Portfolio, event: Deposit) -> BalanceUpdate:
        """Translate Enzyme SharesBought event to our internal deposit storage format."""

        asset = translate_token_details(event.denomination_token)
        if len(portfolio.reserves) == 0:
            # Initial deposit
            portfolio.initialise_reserves(asset)
        else:
            reserve_asset, reserve_price = portfolio.get_default_reserve_currency()
            assert asset == reserve_asset

        reserve_position = portfolio.get_reserve_position(asset)
        past_balance = reserve_position.quantity
        new_balance = reserve_position.quantity + event.investment_amount

        exchange_rate = self.vault.fetch_denomination_token_usd_exchange_rate()
        reserve_position.reserve_token_price = float(exchange_rate)
        reserve_position.last_pricing_at = datetime.datetime.utcnow()
        reserve_position.last_sync_at = datetime.datetime.utcnow()
        reserve_position.quantity += event.investment_amount

        event_id = portfolio.next_balance_update_id
        portfolio.next_balance_update_id += 1

        reserve_position.balance_updates.append(event_id)

        return BalanceUpdate(
            balance_update_id=event_id,
            position_type=BalanceUpdatePositionType.reserve,
            type=BalanceUpdateType.deposit,
            asset=asset,
            block_mined_at=event.timestamp,
            chain_id=asset.chain_id,
            past_quantity=past_balance,
            new_quantity=new_balance,
            owner_address=event.receiver,
            tx_hash=event.event_data["transactionHash"],
            log_index=event.event_data["logIndex"],
            position_id=None,
        )

    def process_redemption(self, portfolio: Portfolio, event: Redemption) -> List[BalanceUpdate]:
        """Translate Enzyme SharesBought event to our internal deposit storage format.

        In-kind redemption exchanges user share tokens to underlying
        assets.

        - User gets whatever strategy reserves there is

        - User gets share of whatever spot positions there are currently open
        """

        events = []

        for token_details, raw_amount in event.redeemed_assets:

            asset = translate_token_details(token_details)
            position = self.get_related_position(portfolio, asset)
            quantity = asset.convert_to_decimal(raw_amount)

            event_id = portfolio.next_balance_update_id
            portfolio.next_balance_update_id += 1

            if isinstance(position, ReservePosition):
                position_id = None
                past_balance = position.quantity
                position.quantity -= quantity
                new_balance = position.quantity
                position_type = BalanceUpdatePositionType.reserve
                position.balance_updates.append(event_id)
            elif isinstance(position, TradingPosition):
                position_id = position.position_id
                past_balance = position.quantity
                position.quantity -= quantity
                new_balance = position.quantity
                position_type = BalanceUpdatePositionType.open_position
                position.balance_updates.append(event_id)
            else:
                raise NotImplementedError()

            assert position.quantity > 0, f"Position went to negative: {position} with token {token_details} and amount {raw_amount}"

            evt = BalanceUpdate(
                balance_update_id=event_id,
                type=BalanceUpdateType.redemption,
                position_type=position_type,
                asset=asset,
                block_mined_at=event.timestamp,
                chain_id=asset.chain_id,
                past_quantity=past_balance,
                new_quantity=new_balance,
                owner_address=event.redeemer,
                tx_hash=event.event_data["transactionHash"],
                log_index=event.event_data["logIndex"],
                position_id=position_id,
            )
            events.append(evt)

        return events

    def translate_and_apply_event(self, state: State, event: EnzymeBalanceEvent) -> List[BalanceUpdate]:
        """Translate on-chain event data to our persistent format."""
        portfolio = state.portfolio
        match event:
            case Deposit():
                # Deposit generated only one event
                event = cast(Deposit, event)
                return [self.process_deposit(portfolio, event)]
            case Redemption():
                # Enzyme in-kind redemption can generate updates for multiple assets
                event = cast(Redemption, event)
                return self.process_redemption(portfolio, event)
            case _:
                raise RuntimeError(f"Unsupported event: {event}")

    def sync_initial(self, state: State):
        """Get the deployment event by scanning the whole chain from the start.

        Updates `state.sync.deployment` structure.
        """
        sync = state.sync
        assert not sync.is_initialised(), "Initialisation twice is not allowed"

        web3 = self.web3
        deployment = state.sync.deployment

        # Set up the reader interface for fetch_deployment_event()
        # extract_timestamp is disabled to speed up the event reading,
        # we handle it separately
        reader: Web3EventReader = cast(
            Web3EventReader,
            partial(read_events, notify=self._notify, chunk_size=self.scan_chunk_size, extract_timestamps=None)
        )

        deployment_event = self.vault.fetch_deployment_event(reader=extract_events)

        # Check that we got good event data
        block_number = deployment_event["blockNumber"]
        block_hash = deployment_event["blockHash"]
        tx_hash = deployment_event["transactionHash"]
        assert block_number > 1

        # Get the block info to get the timestamp for the event
        block_data = extract_timestamps_json_rpc(web3, block_number, block_number)
        timestamp_unix = block_data[block_hash]
        timestamp_dt = datetime.datetime.utcfromtimestamp(timestamp_unix)

        deployment.address = self.vault.vault.address
        deployment.block_number = block_number
        deployment.tx_hash = tx_hash
        deployment.block_mined_at = timestamp_dt
        deployment.vault_token_name = self.vault.get_name()
        deployment.vault_token_symbol = self.vault.get_symbol()

    def sync_treasury(self,
                      strategy_cycle_ts: datetime.datetime,
                      state: State,
                      ) -> List[BalanceUpdate]:
        """Apply the balance sync before each strategy cycle.

        - Deposits by shareholders

        - Redemptions

        :return:
            List of new treasury balance events

        :raise ChainReorganisationDetected:
            When any if the block data in our internal buffer
            does not match those provided by events.
        """

        web3 = self.web3
        sync = state.sync
        assert sync.is_initialised(), "Vault sync not initialised"

        if self.only_chain_listener:
            self.process_blocks()

        vault = self.vault

        treasury_sync = sync.treasury

        if treasury_sync.last_block_scanned:
            start_block = treasury_sync.last_block_scanned + 1
        else:
            start_block = sync.deployment.block_number

        # TODO:
        end_block = web3.eth.block_number

        # Set up the reader interface for fetch_deployment_event()
        # extract_timestamp is disabled to speed up the event reading,
        # we handle it separately
        reader: Web3EventReader = cast(
            Web3EventReader,
            partial(read_events, notify=self._notify, chunk_size=self.scan_chunk_size, reorg_mon=self.reorg_mon, extract_timestamps=None)
        )

        events_iter = fetch_vault_balance_events(
            vault,
            start_block,
            end_block,
            reader,
        )

        events = []
        for chain_event in events_iter:
            events += self.translate_and_apply_event(state, chain_event)

        past_events = set(treasury_sync.processed_events.values())
        # Check that we do not have conflicting events
        for new_event in events:
            # Use BalanceUpdateEvent.__hash__
            assert new_event not in past_events, f"Event already processed: {new_event}"
            treasury_sync.processed_events[new_event.balance_update_id] = new_event

        treasury_sync.last_block_scanned = end_block
        treasury_sync.last_updated_at = datetime.datetime.utcnow()
        treasury_sync.last_cycle_at = strategy_cycle_ts

        return events
