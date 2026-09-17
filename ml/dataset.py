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
    # D82: what the rules read from the transaction itself, not its features.
    instrument: np.ndarray | None = None
    channel: np.ndarray | None = None
    ip_region: np.ndarray | None = None
    has_beneficiary: np.ndarray | None = None
    amount_minor: np.ndarray | None = None
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


def credits_file(corpus_path: Path) -> Path:
    """``corpus.jsonl`` -> ``corpus.credits.jsonl``, as the simulator writes it (D78)."""
    return corpus_path.with_name(corpus_path.stem + ".credits.jsonl")


def _read(path: Path, limit: int | None = None) -> list[dict]:
    records: list[dict] = []
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if limit and i >= limit:
                break
            records.append(json.loads(line))
    return records


def _row(r: dict) -> dict:
    """One record shaped the way the transactions table holds it, tokenised as ingestion would."""
    inbound = r.get("direction") == "INBOUND"
    return {
        "occurred_at": _parse(r["occurred_at"]),
        "amount_minor": r["amount_minor"],
        "auth_result": r["auth_result"],
        "subject_token": subject_token(r["customer_id"]),
        "account_token": account_token(r["account_id"]),
        "beneficiary_token": (
            beneficiary_token(r["beneficiary_account_id"]) if r.get("beneficiary_account_id") else None
        ),
        "device_token": device_token(r["device_fingerprint"]) if r.get("device_fingerprint") else None,
        "direction": "INBOUND" if inbound else "OUTBOUND",
        # D78: a remitter shares the account namespace, as at ingestion.
        "remitter_token": account_token(r["remitter_account_id"]) if r.get("remitter_account_id") else None,
        # D82: region and channel travel into history for region novelty and card-present counts.
        "ip_region": r.get("ip_region"),
        "channel": r.get("channel"),
        "instrument": r.get("instrument"),
    }


def _tx(record: dict, row: dict) -> TxView:
    return TxView(
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
        direction=row["direction"],
        remitter_token=row["remitter_token"],
    )


def load_corpus(path: Path, limit: int | None = None, target: str = "payments") -> Corpus:
    """Read JSONL, tokenise exactly as ingestion would, compute features.

    Tokenising here rather than reusing raw ids matters: the model must see the
    same token shapes serving will produce, and any drift between the two
    tokenisers would be a silent train/serve skew of its own.

    History is always the whole bank: payments, credits (D78) and events (D77).
    ``target`` chooses which rows get a feature vector: ``payments`` for the
    model, ``credits`` for the receiving-side evaluation.
    """
    payments = _read(path, limit)
    # A limited (smoke) load skips credits, whose file is not in step with a prefix.
    credit_records = [] if limit else _read(credits_file(path))
    print(f"  loaded {len(payments)} payments, {len(credit_records)} credits")

    rows = [_row(r) for r in payments]
    credit_rows = [_row(r) for r in credit_records]

    events = load_events(path)
    print(f"  loaded {len(events)} non-payment events")

    print("  building history index ...")
    source = PandasHistorySource(rows + credit_rows, events)
    del events

    records, scored_rows = (credit_records, credit_rows) if target == "credits" else (payments, rows)
    if target == "credits":
        del payments, rows
    print(f"  computing features for {len(records)} {target} ...")
    X = np.zeros((len(records), len(FEATURE_NAMES)), dtype=float)
    y = np.zeros(len(records), dtype=int)
    typology = np.empty(len(records), dtype=object)
    incident = np.empty(len(records), dtype=object)
    occurred = np.empty(len(records), dtype=object)
    instrument = np.empty(len(records), dtype=object)
    channel = np.empty(len(records), dtype=object)
    region = np.empty(len(records), dtype=object)
    has_ben = np.zeros(len(records), dtype=bool)
    amount = np.zeros(len(records), dtype=np.int64)

    for i, (record, row) in enumerate(zip(records, scored_rows)):
        tx = _tx(record, row)
        X[i] = to_vector(compute_features(tx, source.load(tx)))
        y[i] = 1 if record.get("is_fraud") else 0
        typology[i] = record.get("typology")
        incident[i] = record.get("incident_id")
        occurred[i] = row["occurred_at"]
        instrument[i] = record["instrument"]
        channel[i] = record["channel"]
        region[i] = record.get("ip_region")
        has_ben[i] = bool(row["beneficiary_token"])
        amount[i] = record["amount_minor"]
        if i and i % 50_000 == 0:
            print(f"    {i} / {len(records)}")

    return Corpus(X=X, y=y, typology=typology, incident_id=incident, occurred_at=occurred,
                  instrument=instrument, channel=channel, ip_region=region, has_beneficiary=has_ben,
                  amount_minor=amount)


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
