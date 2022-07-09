"""Backtesting dataset load progress baring."""

from typing import Optional

import pandas as pd

from tradeexecutor.strategy.execution_model import ExecutionContext, ExecutionMode
from tradeexecutor.strategy.strategy_module import CreateTradingUniverseProtocol
from tradeexecutor.utils.timer import timed_task
from tradingstrategy.client import Client
from tradingstrategy.environment.jupyter import download_with_tqdm_progress_bar
from tradingstrategy.timebucket import TimeBucket


def preload_data(
        client: Client,
        create_trading_universe: CreateTradingUniverseProtocol,
        candle_time_frame_override: Optional[TimeBucket]=None,
):
    """Show nice progress bar for setting up data fees for backtesting trading universe.

    - We trigger call to `create_trading_universe` before the actual backtesting begins

    - The client is in a mode that it will display dataset download progress bars.
      We do not display these progress bars by default, as it could a bit noisy.
    """

    # Switch to the progress bar downloader
    # TODO: Make this cleaner
    client.transport.download_func = download_with_tqdm_progress_bar

    execution_context = ExecutionContext(
        mode=ExecutionMode.data_preload,
        timed_task_context_manager=timed_task,
    )

    create_trading_universe(
        pd.Timestamp.now(),
        client,
        execution_context,
        candle_time_frame_override=candle_time_frame_override,
    )

