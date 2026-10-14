"""How a foreign file's columns become Risk Radar's canonical transaction.

Plain English
-------------
Every dataset names things differently: ``nameOrig``, ``sender_account``,
``card1``, ``from``. A mapping file says, once, which column is the time, which
is the amount, who paid whom, and which column (if any) says it was fraud. It is
a small YAML file a person can read and correct, because a wrong guess about
which column is the sender makes every number afterwards meaningless while
looking perfectly plausible.

``suggest`` writes a first draft from column names and a look at the values.
It is a draft: ``run`` refuses a mapping still marked ``reviewed: false``
unless told otherwise, so nobody publishes a figure built on a guess.

A field spec is one of::

    column: sender_account                 # take the column as it is
    constant: APPROVED                     # the dataset has no such column
    column: status                         # map codes onto ours
      values: {success: APPROVED, failed: FAILED}
      default: APPROVED
    columns: [card1, addr1, P_emaildomain] # several columns joined into one identifier

plus, for times, ``unit`` (``seconds``/``hours``/``days`` from ``origin``) or
``format``/``timezone``; for money, ``scale`` to minor units and
``sign_gives_direction``; for labels, ``positive`` values.

D10f still holds: a column somebody else computed and named score, risk, flag
or anomaly is never an input. ``suggest`` lists them under ``ignore`` with the
reason, and ``run`` refuses to feed an ignored column to any arm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

# Canonical fields, what they mean, and whether the evaluation can run without them.
FIELDS: dict[str, tuple[str, bool]] = {
    "timestamp": ("when the transaction happened", True),
    "amount": ("how much, converted to minor units by `scale`", True),
    "account": ("the account money left (or, for a credit, arrived in)", False),
    "customer": ("the person or business above the account; defaults to the account", False),
    "card": ("a card number or token, used as the account when there is no account", False),
    "beneficiary": ("who was paid: an account, a wallet, a merchant", False),
    "device": ("device fingerprint or id", False),
    "channel": ("MOBILE_APP, USSD, WEB, POS, ATM, AGENT, BRANCH or API", False),
    "instrument": ("CARD, ACCOUNT_TRANSFER, CASH or WALLET", False),
    "auth_result": ("APPROVED, DECLINED, FAILED or REVERSED", False),
    "decline_reason": ("why it was refused", False),
    "direction": ("OUTBOUND (money leaves) or INBOUND (a credit)", False),
    "remitter": ("for a credit, who sent it", False),
    "region": ("coarse location: state, city, region, country", False),
    "merchant_category": ("merchant category or code", False),
    "account_opened_at": ("when the account was opened", False),
    "label": ("was it fraud", False),
    "fraud_type": ("which kind of fraud, when the dataset says", False),
    "incident": ("which fraud rows belong to one incident", False),
}

REQUIRED = [k for k, (_, req) in FIELDS.items() if req]

CHANNELS = ("MOBILE_APP", "USSD", "WEB", "POS", "ATM", "AGENT", "BRANCH", "API")
INSTRUMENTS = ("CARD", "ACCOUNT_TRANSFER", "CASH", "WALLET")
AUTH_RESULTS = ("APPROVED", "DECLINED", "FAILED", "REVERSED")

# Column-name vocabulary per field, most specific first. Matched on a normalised
# name (lower case, separators removed), whole-token first, then substring.
_NAMES: dict[str, list[str]] = {
    "timestamp": ["timestamp", "transactiondatetime", "datetime", "trans_date_trans_time", "txn_time",
                  "transaction_time", "event_time", "created_at", "occurred_at", "date", "time", "step",
                  "transactiondt", "unix_time"],
    "amount": ["amount_ngn", "transactionamt", "amount", "amt", "value", "txn_amount", "transaction_amount"],
    "account": ["sender_account", "account_id", "nameorig", "source_account", "from_account",
                "debit_account", "originator", "payer_account", "account", "acct", "sender", "origin"],
    "customer": ["customer_id", "cust_id", "client_id", "user_id", "customer", "userid", "cc_num", "bvn"],
    "card": ["card_number", "card_id", "cc_num", "card1", "pan", "card"],
    "beneficiary": ["receiver_account", "beneficiary_account", "namedest", "destination_account", "to_account",
                    "payee", "beneficiary", "receiver", "recipient", "merchant_id", "merchant", "dest"],
    "device": ["device_hash", "device_fingerprint", "device_id", "deviceinfo", "device"],
    "channel": ["payment_channel", "channel", "transaction_channel", "txn_channel"],
    "instrument": ["instrument", "payment_method", "payment_type", "product_type", "productcd", "type",
                   "transaction_type"],
    "auth_result": ["auth_result", "response_code", "status", "transaction_status", "result", "outcome"],
    "decline_reason": ["decline_reason", "failure_reason", "reason"],
    "direction": ["direction", "dr_cr", "debit_credit", "transaction_type", "type"],
    "remitter": ["remitter", "sender_account_credit"],
    "region": ["location", "region", "state", "city", "ip_region", "country", "addr1"],
    "merchant_category": ["merchant_category", "mcc", "category", "merchant_type"],
    "account_opened_at": ["account_opened_at", "account_open_date", "opened_at", "account_creation_date"],
    "label": ["is_fraud", "isfraud", "fraud", "fraud_flag", "label", "class", "target", "is_fraudulent"],
    "fraud_type": ["fraud_type", "fraudtype", "typology", "fraud_category", "scheme"],
    "incident": ["incident_id", "case_id", "fraud_case", "incident"],
}

# D10f: somebody else's opinion about the row. Never an input.
_LEAKY = re.compile(r"(score|risk|anomal|flag|suspici|predict|prob|alert|is_fraud|fraud)", re.I)

_CHANNEL_WORDS = [
    ("ussd", "USSD"), ("pos", "POS"), ("atm", "ATM"), ("agent", "AGENT"), ("branch", "BRANCH"),
    ("counter", "BRANCH"), ("teller", "BRANCH"), ("mobile", "MOBILE_APP"), ("app", "MOBILE_APP"),
    ("phone", "MOBILE_APP"), ("web", "WEB"), ("online", "WEB"), ("internet", "WEB"), ("ecom", "WEB"),
    ("card not present", "WEB"), ("cnp", "WEB"), ("card", "POS"), ("api", "API"),
]
_INSTRUMENT_WORDS = [
    ("card", "CARD"), ("pos", "CARD"), ("atm", "CARD"), ("debit", "CARD"), ("credit card", "CARD"),
    ("cash", "CASH"), ("withdraw", "CASH"), ("wallet", "WALLET"), ("airtime", "WALLET"),
    ("transfer", "ACCOUNT_TRANSFER"), ("nip", "ACCOUNT_TRANSFER"), ("payment", "ACCOUNT_TRANSFER"),
    ("bill", "ACCOUNT_TRANSFER"), ("deposit", "ACCOUNT_TRANSFER"),
]
_AUTH_WORDS = [
    ("revers", "REVERSED"), ("refund", "REVERSED"), ("declin", "DECLINED"), ("reject", "DECLINED"),
    ("denied", "DECLINED"), ("fail", "FAILED"), ("error", "FAILED"), ("timeout", "FAILED"),
    ("success", "APPROVED"), ("approv", "APPROVED"), ("complete", "APPROVED"), ("ok", "APPROVED"),
    ("00", "APPROVED"),
]
_INBOUND_WORDS = ("deposit", "credit", "cash_in", "cash-in", "cashin", "inflow", "incoming", "receive",
                  "received", "topup", "top-up", "salary", "refund in", "cr")


@dataclass
class Mapping:
    fields: dict[str, dict[str, Any]]
    dataset: str = "unnamed dataset"
    reviewed: bool = False
    ignore: dict[str, str] = field(default_factory=dict)
    native_features: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def spec(self, name: str) -> dict[str, Any] | None:
        return self.fields.get(name)

    def has(self, name: str) -> bool:
        s = self.fields.get(name)
        return bool(s) and ("column" in s or "columns" in s or "constant" in s or "derive" in s)

    def columns_used(self) -> set[str]:
        used = {s["column"] for s in self.fields.values() if s and "column" in s}
        for s in self.fields.values():
            used |= set((s or {}).get("columns") or [])
        return used

    def validate(self, columns: list[str]) -> list[str]:
        problems = []
        for name in REQUIRED:
            if not self.has(name):
                problems.append(f"`{name}` is required: {FIELDS[name][0]}")
        if not (self.has("account") or self.has("customer") or self.has("card")):
            problems.append("one of `account`, `customer` or `card` is required: behaviour is per account")
        for name, s in self.fields.items():
            if name not in FIELDS:
                problems.append(f"unknown field `{name}`")
            elif s and "column" in s and s["column"] not in columns:
                problems.append(f"`{name}` names column `{s['column']}`, which the file does not have")
            for col in (s or {}).get("columns") or []:
                if col not in columns:
                    problems.append(f"`{name}` names column `{col}`, which the file does not have")
        for col in self.native_features:
            if col in self.ignore:
                problems.append(f"`{col}` is both ignored ({self.ignore[col]}) and a native feature (D10f)")
            if col not in columns:
                problems.append(f"native feature `{col}` is not in the file")
        used = self.columns_used()
        for col, why in self.ignore.items():
            if col in used and self.fields.get("label", {}).get("column") != col \
                    and self.fields.get("fraud_type", {}).get("column") != col \
                    and self.fields.get("incident", {}).get("column") != col:
                problems.append(f"`{col}` is ignored ({why}) but mapped as an input")
        return problems

    # -- persistence --------------------------------------------------------

    def to_yaml(self) -> str:
        body = {
            "dataset": self.dataset,
            "reviewed": self.reviewed,
            "fields": {k: v for k, v in self.fields.items() if v},
            "native_features": self.native_features,
            "ignore": self.ignore,
        }
        header = [
            "# Risk Radar dataset mapping (D81). Written by `evaluate_dataset.py suggest`.",
            "# Read every field before running: a wrong sender or label column makes every",
            "# figure meaningless while looking plausible. Then set `reviewed: true`.",
        ] + [f"# NOTE: {n}" for n in self.notes] + [""]
        return "\n".join(header) + yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=110)

    @classmethod
    def load(cls, path: Path) -> "Mapping":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            fields={k: v for k, v in (raw.get("fields") or {}).items() if v},
            dataset=raw.get("dataset", path.stem),
            reviewed=bool(raw.get("reviewed", False)),
            ignore=dict(raw.get("ignore") or {}),
            native_features=list(raw.get("native_features") or []),
        )


# ---------------------------------------------------------------------------
# Suggestion
# ---------------------------------------------------------------------------


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _name_score(field_name: str, column: str) -> float:
    n = _norm(column)
    for rank, word in enumerate(_NAMES[field_name]):
        w = _norm(word)
        if n == w:
            return 100.0 - rank
        if len(w) >= 4 and w in n:
            return 50.0 - rank
    return 0.0


def _looks_like_time(s: pd.Series) -> bool:
    sample = s.dropna().astype(str).head(200)
    if sample.empty:
        return False
    parsed = pd.to_datetime(sample, errors="coerce", utc=True, format="mixed")
    return parsed.notna().mean() > 0.9 and not pd.api.types.is_numeric_dtype(s)


def _binary(s: pd.Series) -> bool:
    values = set(map(lambda v: str(v).strip().lower(), s.dropna().unique()[:10]))
    return 0 < len(values) <= 2 and values <= {"0", "1", "true", "false", "yes", "no", "y", "n", "fraud",
                                               "legit", "genuine", "1.0", "0.0"}


def _map_values(values: list[Any], words: list[tuple[str, str]], default: str) -> dict[str, str]:
    """Map codes by whole words: "deposit" must not read as "pos", nor "support" as "atm"."""
    out = {}
    for v in values:
        text = " ".join(re.split(r"[^a-z0-9]+", str(v).strip().lower()))
        out[str(v)] = next((target for word, target in words
                            if re.search(r"(?<![a-z0-9])" + re.escape(word), text)), default)
    return out


def suggest(df: pd.DataFrame, dataset: str) -> Mapping:
    """A first draft of the mapping. Scores every column for every field; one column, one field."""
    columns = list(df.columns)
    taken: set[str] = set()
    fields: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    ignore: dict[str, str] = {}

    def best(field_name: str, ok=lambda s: True) -> str | None:
        scored = sorted(((_name_score(field_name, c), c) for c in columns if c not in taken), reverse=True)
        for score, col in scored:
            if score <= 0:
                return None
            if ok(df[col]):
                return col
        return None

    # Label, type and incident first: they must never be mistaken for inputs.
    label = best("label", _binary)
    if label:
        taken.add(label)
        positive = [v for v in df[label].dropna().unique()
                    if str(v).strip().lower() in {"1", "true", "yes", "y", "fraud", "1.0"}]
        fields["label"] = {"column": label, "positive": [_plain(v) for v in positive] or [1]}
    else:
        notes.append("no fraud label found: `run` will score the data and list alerts, but cannot measure detection")
    ftype = best("fraud_type", lambda s: s.nunique(dropna=True) <= 50)
    if ftype:
        taken.add(ftype)
        fields["fraud_type"] = {"column": ftype}
    inc = best("incident")
    if inc:
        taken.add(inc)
        fields["incident"] = {"column": inc}
    elif label:
        fields["incident"] = {"derive": {"by": "customer", "gap_hours": 24}}
        notes.append("no incident column: fraud rows of one customer (and type) within 24 hours count as one incident")

    ts = best("timestamp", lambda s: _looks_like_time(s) or pd.api.types.is_numeric_dtype(s))
    if ts:
        taken.add(ts)
        if pd.api.types.is_numeric_dtype(df[ts]):
            mx = float(df[ts].max())
            unit = "hours" if mx < 20_000 else ("seconds" if mx < 1e9 else "epoch_seconds")
            if mx > 1e12:
                unit = "epoch_milliseconds"
            fields["timestamp"] = {"column": ts, "unit": unit, "origin": "2026-01-01T00:00:00+00:00"}
            notes.append(f"`{ts}` is numeric; read as {unit}. Check the unit: every per-hour measurement depends on it")
        else:
            fields["timestamp"] = {"column": ts, "timezone": "Africa/Lagos"}

    amount = best("amount", lambda s: pd.api.types.is_numeric_dtype(s))
    if amount:
        taken.add(amount)
        spec: dict[str, Any] = {"column": amount, "scale": 100}
        if (df[amount] < 0).mean() > 0.05:
            spec["sign_gives_direction"] = True
            notes.append(f"`{amount}` has negative values: read the sign as direction (negative = money leaves)")
        fields["amount"] = spec
        notes.append("amounts are multiplied by 100 into minor units (kobo, cents); set `scale: 1` if already minor")

    for name in ("account", "customer", "card", "beneficiary", "device"):
        col = best(name, lambda s: not pd.api.types.is_float_dtype(s) or s.dropna().head(100).mod(1).eq(0).all())
        if col:
            taken.add(col)
            fields[name] = {"column": col}

    for name, words, choices, default in (
        ("channel", _CHANNEL_WORDS, CHANNELS, "API"),
        ("auth_result", _AUTH_WORDS, AUTH_RESULTS, "APPROVED"),
    ):
        col = best(name, lambda s: s.nunique(dropna=True) <= 60)
        if col:
            taken.add(col)
            values = list(df[col].dropna().unique()[:60])
            fields[name] = {"column": col, "values": _map_values(values, words, default), "default": default}
    if "auth_result" not in fields:
        fields["auth_result"] = {"constant": "APPROVED"}
        notes.append("no approval/decline column: every row is treated as approved, so decline-based "
                     "measurements and the card-testing rule cannot fire")

    # Transaction-type columns usually carry both the instrument and the direction.
    type_col = best("instrument", lambda s: s.nunique(dropna=True) <= 60)
    if type_col:
        values = list(df[type_col].dropna().unique()[:60])
        fields["instrument"] = {"column": type_col, "values": _map_values(values, _INSTRUMENT_WORDS, "ACCOUNT_TRANSFER"),
                                "default": "ACCOUNT_TRANSFER"}
        if "direction" not in fields and not fields.get("amount", {}).get("sign_gives_direction"):
            inbound = {str(v): ("INBOUND" if any(w == str(v).strip().lower() or w in str(v).strip().lower().split("_")
                                                 or str(v).strip().lower().startswith(w) for w in _INBOUND_WORDS)
                                else "OUTBOUND") for v in values}
            if "INBOUND" in inbound.values():
                fields["direction"] = {"column": type_col, "values": inbound, "default": "OUTBOUND"}
                notes.append(f"`{type_col}` values read as credits: "
                             f"{sorted(k for k, v in inbound.items() if v == 'INBOUND')}")
        taken.add(type_col)

    for name in ("region", "merchant_category", "decline_reason", "account_opened_at"):
        col = best(name)
        if col:
            taken.add(col)
            fields[name] = {"column": col}

    for col in columns:
        if col in taken:
            continue
        if _LEAKY.search(col):
            ignore[col] = "somebody else's score or flag (D10f): never an input"
    native = [c for c in columns if c not in taken and c not in ignore and pd.api.types.is_numeric_dtype(df[c])
              and df[c].nunique(dropna=True) > 1]
    if native:
        notes.append(f"{len(native)} other numeric columns are offered as `native_features` for the "
                     "comparison arm only; delete any that are derived from the label or from the future")
    return Mapping(fields=fields, dataset=dataset, reviewed=False, ignore=ignore,
                   native_features=native, notes=notes)


def _plain(v: Any) -> Any:
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    return v
