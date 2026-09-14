"""Stamp work-package progress into docs/build-plan.html.

    python scripts/build_plan_status.py

The plan is the tracker: STATUS below is the single place progress is recorded,
and this rewrites the progress table, the chip on each package and the count in
the header from it. Re-running is idempotent.
"""

from __future__ import annotations

import re
from pathlib import Path

PLAN = Path(__file__).resolve().parents[1] / "docs" / "build-plan.html"

# (package, title, state, date, evidence). state: built | doing | todo
STATUS = [
    ("WP-09", "Report the ratio, not the raw count", "built", "14 Sep 2026", "D70 · /v1/metrics/budget-menu"),
    ("WP-05", "Clocks the regulator sets", "doing", "14 Sep 2026", "started"),
    ("WP-01", "One event, not one transaction", "todo", "", ""),
    ("WP-06", "The twenty-four hour flag", "todo", "", "needs WP-05"),
    ("WP-07", "A decision a bank can act on", "todo", "", ""),
    ("WP-02", "Signals before the money moves", "todo", "", "needs WP-01"),
    ("WP-03", "Watch the money coming in", "todo", "", "needs WP-01"),
    ("WP-04", "The second leg", "todo", "", "needs WP-03"),
    ("WP-08", "Spend the budget by tier", "todo", "", "needs WP-06"),
]
CHIP = {"built": ('ok', 'Built'), "doing": ('chg', 'In progress'), "todo": ('', 'Not started')}


def chip(state: str) -> str:
    cls, label = CHIP[state]
    return f'<span class="chip {cls}">{label}</span>'.replace('chip ">', 'chip">')


def main() -> None:
    html = PLAN.read_text(encoding="utf-8")

    rows = "\n".join(
        f'          <tr{" class=\"now\"" if s == "doing" else ""}><td class="v">{wp}</td><td>{title}</td>'
        f'<td>{chip(s)}</td><td class="v">{date}</td><td>{ev}</td></tr>'
        for wp, title, s, date, ev in STATUS
    )
    built = sum(s == "built" for _, _, s, _, _ in STATUS)
    block = (
        "<!-- progress:start -->\n"
        '    <h3>Progress</h3>\n'
        f'    <p class="narrow">{built} of {len(STATUS)} built. Listed in build order; '
        "updated as each package lands, with the decision that records it.</p>\n"
        '    <div class="tablewrap">\n      <table>\n        <thead>\n'
        "          <tr><th>Package</th><th>What it does</th><th>Status</th><th>Updated</th><th>Record</th></tr>\n"
        f"        </thead>\n        <tbody>\n{rows}\n        </tbody>\n      </table>\n    </div>\n"
        "    <!-- progress:end -->"
    )
    if "<!-- progress:start -->" in html:
        html = re.sub(r"<!-- progress:start -->.*?<!-- progress:end -->", block, html, flags=re.S)
    else:
        anchor = '    <div class="wps">'
        html = html.replace(anchor, "    " + block + "\n\n" + anchor, 1)

    for wp, _, s, _, _ in STATUS:
        pattern = re.compile(
            rf'(<div class="id">{wp}</div>.*?<div class="side">)(\s*<div class="row status">.*?</div>)?', re.S
        )
        html = pattern.sub(lambda m: m.group(1) + f'\n          <div class="row status">{chip(s)}</div>', html, 1)

    html = re.sub(r"<span>9 work packages(?: · \d+ built)?</span>",
                  f"<span>9 work packages · {built} built</span>", html)
    PLAN.write_text(html, encoding="utf-8")
    print(f"{built} of {len(STATUS)} built")


if __name__ == "__main__":
    main()
