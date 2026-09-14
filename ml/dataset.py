"""Corpus loading and feature construction for training and evaluation.

Everything here goes through ``riskradar.features``. There is deliberately no
feature code in this file — if training computed its own version of
``txn_count_1h_account``, the two implementations would drift and the offline
metrics would keep looking fine while serving quietly rotted (D15).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from riskradar.features import (  # noqa: E402
    FEATURE_NAMES,
    FEATURE_SPEC_VERSION,
    PandasHistorySource,
    TxView,
    compute_features,
    to_vector,
)
from riskradar.security.tokens import (  # noqa: E402
    account_token,
    beneficiary_token,
    device_token,
    subject_token,
)


@dataclass
class Corpus:
    X: np.ndarray
    y: np.ndarray
    typology: np.ndarray       # None for legitimate rows
    incident_id: np.ndarray
    occurred_at: np.ndarray
    feature_names: tuple[str, ...] = FEATURE_NAMES
    spec_version: str = FEATURE_SPEC_VERSION


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def events_file(corpus_path: Path) -> Path:
    """``corpus.jsonl`` -> ``corpus.events.jsonl``, as the simulator writes it (D77)."""
    return corpus_path.with_name(corpus_path.stem + ".events.jsonl")


def load_events(corpus_path: Path) -> list[dict]:
    """The corpus's non-payment events, shaped and tokenised as the events table holds them.

    Same tokenisers as ingestion, and ``detail`` flattened to exactly the fields
    the SQL loader selects, so the training path sees what serving sees (D15).
    A corpus built before D77 has no events file; its event features are then
    all "not recently", which is what a bank with no event feed would see.
    """
    path = events_file(corpus_path)
    if not path.exists():
        return []
    from riskradar.features.spec import FEATURE_EVENT_TYPES

    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            detail = r.get("detail") or {}
            # Memory, not meaning: no feature reads a successful login or a
            # limit change, so skipping them here gives identical features while
            # keeping millions of rows out of memory. The SQL path reads them
            # and ignores them; the equality test covers both.
            if r["event_type"] not in FEATURE_EVENT_TYPES or (
                    r["event_type"] == "LOGIN" and detail.get("result") != "FAILED"):
                continue
            ben = detail.get("beneficiary_account_id")
            out.append({
                "occurred_at": _parse(r["occurred_at"]),
                "event_type": r["event_type"],
                "subject_token": subject_token(r["customer_id"]),
                "device_token": device_token(r["device_fingerprint"]) if r.get("device_fingerprint") else None,
                "login_result": detail.get("result"),
                "binding": detail.get("binding"),
                "beneficiary_token": beneficiary_token(ben) if ben else None,
            })
    return out


def load_corpus(path: Path, limit: int | None = None) -> Corpus:
    """Read JSONL, tokenise exactly as ingestion would, compute features.

    Tokenising here rather than reusing raw ids matters: the model must see the
    same token shapes serving will produce, and any drift between the two
    tokenisers would be a silent train/serve skew of its own.
    """
    records: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if limit and i >= limit:
                break
            records.append(json.loads(line))

    print(f"  loaded {len(records)} records")

    # Shape them the way the transactions table would hold them.
    rows = []
    for r in records:
        rows.append(
            {
                "occurred_at": _parse(r["occurred_at"]),
                "amount_minor": r["amount_minor"],
                "auth_result": r["auth_result"],
                "subject_token": subject_token(r["customer_id"]),
                "account_token": account_token(r["account_id"]),
                "beneficiary_token": (
                    beneficiary_token(r["beneficiary_account_id"])
                    if r.get("beneficiary_account_id")
                    else None
                ),
                "device_token": (
                    device_token(r["device_fingerprint"]) if r.get("device_fingerprint") else None
                ),
            }
        )

    events = load_events(path)
    print(f"  loaded {len(events)} non-payment events")

    print("  building history index ...")
    source = PandasHistorySource(rows, events)
    del events

    print("  computing features ...")
    X = np.zeros((len(records), len(FEATURE_NAMES)), dtype=float)
    y = np.zeros(len(records), dtype=int)
    typology = np.empty(len(records), dtype=object)
    incident = np.empty(len(records), dtype=object)
    occurred = np.empty(len(records), dtype=object)

    for i, (record, row) in enumerate(zip(records, rows)):
        tx = TxView(
            transaction_ref=record["transaction_ref"],
            occurred_at=row["occurred_at"],
            amount_minor=record["amount_minor"],
            currency=record["currency"],
            channel=record["channel"],
            instrument=record["instrument"],
            rail=record["rail"],
            subject_token=row["subject_token"],
            account_token=row["account_token"],
            beneficiary_token=row["beneficiary_token"],
            device_token=row["device_token"],
            ip_region=record.get("ip_region"),
            merchant_category=record.get("merchant_category"),
            auth_result=record["auth_result"],
            decline_reason=record.get("decline_reason"),
            account_opened_at=_parse(record.get("account_opened_at")),
            last_activity_at=_parse(record.get("last_activity_at")),
            product_type=record.get("product_type"),
            origin_sol_id=record.get("origin_sol_id"),
        )
        X[i] = to_vector(compute_features(tx, source.load(tx)))
        y[i] = 1 if record.get("is_fraud") else 0
        typology[i] = record.get("typology")
        incident[i] = record.get("incident_id")
        occurred[i] = row["occurred_at"]
        if i and i % 50_000 == 0:
            print(f"    {i} / {len(records)}")

    return Corpus(X=X, y=y, typology=typology, incident_id=incident, occurred_at=occurred)


def holdout_typology_split(corpus: Corpus, held_out: str, rng_seed: int = 20260909):
    """Split by **typology**, not by row (D10b).

    Standard random splits leak: if account takeover appears in both halves you
    are measuring memorisation, not generalisation. Here every fraud incident of
    the held-out typology goes to the test set and none of it is ever seen in
    training, so the resulting number honestly answers "will this catch fraud we
    did not anticipate?".

    Legitimate rows are split randomly, because there is nothing to leak in
    ordinary behaviour and both halves need a realistic negative population.
    """
    rng = np.random.default_rng(rng_seed)
    legit = corpus.y == 0
    fraud_held = (corpus.y == 1) & (corpus.typology == held_out)
    fraud_train = (corpus.y == 1) & (corpus.typology != held_out) & (corpus.y == 1)

    legit_idx = np.flatnonzero(legit)
    rng.shuffle(legit_idx)
    cut = int(0.75 * len(legit_idx))
    legit_train, legit_test = legit_idx[:cut], legit_idx[cut:]

    train_idx = np.concatenate([legit_train, np.flatnonzero(fraud_train)])
    test_idx = np.concatenate([legit_test, np.flatnonzero(fraud_held)])
    rng.shuffle(train_idx)
    rng.shuffle(test_idx)
    return train_idx, test_idx
