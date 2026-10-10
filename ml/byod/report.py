"""Write what the evaluation found: JSON for machines, HTML for people, CSV of alerts.

The HTML leads with what the data could not feed, then the arms. A detection
figure read without that context is the easiest thing in this project to
misquote.
"""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ARM_ORDER = ["rules only", "shipped model + rules", "shipped model alone", "retrained model + rules",
             "retrained model alone", "retrained with the dataset's own columns + rules",
             "anomaly score (no labels) + rules", "amount alone"]

ARM_NOTE = {
    "rules only": "no model",
    "shipped model + rules": "as it ships, never shown this data",
    "shipped model alone": "as it ships, no rules",
    "retrained model + rules": "trained on the older part of this file",
    "retrained model alone": "trained on this file, no rules",
    "retrained with the dataset's own columns + rules": "a ceiling: uses columns a bank feed would not have",
    "anomaly score (no labels) + rules": "never sees a label",
    "amount alone": "the floor",
}


def public(result: dict) -> dict:
    return {k: v for k, v in result.items() if not k.startswith("_")}


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_alerts(path: Path, df: pd.DataFrame, result: dict) -> str | None:
    flagged = result["_flagged"]
    arm = next((a for a in ("shipped model + rules", "retrained model + rules", "rules only") if a in flagged), None)
    if arm is None:
        return None
    test_idx = result["_test_idx"]
    mask = flagged[arm]
    scores = result["_scores"].get(arm)
    sigs = result["_signals_test"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["source_row", "occurred_at", "account", "customer", "beneficiary", "amount_minor", "direction",
                    "channel", "instrument", "score", "rules", "is_fraud", "fraud_type"])
        for j in np.flatnonzero(mask):
            r = df.iloc[test_idx[j]]
            w.writerow([int(r["row_id"]), r["occurred_at"], r["account_token"], r["subject_token"],
                        r["beneficiary_token"], int(r["amount_minor"]), r["direction"], r["channel"], r["instrument"],
                        round(float(scores[j]), 5) if scores is not None else "",
                        " ".join(s.code for s in sigs[j]), int(r["is_fraud"]), r["fraud_type"] or ""])
    return arm


def _pct(v: Any) -> str:
    return "—" if v is None else f"{100 * float(v):.1f}%"


def _num(v: Any, digits: int = 3) -> str:
    return "—" if v is None else f"{float(v):.{digits}f}"


def _e(v: Any) -> str:
    return html.escape(str(v))


def write_html(path: Path, payload: dict) -> None:
    arms = sorted(payload["arms"], key=lambda a: ARM_ORDER.index(a["arm"]) if a["arm"] in ARM_ORDER else 99)
    labelled = payload["labelled"]
    parts: list[str] = []
    add = parts.append

    add(f"<header><p class='kicker'>Risk Radar · dataset evaluation (D81) · feature spec {_e(payload['feature_spec'])}</p>"
        f"<h1>{_e(payload['dataset'])}</h1>"
        f"<p class='lede'>{payload['source']['rows_evaluated']:,} transactions evaluated"
        + (f" of {payload['source'].get('rows_in_file', 0):,} in the file" if payload['source'].get('rows_in_file') else "")
        + f". Test period {_e(payload['split']['test_from'][:10])} to {_e(payload['split']['test_to'][:10])}"
        f" ({payload['split']['test_days']} days), budget {payload['budget']['alerts_per_day']} alerts a day"
        f" ({_e(payload['budget']['basis'])}).</p></header>")

    if not payload.get("mapping_reviewed"):
        add("<div class='warn'><b>Unreviewed mapping.</b> These figures were produced from a suggested column mapping "
            "nobody has confirmed. Do not quote them.</div>")
    for w in payload.get("warnings", []):
        add(f"<div class='warn'>{_e(w)}</div>")
    if payload["source"].get("sampled"):
        s = payload["source"]["sampled"]
        add(f"<p class='note'>Sampled by <code>{_e(s['by'])}</code>: {s['customers_kept']:,} whole customers kept, "
            f"{s['rows_kept']:,} rows. {_e(s['why'])}.</p>")

    # Headline
    if labelled:
        best = max((a for a in arms if a.get("incident_recall") is not None),
                   key=lambda a: a["incident_recall"], default=None)
        shipped = next((a for a in arms if a["arm"] == "shipped model + rules"), None)
        retrained = next((a for a in arms if a["arm"] == "retrained model + rules"), None)
        tiles = []
        for a, label in ((shipped, "Switched on as it ships"), (retrained, "After learning this data"), (best, "Best arm")):
            if a is None:
                continue
            tiles.append(f"<div class='tile'><div class='v'>{_num(a['incident_recall'], 2)}</div>"
                         f"<div class='k'>{label}: incidents caught ({_e(a['arm'])})<br>"
                         f"{a['incidents_caught']} of {a['incidents']} · 95% {a['ci95'][0]:.2f}–{a['ci95'][1]:.2f} · "
                         f"money {_pct(a.get('fraud_value_recall'))} · "
                         f"{_num(a.get('false_alerts_per_incident_caught'), 1)} false alerts per incident</div></div>")
        add("<section class='tiles'>" + "".join(tiles) + "</section>")
        br = payload.get("base_rate", {})
        add(f"<p class='note'>{br.get('fraud_rows', 0):,} fraud rows ({_pct(br.get('fraud_row_share'))} of rows) in "
            f"{br.get('incidents', 0):,} incidents; {br.get('incidents_in_test', 0):,} incidents fall in the test period.</p>")
    else:
        add("<div class='warn'>No fraud label mapped: this report lists what the system would alert on and why, "
            "but cannot say whether any of it is fraud.</div>")

    # Capability
    feats = payload["capability"]["features"]
    avail = sum(1 for f in feats if f["available"] and not f.get("constant_on_this_data"))
    add(f"<h2>What this data can feed</h2><p>{avail} of {len(feats)} measurements carry information on this data. "
        "A measurement that cannot work here is not a detection failure; it is a fact about the file.</p>")
    add("<div class='scroll'><table><thead><tr><th>Measurement</th><th>Works here</th><th>Why not</th>"
        "<th>Share “does not apply”</th></tr></thead><tbody>")
    for f in feats:
        ok = f["available"] and not f.get("constant_on_this_data")
        why = "; ".join(f["why"]) if f["why"] else ("constant on this data" if f.get("constant_on_this_data") else "")
        add(f"<tr><td><code>{_e(f['feature'])}</code></td><td><span class='chip {'ok' if ok else 'no'}'>"
            f"{'yes' if ok else 'no'}</span></td><td>{_e(why)}</td><td class='n'>{_pct(f.get('share_not_applicable'))}</td></tr>")
    add("</tbody></table></div>")
    add("<div class='scroll'><table><thead><tr><th>Rule</th><th>Can fire</th><th>Why not</th><th>Fired in test</th>"
        "</tr></thead><tbody>")
    for r in payload["capability"]["rules"]:
        add(f"<tr><td><code>{_e(r['rule'])}</code></td><td><span class='chip {'ok' if r['can_fire'] else 'no'}'>"
            f"{'yes' if r['can_fire'] else 'no'}</span></td><td>{_e('; '.join(r['why']))}</td>"
            f"<td class='n'>{r['fired']:,}</td></tr>")
    add("</tbody></table></div>")

    # Arms
    add("<h2>Each layer, same rows, same budget</h2>")
    head = "<th>Arm</th><th>Alerts a day</th>"
    if labelled:
        head += ("<th>Incidents caught</th><th>95% range</th><th>Money caught</th><th>False alerts per incident</th>"
                 "<th>Precision</th><th>Ranking (PR-AUC)</th>")
    add(f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>")
    for a in arms:
        row = f"<td>{_e(a['arm'])}<div class='sub'>{_e(ARM_NOTE.get(a['arm'], ''))}</div></td><td class='n'>{a['alerts_per_day']}</td>"
        if labelled:
            row += (f"<td class='n'>{a['incidents_caught']} / {a['incidents']} = {_num(a['incident_recall'], 3)}</td>"
                    f"<td class='n'>{a['ci95'][0]:.2f}–{a['ci95'][1]:.2f}</td><td class='n'>{_pct(a.get('fraud_value_recall'))}</td>"
                    f"<td class='n'>{_num(a.get('false_alerts_per_incident_caught'), 1)}</td><td class='n'>{_pct(a.get('precision'))}</td>"
                    + (f"<td class='n'>{_num(a['pr_auc'], 3)}<div class='sub'>{a['pr_auc_lift_over_random']}× random</div></td>"
                       if a.get('pr_auc') is not None else "<td>—</td>"))
        add(f"<tr>{row}</tr>")
    add("</tbody></table></div>")
    add("<p class='note'>Ranking is budget-free: how well the arm's score puts fraud above everything else, "
        "against a random order. A good ranking caught little only when the budget is far below the fraud volume.</p>")
    for k, v in payload.get("skipped_arms", {}).items():
        add(f"<p class='note'>Not run: {_e(k)} — {_e(v)}.</p>")

    # By type
    types = sorted({t for a in arms for t in (a.get("by_fraud_type") or {})})
    if labelled and types:
        add("<h2>By fraud type</h2><div class='scroll'><table><thead><tr><th>Arm</th>"
            + "".join(f"<th>{_e(t)}</th>" for t in types) + "</tr></thead><tbody>")
        for a in arms:
            cells = []
            for t in types:
                c = (a.get("by_fraud_type") or {}).get(t)
                cells.append(f"<td class='n'>{c['caught']}/{c['incidents']} = {_num(c['recall'], 2)}</td>" if c else "<td>—</td>")
            add(f"<tr><td>{_e(a['arm'])}</td>{''.join(cells)}</tr>")
        add("</tbody></table></div>")

    ut = payload.get("unseen_type_test")
    if ut:
        add("<h2>Fraud types the model was never shown</h2>")
        if ut.get("skipped"):
            add(f"<p class='note'>Not run: {_e(ut['skipped'])}.</p>")
        else:
            add("<p>Each type in turn is removed from training, then looked for. This is the closest this file can come "
                "to the question “would it catch a new kind of fraud?”.</p>")
            arm_names = ["rules only", "retrained model + rules", "retrained model alone", "anomaly score (no labels) + rules"]
            add("<div class='scroll'><table><thead><tr><th>Hidden type</th>" + "".join(f"<th>{_e(n)}</th>" for n in arm_names)
                + "</tr></thead><tbody>")
            for t, rows in ut["results"].items():
                if "skipped" in rows:
                    add(f"<tr><td>{_e(t)}</td><td colspan='4'>{_e(rows['skipped'])}</td></tr>")
                    continue
                cells = "".join(
                    f"<td class='n'>{rows[n]['incidents_caught']}/{rows[n]['incidents']} = {_num(rows[n]['incident_recall'], 2)}"
                    f"<div class='sub'>{rows[n]['ci95'][0]:.2f}–{rows[n]['ci95'][1]:.2f}</div></td>" for n in arm_names)
                add(f"<tr><td>{_e(t)}</td>{cells}</tr>")
            add("</tbody></table></div>")
            if ut.get("note"):
                add(f"<p class='note'>{_e(ut['note'])}</p>")

    add("<h2>How to read this</h2><ul>"
        "<li><b>Incidents, not rows.</b> An incident counts as caught if any of its transactions is alerted: one alert opens the case.</li>"
        "<li><b>Ranges overlap, no difference.</b> Two arms whose 95% ranges overlap are not distinguishable on this data.</li>"
        "<li><b>Shipped versus retrained.</b> The shipped model learned a simulated Nigerian bank. A large gap to the retrained arm "
        "is expected on foreign data and says the model must be retrained on a bank's own outcomes before it is trusted.</li>"
        "<li><b>No borrowed scores.</b> Columns somebody else computed (scores, risk, flags) were excluded as inputs (D10f).</li>"
        "</ul>")
    if payload.get("mapping"):
        add("<h2>Mapping used</h2><pre>" + _e(payload["mapping"]) + "</pre>")
    add(f"<footer>Written by <code>ml/evaluate_dataset.py</code>. Alerts listed in <code>{_e(payload.get('alerts_file') or '—')}</code>"
        f" ({_e(payload.get('alerts_arm') or '')}). JSON beside this file.</footer>")

    path.write_text(TEMPLATE.replace("{{TITLE}}", _e(payload["dataset"]) + " evaluation")
                    .replace("{{BODY}}", "\n".join(parts)), encoding="utf-8")


TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Libre+Franklin:wght@400;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root { --ink:#16202c; --ink-2:#4a5667; --line:#d9dee6; --bg:#fbfcfd; --surface:#fff; --accent:#1f5fa8;
        --ok:#1d7a4f; --ok-bg:#e3f3ea; --no:#9a3b21; --no-bg:#f8e7e0; --warn-bg:#fff5da; }
@media (prefers-color-scheme: dark) { :root { --ink:#e6ebf2; --ink-2:#a6b1c0; --line:#2c3644; --bg:#0f141b;
        --surface:#161d27; --accent:#7fb0ea; --ok:#7fd3a5; --ok-bg:#15301f; --no:#f0a58c; --no-bg:#3a1d14; --warn-bg:#3a3014; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 "Libre Franklin", system-ui, sans-serif; }
main { max-width: 1080px; margin: 0 auto; padding-block: 28px 60px; padding-inline: 20px; }
h1 { font-size: 30px; margin: 4px 0 8px; text-wrap: balance; } h2 { font-size: 19px; margin: 34px 0 8px; }
.kicker { font: 12px "IBM Plex Mono", monospace; letter-spacing: .06em; color: var(--ink-2); margin: 0; }
.lede { color: var(--ink-2); max-width: 70ch; }
.note, .sub { color: var(--ink-2); font-size: 13px; } .sub { margin-top: 2px; }
.warn { background: var(--warn-bg); border: 1px solid var(--line); padding: 10px 14px; border-radius: 4px; margin: 10px 0; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin: 18px 0 6px; }
.tile { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 14px 16px; }
.tile .v { font: 600 32px "IBM Plex Mono", monospace; color: var(--accent); } .tile .k { font-size: 13px; color: var(--ink-2); }
.scroll { overflow-x: auto; } table { border-collapse: collapse; width: 100%; background: var(--surface); font-size: 13.5px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 12px; color: var(--ink-2); font-weight: 600; } td.n { font-variant-numeric: tabular-nums; white-space: nowrap; }
code, pre { font-family: "IBM Plex Mono", monospace; font-size: 12.5px; }
pre { background: var(--surface); border: 1px solid var(--line); padding: 12px; overflow-x: auto; }
.chip { font: 11px "IBM Plex Mono", monospace; padding: 1px 7px; border-radius: 10px; }
.chip.ok { background: var(--ok-bg); color: var(--ok); } .chip.no { background: var(--no-bg); color: var(--no); }
footer { margin-top: 40px; color: var(--ink-2); font-size: 12.5px; }
</style></head><body><main>
{{BODY}}
</main></body></html>
"""
