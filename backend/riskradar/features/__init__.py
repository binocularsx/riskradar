"""``riskradar.features`` — the shared feature package (D15).

Training and serving both import from here. Neither is permitted its own
implementation of a feature; the equality test enforces it.
"""

from .compute import compute_features, to_vector
from .indexed import PandasHistorySource
from .sources import load_history_frame, load_history_sql
from .spec import FEATURE_NAMES, FEATURE_SPEC_VERSION
from .types import HistoryBundle, PriorTx, TxView

__all__ = [
    "FEATURE_NAMES",
    "FEATURE_SPEC_VERSION",
    "HistoryBundle",
    "PandasHistorySource",
    "PriorTx",
    "TxView",
    "compute_features",
    "load_history_frame",
    "load_history_sql",
    "to_vector",
]
