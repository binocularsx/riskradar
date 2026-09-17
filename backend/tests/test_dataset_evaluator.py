"""D81: bring your own dataset.

Pinned: a mapping is suggested from very different column conventions, the
suggestion never feeds somebody else's score column, codes map by whole words,
money and time units are applied, sampling keeps whole customers, incidents are
derived when absent, every feature has a declared requirement, and a small
labelled file runs end to end through every arm inside the budget.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ml"))

from byod import canonical, capability, evaluate  # noqa: E402
from byod.mapping import Mapping, suggest  # noqa: E402
from riskradar.features.spec import FEATURE_NAMES  # noqa: E402


def paysim_like(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    return pd.DataFrame({
        "step": rng.integers(1, 200, n),
        "type": rng.choice(["PAYMENT", "TRANSFER", "CASH_OUT", "CASH_IN", "DEBIT"], n),
        "amount": rng.uniform(10, 50_000, n).round(2),
        "nameOrig": [f"C{rng.integers(1, 60)}" for _ in range(n)],
        "nameDest": [f"M{rng.integers(1, 90)}" for _ in range(n)],
        "oldbalanceOrg": rng.uniform(0, 1e5, n),
        "isFraud": (rng.random(n) < 0.05).astype(int),
        "isFlaggedFraud": 0,
    })


def bank_statement_like(n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(2)
    start = datetime(2026, 3, 1)
    return pd.DataFrame({
        "Transaction Date": [(start + timedelta(minutes=int(m))).strftime("%Y-%m-%d %H:%M:%S")
                             for m in rng.integers(0, 60 * 24 * 40, n)],
        "Customer ID": [f"CUST{rng.integers(1, 25)}" for _ in range(n)],
        "Amount": rng.choice([-1, 1], n) * rng.uniform(100, 90_000, n).round(2),
        "Channel": rng.choice(["Mobile App", "USSD", "POS Terminal", "Internet Banking", "ATM"], n),
        "Status": rng.choice(["Successful", "Declined", "Failed"], n, p=[0.9, 0.07, 0.03]),
        "Counterparty": [f"ACC{rng.integers(1, 80)}" for _ in range(n)],
        "risk_score": rng.random(n),
        "label": (rng.random(n) < 0.04).astype(int),
    })


def test_paysim_columns_are_recognised_and_its_flag_is_ignored():
    m = suggest(paysim_like(), "paysim")
    assert m.fields["timestamp"] == {"column": "step", "unit": "hours", "origin": "2026-01-01T00:00:00+00:00"}
    assert m.fields["account"]["column"] == "nameOrig"
    assert m.fields["beneficiary"]["column"] == "nameDest"
    assert m.fields["label"]["column"] == "isFraud"
    assert "isFlaggedFraud" in m.ignore, "somebody else's flag is never an input (D10f)"
    assert m.fields["direction"]["values"]["CASH_IN"] == "INBOUND"
    assert not m.reviewed


def test_a_bank_statement_maps_signed_money_status_and_channel_words():
    m = suggest(bank_statement_like(), "statement")
    assert m.fields["timestamp"]["column"] == "Transaction Date"
    assert m.fields["amount"].get("sign_gives_direction") is True
    assert m.fields["auth_result"]["values"] == {"Successful": "APPROVED", "Declined": "DECLINED", "Failed": "FAILED"}
    channels = m.fields["channel"]["values"]
    assert channels["POS Terminal"] == "POS" and channels["Internet Banking"] == "WEB" and channels["USSD"] == "USSD"
    assert "risk_score" in m.ignore
    assert "risk_score" not in m.native_features


def test_whole_words_only():
    m = suggest(pd.DataFrame({"when": pd.date_range("2026-01-01", periods=4, freq="h").astype(str),
                              "amt": [1.0, 2.0, 3.0, 4.0], "account": ["a", "b", "a", "b"],
                              "transaction_type": ["deposit", "transfer", "deposit", "transfer"]}), "x")
    assert m.fields["instrument"]["values"]["deposit"] == "ACCOUNT_TRANSFER", "'deposit' is not 'pos'"


def test_canonical_applies_units_signs_and_derives_incidents(tmp_path):
    df = bank_statement_like()
    m = suggest(df, "statement")
    out = canonical.canonicalise(df, m)
    assert (out["amount_minor"] > 0).all()
    raw_out = df["Amount"] < 0
    assert set(out["direction"]) <= {"INBOUND", "OUTBOUND"}
    assert (out["direction"] == "OUTBOUND").sum() == int(raw_out.sum())
    assert out["occurred_at"].is_monotonic_increasing
    assert str(out["occurred_at"].dt.tz) == "UTC"
    fraud = out[out["is_fraud"] == 1]
    assert fraud["incident_id"].notna().all() and out.loc[out["is_fraud"] == 0, "incident_id"].isna().all()


def test_incidents_split_on_the_gap():
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    df = pd.DataFrame({"subject_token": ["s"] * 3 + ["t"], "account_token": ["a"] * 3 + ["b"],
                       "is_fraud": [1, 1, 1, 1], "fraud_type": ["X"] * 4,
                       "occurred_at": [t0, t0 + pd.Timedelta(hours=2), t0 + pd.Timedelta(hours=40), t0]})
    ids = canonical.derive_incidents(df, by="customer", gap_hours=24)
    assert ids[0] == ids[1] != ids[2] and ids[3] not in (ids[0], ids[2])


def test_sampling_keeps_whole_customers(tmp_path):
    df = paysim_like(2000)
    path = tmp_path / "p.csv"
    df.to_csv(path, index=False)
    m = suggest(df, "p")
    out, notes, _native = canonical.load(path, m, max_rows=600)
    assert notes["sampled"]["rows_kept"] <= 700
    kept = set(out["account_token"])
    full = df[df["nameOrig"].isin({k[2:] for k in kept})]
    assert len(full) == len(out), "every row of every kept customer"


def test_every_feature_declares_what_it_needs():
    assert set(FEATURE_NAMES) <= set(capability.REQUIRES)


def test_an_unreviewed_mapping_is_refused(tmp_path):
    import subprocess

    df = paysim_like()
    data, mapping = tmp_path / "p.csv", tmp_path / "p.mapping.yaml"
    df.to_csv(data, index=False)
    mapping.write_text(suggest(df, "p").to_yaml(), encoding="utf-8")
    run = subprocess.run([sys.executable, str(ROOT / "ml" / "evaluate_dataset.py"), "run", str(data),
                          "--mapping", str(mapping), "--out", str(tmp_path / "o")], capture_output=True, text=True)
    assert run.returncode != 0 and "reviewed: false" in (run.stderr + run.stdout)


def test_end_to_end_every_arm_stays_inside_the_budget():
    rng = np.random.default_rng(3)
    n = 3000
    start = pd.Timestamp("2026-02-01", tz="UTC")
    df = pd.DataFrame({
        "ts": [(start + pd.Timedelta(minutes=int(m))).isoformat() for m in np.sort(rng.integers(0, 60 * 24 * 30, n))],
        "sender": [f"S{rng.integers(1, 120)}" for _ in range(n)],
        "receiver": [f"R{rng.integers(1, 400)}" for _ in range(n)],
        "amount": rng.lognormal(9, 1.2, n).round(2),
        "channel": rng.choice(["USSD", "Mobile App", "POS"], n),
        "is_fraud": (rng.random(n) < 0.03).astype(int),
        "fraud_type": None,
    })
    df.loc[df["is_fraud"] == 1, "fraud_type"] = rng.choice(["TYPE_A", "TYPE_B"], int(df["is_fraud"].sum()))
    df.loc[df["is_fraud"] == 1, "amount"] *= 8  # something to learn
    m = Mapping(fields={"timestamp": {"column": "ts"}, "amount": {"column": "amount", "scale": 100},
                        "account": {"column": "sender"}, "beneficiary": {"column": "receiver"},
                        "channel": {"column": "channel", "values": {"USSD": "USSD", "Mobile App": "MOBILE_APP",
                                                                    "POS": "POS"}},
                        "label": {"column": "is_fraud", "positive": [1]}, "fraud_type": {"column": "fraud_type"},
                        "incident": {"derive": {"by": "customer", "gap_hours": 24}}}, reviewed=True)
    canon = canonical.canonicalise(df, m)
    result = evaluate.run(canon, None, evaluate.Settings(unseen_types=True), cache=None)
    allowed = result["budget"]["alerts_in_test"]
    names = {a["arm"] for a in result["arms"]}
    assert {"rules only", "retrained model + rules", "anomaly score (no labels) + rules", "amount alone"} <= names
    for arm in result["arms"]:
        assert arm["alerts"] <= allowed + result["arms"][0]["alerts"], arm["arm"]
    amount_arm = next(a for a in result["arms"] if a["arm"] == "amount alone")
    assert amount_arm["alerts"] <= allowed, "ties must not push an arm over the budget"
    assert amount_arm["pr_auc_lift_over_random"] > 2, "the planted signal is found"
    assert set(result["unseen_type_test"]["tested"]) == {"TYPE_A", "TYPE_B"}
