"""Apply a mapping: a foreign file becomes canonical transactions.

Plain English
-------------
Reads the file (CSV, TSV, JSON lines, or Parquet/Excel when their libraries are
installed), keeps only the columns the mapping names, and turns each row into
the shape the feature package reads: a time in UTC, an amount in whole minor
units, who paid whom, on which channel, approved or not, and whether it was
fraud.

Two things here change results, so both are stated in the report:

* **Sampling keeps whole customers.** A big file is cut down by choosing
  customers, never rows, because a customer with half their history removed
  looks like a new customer to every behavioural measurement.
* **Incidents.** Risk Radar counts an incident caught if any of its rows is
  alerted. When the dataset has no incident column, fraud rows of one customer
  (and fraud type) with no gap longer than ``gap_hours`` are one incident.

Identifiers are kept as the file has them, prefixed by kind, in memory only;
they never reach the database. The live system hashes them at ingestion (D9c);
here they stay readable so the alert list can be checked against the source.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .mapping import AUTH_RESULTS, CHANNELS, INSTRUMENTS, Mapping

CANONICAL = [
    "row_id", "occurred_at", "amount_minor", "direction", "subject_token", "account_token",
    "beneficiary_token", "remitter_token", "device_token", "channel", "instrument", "auth_result",
    "decline_reason", "ip_region", "merchant_category", "account_opened_at", "last_activity_at",
    "is_fraud", "fraud_type", "incident_id",
]


def read_table(path: Path, usecols: list[str] | None = None, nrows: int | None = None,
               chunksize: int | None = None):
    suffix = path.suffix.lower()
    if suffix in (".csv", ".txt", ".tsv"):
        return pd.read_csv(path, usecols=usecols, nrows=nrows, chunksize=chunksize,
                           sep="\t" if suffix == ".tsv" else ",", low_memory=False)
    if suffix in (".jsonl", ".ndjson"):
        return pd.read_json(path, lines=True, nrows=nrows, chunksize=chunksize)
    if suffix == ".json":
        df = pd.read_json(path)
        return df.head(nrows) if nrows else df
    if suffix == ".parquet":
        try:
            df = pd.read_parquet(path, columns=usecols)
        except ImportError as exc:
            raise SystemExit("Parquet needs pyarrow: pip install pyarrow") from exc
        return df.head(nrows) if nrows else df
    if suffix in (".xlsx", ".xls"):
        try:
            df = pd.read_excel(path, usecols=usecols, nrows=nrows)
        except ImportError as exc:
            raise SystemExit("Excel needs openpyxl: pip install openpyxl") from exc
        return df
    raise SystemExit(f"unsupported file type {suffix}: use CSV, TSV, JSON lines, Parquet or Excel")


def _key_column(mapping: Mapping) -> str:
    for name in ("customer", "account", "card"):
        spec = mapping.spec(name)
        if spec and "column" in spec:
            return spec["column"]
        if spec and spec.get("columns"):
            # Sampling by the first part keeps every row of each kept value: a
            # superset of whole customers, never a split one.
            return spec["columns"][0]
    raise SystemExit("the mapping names no customer, account or card column")


def load(path: Path, mapping: Mapping, *, max_rows: int | None,
         seed: int = 20260917) -> tuple[pd.DataFrame, dict, pd.DataFrame | None]:
    """Read, sample by customer when too big, and canonicalise.

    Returns the canonical rows, notes for the report, and the mapping's native
    feature columns aligned to the canonical rows (or None).
    """
    usecols = sorted(mapping.columns_used() | set(mapping.native_features))
    notes: dict[str, Any] = {"source_file": path.name}
    suffix = path.suffix.lower()
    chunked = suffix in (".csv", ".txt", ".tsv", ".jsonl", ".ndjson")

    if max_rows and chunked:
        key = _key_column(mapping)
        counts: dict[Any, int] = {}
        total = 0
        for chunk in read_table(path, usecols=[key], chunksize=500_000):
            vc = chunk[key].value_counts(dropna=False)
            for k, n in vc.items():
                counts[k] = counts.get(k, 0) + int(n)
            total += len(chunk)
        notes["rows_in_file"] = total
        if total > max_rows:
            keys = list(counts)
            random.Random(seed).shuffle(keys)
            chosen, rows = set(), 0
            for k in keys:
                if rows + counts[k] > max_rows and chosen:
                    continue
                chosen.add(k)
                rows += counts[k]
                if rows >= max_rows * 0.98:
                    break
            parts = [c[c[key].isin(chosen)] for c in read_table(path, usecols=usecols, chunksize=500_000)]
            raw = pd.concat(parts, ignore_index=True)
            notes["sampled"] = {"by": key, "customers_kept": len(chosen), "customers_in_file": len(counts),
                                "rows_kept": int(len(raw)),
                                "why": "whole customers, so every kept customer keeps their full history"}
        else:
            raw = read_table(path, usecols=usecols)
    else:
        raw = read_table(path, usecols=usecols)
        notes["rows_in_file"] = int(len(raw))
        if max_rows and len(raw) > max_rows:
            key = _key_column(mapping)
            keys = list(raw[key].unique())
            random.Random(seed).shuffle(keys)
            sizes = raw[key].value_counts()
            chosen, rows = [], 0
            for k in keys:
                if rows >= max_rows:
                    break
                chosen.append(k)
                rows += int(sizes[k])
            raw = raw[raw[key].isin(set(chosen))].reset_index(drop=True)
            notes["sampled"] = {"by": key, "customers_kept": len(chosen), "rows_kept": int(len(raw)),
                                "why": "whole customers, so every kept customer keeps their full history"}

    df = canonicalise(raw, mapping)
    notes["rows_evaluated"] = int(len(df))
    return df, notes, raw.loc[df["row_id"].to_numpy(), mapping.native_features].reset_index(drop=True) \
        if mapping.native_features else None


# ---------------------------------------------------------------------------


def _column(raw: pd.DataFrame, spec: dict[str, Any] | None) -> pd.Series | None:
    if not spec:
        return None
    if "constant" in spec:
        return pd.Series([spec["constant"]] * len(raw), index=raw.index)
    if "column" in spec:
        return raw[spec["column"]]
    if "columns" in spec:
        # A composite identifier, e.g. a card as issuer + billing region + email
        # domain when the file has no card number. Missing if every part is.
        parts = raw[spec["columns"]]
        joined = parts.astype("string").fillna("").agg("|".join, axis=1)
        return joined.where(parts.notna().any(axis=1), None)
    return None


def _coded(raw: pd.DataFrame, spec: dict[str, Any] | None, allowed: tuple[str, ...], default: str) -> pd.Series:
    s = _column(raw, spec)
    if s is None:
        return pd.Series([default] * len(raw), index=raw.index)
    values = spec.get("values") if spec else None
    fallback = (spec or {}).get("default", default)
    if values:
        lookup = {str(k): v for k, v in values.items()}
        out = s.map(lambda v: lookup.get(str(v), fallback))
    else:
        out = s.astype(str).str.upper().where(s.astype(str).str.upper().isin(allowed), fallback)
    bad = set(out.unique()) - set(allowed)
    if bad:
        raise SystemExit(f"mapping produces values outside {allowed}: {sorted(bad)}")
    return out


def _times(raw: pd.DataFrame, spec: dict[str, Any]) -> pd.Series:
    s = raw[spec["column"]]
    unit = spec.get("unit")
    if unit in ("epoch_seconds", "epoch_milliseconds"):
        t = pd.to_datetime(s, unit="s" if unit == "epoch_seconds" else "ms", utc=True)
    elif unit:
        origin = pd.Timestamp(spec.get("origin", "2026-01-01T00:00:00+00:00"))
        if origin.tzinfo is None:
            origin = origin.tz_localize("UTC")
        t = origin + pd.to_timedelta(s.astype(float), unit={"seconds": "s", "hours": "h", "days": "D",
                                                            "minutes": "m"}[unit])
    else:
        t = pd.to_datetime(s, format=spec.get("format") or "mixed", errors="coerce")
        if t.dt.tz is None:
            t = t.dt.tz_localize(spec.get("timezone", "UTC"), ambiguous="NaT", nonexistent="shift_forward")
        t = t.dt.tz_convert("UTC")
    return t


def _ids(raw: pd.DataFrame, spec: dict[str, Any] | None, prefix: str) -> pd.Series | None:
    s = _column(raw, spec)
    if s is None:
        return None
    if pd.api.types.is_float_dtype(s) and s.dropna().mod(1).eq(0).all():
        s = s.astype("Int64")  # account numbers read as floats: 1000018177.0 is account 1000018177
    text = s.astype("string")
    return (prefix + text).where(s.notna() & (text.str.strip() != ""), None)


def canonicalise(raw: pd.DataFrame, mapping: Mapping) -> pd.DataFrame:
    n = len(raw)
    out = pd.DataFrame({"row_id": np.arange(n)})

    t = _times(raw, mapping.spec("timestamp"))
    keep = t.notna().to_numpy().copy()

    amount_spec = mapping.spec("amount")
    amount = pd.to_numeric(raw[amount_spec["column"]], errors="coerce")
    keep &= amount.notna().to_numpy()
    scale = float(amount_spec.get("scale", 100))
    out["amount_minor"] = (amount.abs() * scale).round().fillna(0).astype("int64").to_numpy()

    if amount_spec.get("sign_gives_direction"):
        direction = np.where(amount.to_numpy() < 0, "OUTBOUND", "INBOUND")
    else:
        direction = _coded(raw, mapping.spec("direction"), ("OUTBOUND", "INBOUND"), "OUTBOUND").to_numpy()
    out["direction"] = direction

    account = _ids(raw, mapping.spec("account"), "a:")
    card = _ids(raw, mapping.spec("card"), "c:")
    customer = _ids(raw, mapping.spec("customer"), "s:")
    if account is None:
        account = card
    if account is None:
        account = customer.str.replace("s:", "a:", n=1, regex=False)
    if customer is None:
        customer = account.str.replace(r"^[ac]:", "s:", regex=True)
    keep &= account.notna().to_numpy()
    out["account_token"] = account.to_numpy()
    out["subject_token"] = customer.fillna(account).to_numpy()

    other = _ids(raw, mapping.spec("beneficiary"), "b:")
    remitter = _ids(raw, mapping.spec("remitter"), "a:")
    inbound = out["direction"].to_numpy() == "INBOUND"
    if other is None:
        out["beneficiary_token"] = None
        out["remitter_token"] = remitter.to_numpy() if remitter is not None else None
    else:
        # For a credit the counterparty column names the sender, not a payee.
        counter = other.to_numpy(dtype=object)
        out["beneficiary_token"] = np.where(inbound, None, counter)
        rem = remitter.to_numpy(dtype=object) if remitter is not None else \
            np.array([None if c is None or c is pd.NA else "a:" + str(c)[2:] for c in counter], dtype=object)
        out["remitter_token"] = np.where(inbound, rem, None)

    device = _ids(raw, mapping.spec("device"), "d:")
    out["device_token"] = device.to_numpy() if device is not None else None
    out["channel"] = _coded(raw, mapping.spec("channel"), CHANNELS, "API").to_numpy()
    out["instrument"] = _coded(raw, mapping.spec("instrument"), INSTRUMENTS, "ACCOUNT_TRANSFER").to_numpy()
    out["auth_result"] = _coded(raw, mapping.spec("auth_result"), AUTH_RESULTS, "APPROVED").to_numpy()
    for name, col in (("decline_reason", "decline_reason"), ("region", "ip_region"),
                      ("merchant_category", "merchant_category")):
        s = _column(raw, mapping.spec(name))
        out[col] = s.astype("string").to_numpy(dtype=object) if s is not None else None
    opened = mapping.spec("account_opened_at")
    out["account_opened_at"] = (pd.to_datetime(raw[opened["column"]], errors="coerce", utc=True)
                                if opened and "column" in opened else pd.NaT)

    label = mapping.spec("label")
    if label:
        positive = {str(v).strip().lower() for v in label.get("positive", [1, True, "1", "true"])}
        out["is_fraud"] = raw[label["column"]].map(lambda v: str(v).strip().lower() in positive).astype(int).to_numpy()
    else:
        out["is_fraud"] = -1  # unlabelled
    ftype = _column(raw, mapping.spec("fraud_type"))
    out["fraud_type"] = ftype.astype("string").to_numpy(dtype=object) if ftype is not None else None
    out["occurred_at"] = t.to_numpy()

    out = out[keep].reset_index(drop=True)
    dropped = int(n - len(out))
    out.attrs["rows_dropped_unparseable"] = dropped

    out = out.sort_values(["occurred_at", "row_id"], kind="stable").reset_index(drop=True)
    if "fraud_type" in out and out["fraud_type"].notna().any():
        out.loc[out["is_fraud"] != 1, "fraud_type"] = None
        out.loc[(out["is_fraud"] == 1) & out["fraud_type"].isna(), "fraud_type"] = "UNSPECIFIED"
    elif (out["is_fraud"] == 1).any():
        out["fraud_type"] = np.where(out["is_fraud"] == 1, "UNSPECIFIED", None)

    # Last activity before each row, from the data itself (D22: point in time).
    prev = out.groupby("account_token", sort=False)["occurred_at"].shift(1)
    out["last_activity_at"] = prev

    inc_spec = mapping.spec("incident") or {}
    if "column" in inc_spec:
        inc = raw.loc[out["row_id"], inc_spec["column"]].astype("string").to_numpy(dtype=object)
        out["incident_id"] = np.where(out["is_fraud"] == 1, inc, None)
    else:
        derive = inc_spec.get("derive", {"by": "customer", "gap_hours": 24})
        out["incident_id"] = derive_incidents(out, by=derive.get("by", "customer"),
                                              gap_hours=float(derive.get("gap_hours", 24)))
    return out


def derive_incidents(df: pd.DataFrame, *, by: str, gap_hours: float) -> np.ndarray:
    """Fraud rows of one customer and type, with no gap over ``gap_hours``, are one incident."""
    key = "subject_token" if by == "customer" else "account_token"
    ids = np.array([None] * len(df), dtype=object)
    fraud = df[df["is_fraud"] == 1]
    if fraud.empty:
        return ids
    gap = timedelta(hours=gap_hours)
    last: dict[tuple, tuple[datetime, str]] = {}
    counter = 0
    for idx, k, ft, when in zip(fraud.index, fraud[key], fraud["fraud_type"], fraud["occurred_at"]):
        group = (k, ft)
        prior = last.get(group)
        when = pd.Timestamp(when)
        if prior is None or when - prior[0] > gap:
            counter += 1
            name = f"INC-{counter:06d}"
        else:
            name = prior[1]
        last[group] = (when, name)
        ids[idx] = name
    return ids


def to_rows(df: pd.DataFrame) -> list[dict]:
    """Rows for :class:`PandasHistorySource`: python datetimes, None for missing."""
    cols = ["occurred_at", "amount_minor", "auth_result", "subject_token", "account_token",
            "beneficiary_token", "device_token", "direction", "remitter_token", "ip_region", "channel",
            "instrument"]
    rows = df[cols].to_dict("records")
    for r in rows:
        r["occurred_at"] = pd.Timestamp(r["occurred_at"]).to_pydatetime()
        for k in ("beneficiary_token", "device_token", "remitter_token", "ip_region"):
            v = r[k]
            if v is None or v is pd.NA or (isinstance(v, float) and np.isnan(v)):
                r[k] = None
    return rows


def utc(ts: Any) -> datetime | None:
    if ts is None or ts is pd.NaT or (isinstance(ts, float) and np.isnan(ts)):
        return None
    t = pd.Timestamp(ts)
    if pd.isna(t):
        return None
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.to_pydatetime().astimezone(timezone.utc)
