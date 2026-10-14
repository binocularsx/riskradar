import json, sys
from pathlib import Path
import numpy as np
R = Path(r"C:\Users\CHIDERA\Desktop\Tech Academy\RiskRadar")
sys.path.insert(0, str(R / "backend")); sys.path.insert(0, str(R / "ml"))
from riskradar.features.spec import FEATURE_NAMES
from riskradar.model.calibrated import TimeSplitCalibratedBooster
T = ("ACCOUNT_TAKEOVER", "MULE_FANOUT", "CARD_TESTING")
d = np.load(R / "ml/data/corpus.features.npz", allow_pickle=True)
X, y, typ, occ, inc = d["X"], d["y"].astype(int), d["typology"], d["occurred_at"], d["incident_id"]
inst = np.array([json.loads(l)["instrument"] for l in open(R / "ml/data/corpus.jsonl", encoding="utf-8")])
o = np.argsort(occ); X, y, typ, occ, inc, inst = X[o], y[o], typ[o], occ[o], inc[o], inst[o]
cut = int(0.75 * len(y)); days = (occ[-1] - occ[cut]).total_seconds() / 86400
p = TimeSplitCalibratedBooster().fit(X[:cut], y[:cut], times=occ[:cut]).predict_proba(X[cut:])[:, 1]
Xt, yt, tt, it, nt = X[cut:], y[cut:], typ[cut:], inc[cut:], inst[cut:]
c = lambda n: Xt[:, FEATURE_NAMES.index(n)]
vel, nb, ben_new, first, dev, dorm = (c("txn_count_1h_account"), c("distinct_beneficiaries_1h_account"),
    c("beneficiary_is_new_to_account"), c("beneficiary_first_seen_days"), c("device_is_new_to_subject"), c("days_since_account_activity"))
card = (nt == "CARD") & (c("decline_rate_24h_account") >= .5) & (c("failed_attempts_1h_account") >= 3)
new_dest = ben_new == 1
fresh_dest = (first >= 0) & (first < 1)
risky_ctx = new_dest | (dev == 1) | (dorm >= 21)
V = {
  "now: >=10": vel >= 10,
  ">=10 & new dest": (vel >= 10) & new_dest,
  ">=10 & dest new to bank": (vel >= 10) & fresh_dest,
  ">=10 & (new dest|new device|dormant)": (vel >= 10) & risky_ctx,
  ">=5 & new dest": (vel >= 5) & new_dest,
  ">=5 & (new dest|new device|dormant)": (vel >= 5) & risky_ctx,
  ">=5 & dest new to bank": (vel >= 5) & fresh_dest,
  ">=5 & >=3 payees & new dest": (vel >= 5) & (nb >= 3) & new_dest,
  ">=7 & new dest": (vel >= 7) & new_dest,
}
tot = {t: len(set(it[tt == t])) for t in T}
print(f"test {days:.1f}d incidents {tot}")
print(f"{'variant':<40}{'rule/day':>9}{'rule FA/day':>12}{'rule prec':>10}{'mule by rule':>13} | system@120: FA/day  ATO  MULE  CARD")
out = []
for name, v in V.items():
    rule = card | v
    rv = v & ~card
    mule_rule = len(set(it[v & (tt == "MULE_FANOUT")])) / tot["MULE_FANOUT"]
    spare = int(120 * days - rule.sum())
    free = np.flatnonzero(~rule); rk = free[np.argsort(-p[free], kind="stable")]
    mf = np.zeros(len(yt), bool); mf[rk[:max(spare, 0)]] = True; s = rule | mf
    caught = {t: len(set(it[s & (tt == t)])) for t in T}
    row = dict(variant=name, rule_day=round(v.sum()/days,1), rule_fa_day=round((v & (yt==0)).sum()/days,1),
               rule_precision=round(float((v & (yt==1)).sum()/max(v.sum(),1)),3), mule_by_rule_alone=round(mule_rule,3),
               system_fa_day=round((s & (yt==0)).sum()/days,1), caught=caught)
    out.append(row)
    print(f"{name:<40}{row['rule_day']:>9}{row['rule_fa_day']:>12}{row['rule_precision']:>10}{row['mule_by_rule_alone']:>13} | "
          f"{row['system_fa_day']:>6}  {caught['ACCOUNT_TAKEOVER']:>3}  {caught['MULE_FANOUT']:>4}  {caught['CARD_TESTING']:>4}")
(R / "ml/artifacts/velocity-variants.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
