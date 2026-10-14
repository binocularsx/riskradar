"""Bring your own dataset (D81).

Point Risk Radar at a transaction file somebody else produced and find out, with
the same measurements, rules and policy the live system uses, what it catches,
what it misses, and which of its measurements the data could not feed.

    python ml/evaluate_dataset.py suggest data.csv          # writes a mapping to review
    python ml/evaluate_dataset.py run data.csv --mapping data.mapping.yaml

The package is four steps, one module each:

* :mod:`mapping`  — how the file's columns become the canonical transaction;
                     a suggestion from column names and values, never trusted
                     until a person has read it.
* :mod:`canonical` — applies a mapping: parse times and money, map codes,
                     tokenise identities as ingestion does, derive incidents.
* :mod:`capability` — which features and rules this data can feed at all.
* :mod:`evaluate`   — features, arms, metrics, the unseen-fraud-type test.
* :mod:`report`     — JSON, an HTML report and the alert list.
"""
