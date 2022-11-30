"""Strategy status summary."""
from dataclasses import dataclass
from typing import Optional

@dataclass
class StrategySummary:
    """Strategy summary.

    - Helper class to render strategy tiles data

    - Contains mixture of static metadata, trade executor crash status,
      latest strategy performance stats and visualisation

    - Is not stored as the part of the strategy state

    - See /summary API endpoint where it is constructed before returning to the client
    """

    #: Strategy name
    name: str

    #: 1 sentence
    short_description: Optional[str]

    #: Multiple paragraphs.
    long_description: Optional[str]

    #: For <img src>
    icon_url: Optional[str]

    #: When the instance was started last time
    #:
    #: Unix timestamp, as UTC
    started_at: float

    #: Is the executor main loop running or crashed.
    #:
    #: Use /status endpoint to get the full exception info.
    #:
    #: Not really a part of metadata, but added here to make frontend
    #: queries faster. See also :py:class:`tradeexecutor.state.executor_state.ExecutorState`.
    executor_running: bool

    #: Profitability of last 90 days
    #:
    profitability_90_days: Optional[float] = None

