"""Bring your own dataset: evaluate Risk Radar on a transaction file (D81).

    # 1. Draft a mapping from the file's columns, then read and correct it.
    python ml/evaluate_dataset.py suggest path/to/transactions.csv

    # 2. Run. Writes <name>.evaluation.html / .json / .alerts.csv beside --out.
    python ml/evaluate_dataset.py run path/to/transactions.csv \
        --mapping path/to/transactions.mapping.yaml

Options for ``run``:

    --max-rows N        sample whole customers down to about N rows (default 1,000,000)
    --budget N          alerts a day; default is the shipped share of traffic (0.536%)
    --test-share F      newest share of the file used as the test period (default 0.3)
    --model PATH        a model artifact other than the newest shipped one
    --no-unseen-types   skip the hide-one-fraud-type-at-a-time test
    --allow-unreviewed  run on a mapping still marked ``reviewed: false`` (report says so)

Works on CSV, TSV, JSON lines, and Parquet or Excel when pyarrow or openpyxl is
installed. See ``ml/byod/`` for how each step works and why.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "ml"))

from byod import capability, canonical, evaluate, report  # noqa: E402
from byod.mapping import Mapping, suggest  # noqa: E402


def cmd_suggest(args: argparse.Namespace) -> None:
    path = Path(args.data)
    sample = canonical.read_table(path, nrows=args.sample_rows)
    mapping = suggest(sample, dataset=args.name or path.stem)
    out = Path(args.out) if args.out else path.with_name(path.stem + ".mapping.yaml")
    out.write_text(mapping.to_yaml(), encoding="utf-8")
    print(f"read {len(sample):,} rows, {len(sample.columns)} columns")
    for name, spec in mapping.fields.items():
        print(f"  {name:<18} <- {spec}")
    if mapping.ignore:
        print(f"  ignored (D10f): {', '.join(mapping.ignore)}")
    for note in mapping.notes:
        print(f"  note: {note}")
    problems = mapping.validate(list(sample.columns))
    for p in problems:
        print(f"  PROBLEM: {p}")
    print(f"\nwrote {out}. Read it, correct it, set `reviewed: true`, then:")
    print(f"  python ml/evaluate_dataset.py run {path} --mapping {out}")


def cmd_run(args: argparse.Namespace) -> None:
    started = time.perf_counter()
    path = Path(args.data)
    mapping_path = Path(args.mapping)
    mapping = Mapping.load(mapping_path)
    if not mapping.reviewed and not args.allow_unreviewed:
        raise SystemExit(f"{mapping_path} is marked `reviewed: false`. Check every field, set it to true, "
                         "or pass --allow-unreviewed (the report will say the mapping was not checked).")
    header = canonical.read_table(path, nrows=50)
    problems = mapping.validate(list(header.columns))
    if problems:
        raise SystemExit("the mapping does not fit this file:\n  " + "\n  ".join(problems))

    print(f"=== {mapping.dataset} ===")
    df, notes, native = canonical.load(path, mapping, max_rows=args.max_rows)
    if df.attrs.get("rows_dropped_unparseable"):
        notes["rows_dropped_unparseable"] = df.attrs["rows_dropped_unparseable"]
    print(f"  {len(df):,} canonical rows ({(df['direction'] == 'INBOUND').sum():,} credits)")

    out_base = Path(args.out) if args.out else REPO_ROOT / "ml" / "artifacts" / "datasets" / mapping_path.stem.replace(".mapping", "")
    out_base.parent.mkdir(parents=True, exist_ok=True)
    cache = out_base.with_name(out_base.name + f".features-{evaluate.cache_key(path, mapping_path.read_text(encoding='utf-8'), args.max_rows)}.npz")

    settings = evaluate.Settings(budget_per_day=args.budget, alert_rate=None if args.budget else evaluate.SHIPPED_ALERT_RATE,
                                 test_share=args.test_share, model_path=Path(args.model) if args.model else None,
                                 unseen_types=not args.no_unseen_types)
    result = evaluate.run(df, native, settings, cache)

    have = capability.declared(mapping, df)
    fired: dict[str, int] = {}
    for sigs in result["_signals_test"]:
        for s in sigs:
            fired[s.code] = fired.get(s.code, 0) + 1
    payload = {
        "dataset": mapping.dataset,
        "mapping_reviewed": mapping.reviewed,
        "source": notes,
        **report.public(result),
        "capability": {"inputs": have, "features": capability.feature_table(have, result["_X"]),
                       "rules": capability.rule_table(have, fired)},
        "mapping": mapping_path.read_text(encoding="utf-8"),
        "seconds": round(time.perf_counter() - started, 1),
    }
    alerts = out_base.with_name(out_base.name + ".alerts.csv")
    payload["alerts_arm"] = report.write_alerts(alerts, df, result)
    payload["alerts_file"] = alerts.name if payload["alerts_arm"] else None
    report.write_json(out_base.with_name(out_base.name + ".evaluation.json"), payload)
    report.write_html(out_base.with_name(out_base.name + ".evaluation.html"), payload)

    print()
    for a in payload["arms"]:
        line = f"  {a['arm']:<50} {a['alerts_per_day']:>8}/day"
        if a.get("incident_recall") is not None:
            line += (f"  incidents {a['incidents_caught']}/{a['incidents']} = {a['incident_recall']:.3f} "
                     f"[{a['ci95'][0]:.2f}, {a['ci95'][1]:.2f}]  money {a.get('fraud_value_recall')}")
        print(line)
    print(f"\nwrote {out_base.name}.evaluation.html / .json / .alerts.csv in {out_base.parent} "
          f"({payload['seconds']}s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Risk Radar on any transaction dataset (D81)")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("suggest", help="draft a column mapping to review")
    s.add_argument("data")
    s.add_argument("--out", default=None)
    s.add_argument("--name", default=None)
    s.add_argument("--sample-rows", type=int, default=100_000)
    s.set_defaults(func=cmd_suggest)

    r = sub.add_parser("run", help="evaluate with a reviewed mapping")
    r.add_argument("data")
    r.add_argument("--mapping", required=True)
    r.add_argument("--out", default=None, help="output path prefix")
    r.add_argument("--max-rows", type=int, default=1_000_000)
    r.add_argument("--budget", type=float, default=None)
    r.add_argument("--test-share", type=float, default=0.3)
    r.add_argument("--model", default=None)
    r.add_argument("--no-unseen-types", action="store_true")
    r.add_argument("--allow-unreviewed", action="store_true")
    r.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
