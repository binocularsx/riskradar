import { naira } from '../lib/api'
import { RULE, sayLower } from '../lib/words'

/**
 * "Why did this alert happen?" — answered in sentences, not codes.
 *
 * The first version showed `VELOCITY_BURST_1H` and a bar chart of feature
 * attributions. Both are true and neither answers the question an analyst is
 * actually asking, which is *what did this customer do that was unusual, and
 * unusual compared to what?*
 *
 * So each measurement gets a plain sentence and, where it makes
 * sense, an explicit comparison to that account's own normal. Only the readings
 * that are genuinely out of the ordinary are shown — a list of every number,
 * ten of them boring, is the same problem in a different shape.
 */

/**
 * Each entry decides two things: is this reading unusual enough to mention, and
 * what is the sentence. Ordered by how strongly it usually matters, so the
 * strongest evidence reads first.
 */
const READINGS = [
  // D82: the four added fraud types.
  {
    key: 'beneficiary_distinct_senders_24h',
    unusual: (v) => v >= 2,
    weight: 95,
    text: (v) => `**${Math.round(v)} other customers paid this recipient** in the last day.`,
    meaning: 'A scam collects from many victims into one account; a school or a levy does too, if the account is not new.',
  },
  {
    key: 'card_present_count_1h_account',
    unusual: (v) => v >= 3,
    weight: 92,
    text: (v) => `The card was used at **${Math.round(v)} terminals within the hour**.`,
    meaning: 'A cloned card is cashed out at machine after machine before the owner notices.',
  },
  {
    key: 'region_is_new_to_subject',
    unusual: (v) => v === 1,
    weight: 70,
    text: () => 'This happened **in a region the customer has not used this month**.',
    meaning: 'People travel, so this matters only alongside something else.',
  },
  {
    key: 'hour_of_day_local',
    unusual: (v) => v >= 0 && v < 5,
    weight: 60,
    text: (v) => `It happened at **${String(Math.round(v)).padStart(2, '0')}:00, in the small hours**.`,
    meaning: 'SIM-swap drains and card cash-outs prefer the hours the owner is asleep.',
  },
  // D77: what happened to the customer before the payment.
  {
    key: 'failed_logins_1h_subject',
    unusual: (v) => v >= 3,
    weight: 98,
    text: (v) => `**${Math.round(v)} failed logins in the hour before** this payment.`,
    meaning: 'Somebody guessing a password, not a customer mistyping once.',
  },
  {
    key: 'device_bound_hours',
    // -1: no device on the payment; 72: not bound in the last three days.
    unusual: (v) => v >= 0 && v < 24,
    weight: 97,
    text: (v) => `The phone making this payment was **bound to the account ${v < 1 ? 'under an hour' : `${Math.round(v)} hours`} ago**.`,
    meaning: 'A takeover starts by putting the attacker\'s own phone on the account.',
  },
  {
    key: 'sim_changed_hours',
    unusual: (v) => v < 24,
    weight: 96,
    text: (v) => `The customer's **SIM was changed ${Math.round(v)} hours ago**.`,
    meaning: 'Whoever holds the new SIM receives the one-time passwords.',
  },
  {
    key: 'credential_changed_hours',
    unusual: (v) => v < 24,
    weight: 94,
    text: (v) => `The **PIN, password or two-step login was changed ${Math.round(v)} hours ago**.`,
    meaning: 'Changing login details locks the real owner out.',
  },
  {
    key: 'payee_added_minutes',
    unusual: (v) => v >= 0 && v < 60,
    weight: 86,
    text: (v) => `This recipient was **added as a payee ${Math.round(v)} minutes before** being paid.`,
    meaning: 'Enrolled and paid at once is how mule accounts are lined up.',
  },
  // D78: the receiving side.
  {
    key: 'distinct_remitters_24h_account',
    unusual: (v) => v >= 4,
    weight: 93,
    text: (v) => `**${Math.round(v)} different senders** paid into this account in a day.`,
    meaning: 'Many strangers paying one account is how a mule collects victims\' money.',
  },
  {
    key: 'inbound_count_ratio_24h_vs_daily_mean_30d',
    unusual: (v) => v >= 5,
    weight: 89,
    text: (v) => `Today's credits are **${v.toFixed(1)}× this account's normal day**.`,
    meaning: 'A trader is busy every day; this account is not.',
  },
  {
    key: 'pass_through_ratio_24h',
    unusual: (v) => v >= 0.7,
    weight: 91,
    text: (v) => `**${Math.round(Math.min(v, 9.99) * 100)}% of the money that came in today has gone out again**.`,
    meaning: 'Money that only passes through is someone else\'s money being moved.',
  },
  {
    key: 'device_is_new_to_subject',
    unusual: (v) => v >= 1,
    weight: 100,
    text: () => 'The transaction came from a **device this customer has never used**.',
    meaning: 'Someone else may be holding the phone, or the customer changed device.',
  },
  {
    key: 'txn_count_1h_account',
    unusual: (v) => v >= 4,
    weight: 95,
    text: (v) => `**${Math.round(v)} transactions in one hour** on this account.`,
    meaning: 'Ordinary customers rarely transact more than once or twice an hour.',
  },
  {
    key: 'distinct_beneficiaries_1h_account',
    unusual: (v) => v >= 3,
    weight: 92,
    text: (v) => `Money went to **${Math.round(v)} different recipients within the hour**.`,
    meaning: 'Paying many separate people within minutes is how stolen money is spread across mule accounts.',
  },
  {
    key: 'amount_ratio_to_account_p95_30d',
    unusual: (v) => v >= 1.5,
    weight: 90,
    text: (v) => `The amount is **${v.toFixed(1)}× the largest this account normally sends** ` +
                 '(based on its last 30 days).',
    meaning: 'Compared against this customer, not against everybody.',
  },
  {
    key: 'decline_rate_24h_account',
    unusual: (v) => v >= 0.35,
    weight: 88,
    text: (v) => `**${Math.round(v * 100)}% of this account's attempts today were declined.**`,
    meaning: 'A wall of declines is what testing a stolen card looks like.',
  },
  {
    key: 'failed_attempts_1h_account',
    unusual: (v) => v >= 2,
    weight: 85,
    text: (v) => `**${Math.round(v)} failed attempts in the last hour** — wrong PIN, ` +
                 'insufficient funds or a limit refusal.',
    meaning: 'Somebody probing for what will go through.',
  },
  {
    key: 'days_since_account_activity',
    unusual: (v) => v >= 21,
    weight: 80,
    text: (v) => `The account had been **quiet for ${Math.round(v)} days** before this.`,
    meaning: 'Dormant, then suddenly active, is the classic takeover pattern.',
  },
  {
    key: 'approved_value_ratio_24h_vs_daily_mean_30d',
    unusual: (v) => v >= 3,
    weight: 75,
    text: (v) => `Today's outflow is **${v.toFixed(1)}× a normal day** for this account.`,
    meaning: 'The account is emptying faster than it ever has.',
  },
  {
    key: 'beneficiary_first_seen_days',
    // 0 means brand new; -1 means there is no destination at all (card, cash).
    unusual: (v) => v >= 0 && v < 1,
    weight: 70,
    text: () => 'The recipient\'s account **was first seen by the bank within the last day**.',
    meaning: 'Brand-new receiving accounts are what mule networks are built from.',
  },
  {
    key: 'beneficiary_is_new_to_account',
    unusual: (v) => v >= 1,
    weight: 60,
    text: () => 'This customer has **never paid this recipient before**.',
    meaning: 'Common and usually innocent on its own — it matters alongside the rest.',
  },
  {
    key: 'account_age_days',
    unusual: (v) => v >= 0 && v < 45,
    weight: 55,
    text: (v) => `The account itself is only **${Math.round(v)} days old**.`,
    meaning: 'Very new accounts have no behaviour to compare against.',
  },
]

/** Renders **bold** inside our sentences without pulling in a markdown library. */
function Rich({ children }) {
  const parts = String(children).split(/(\*\*[^*]+\*\*)/g)
  return (
    <>
      {parts.map((part, i) =>
        part.startsWith('**') && part.endsWith('**')
          ? <strong key={i}>{part.slice(2, -2)}</strong>
          : <span key={i}>{part}</span>
      )}
    </>
  )
}

/**
 * Take the strongest reading of each measurement across the whole case.
 *
 * Each feature is computed per transaction, but the case is the unit of work.
 * Showing the first transaction's view produced a screen that said "3
 * destinations" in the explanation and "8 destinations" in the header — both
 * true, and together plainly confusing. The peak reading is the honest
 * case-level answer: *at its worst, this is what the account was doing.*
 */
// For these, a SMALLER number is the more suspicious one: a brand-new payee,
// a brand-new account. Everything else peaks at its maximum.
const SMALLER_IS_WORSE = new Set(['beneficiary_first_seen_days', 'account_age_days'])

function peakAcross(alerts) {
  if (!alerts?.length) return null
  const peak = {}
  for (const a of alerts) {
    for (const [k, v] of Object.entries(a.features || {})) {
      const n = Number(v)
      // -1 means "does not apply" (no payee on a card payment). It is not a
      // reading, so it never competes with one.
      if (!Number.isFinite(n) || n < 0) continue
      const current = peak[k]
      if (current === undefined) { peak[k] = n; continue }
      peak[k] = SMALLER_IS_WORSE.has(k) ? Math.min(current, n) : Math.max(current, n)
    }
  }
  return peak
}

export default function Why({ alerts, features, signals, amountMinor, authResult }) {
  const merged = peakAcross(alerts) || features
  if (!merged) return null
  features = merged

  const found = READINGS
    .filter((r) => {
      const v = Number(features[r.key])
      return Number.isFinite(v) && r.unusual(v)
    })
    .sort((a, b) => b.weight - a.weight)
    .slice(0, 5)

  // Every rule that fired anywhere on the case, not just on one transaction.
  const allSignals = alerts?.length
    ? alerts.flatMap((a) => a.signals || [])
    : (signals || [])
  const ruleNames = [...new Set(allSignals.map((x) => x.code))]
  const suppressed = allSignals.filter((x) => x.power === 'SUPPRESS')

  return (
    <div className="card" style={{ background: 'var(--bg-2)' }}>
      <h3 style={{ marginBottom: 10 }}>
        Why this was flagged{alerts?.length > 1 ? ` — across all ${alerts.length} transactions` : ''}
      </h3>

      {found.length === 0 ? (
        <p className="muted" style={{ marginBottom: 0, fontSize: 13 }}>
          No single reading was far out of the ordinary. The model flagged this on
          the <em>combination</em> of several mildly unusual things — which is
          exactly what a model is for and what a fixed rule would have missed.
          Open “What moved the score” below to see the weightings.
        </p>
      ) : (
        <ol style={{ margin: 0, paddingLeft: 20 }}>
          {found.map((r) => (
            <li key={r.key} style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 13.5 }}>
                <Rich>{r.text(Number(features[r.key]))}</Rich>
              </div>
              <div className="dim" style={{ fontSize: 12, marginTop: 2 }}>{r.meaning}</div>
            </li>
          ))}
        </ol>
      )}

      {authResult && authResult !== 'APPROVED' && (
        <p className="muted" style={{ fontSize: 12.5, marginTop: 10, marginBottom: 0 }}>
          Note: this particular transaction was <strong>{authResult.toLowerCase()}</strong> —
          the money did not leave. It still matters, because the attempt is evidence.
        </p>
      )}

      {ruleNames.length > 0 && (
        <p className="dim" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
          Warning signs that also matched: {ruleNames.map((r) => sayLower(RULE, r)).join('; ')}.
          {suppressed.length > 0 && (
            <> One of them <strong>reduced</strong> the severity — the system already
            thinks part of this looks legitimate.</>
          )}
        </p>
      )}
    </div>
  )
}

/**
 * A compact "this transaction vs this customer's normal" strip.
 *
 * The single most common question after "why" is "compared to what?", and
 * answering it with a bar chart of feature attributions answers a different
 * question. This answers that one directly.
 */
export function VersusNormal({ alerts, features, amountMinor }) {
  const merged = peakAcross(alerts) || features
  if (!merged) return null
  features = merged

  // Compare the *largest* flagged amount, since that is the one an analyst
  // would question first.
  const biggest = alerts?.length
    ? Math.max(...alerts.map((a) => Number(a.amount_minor) || 0))
    : amountMinor
  const ratio = Number(features.amount_ratio_to_account_p95_30d)
  const usualCeiling = ratio > 0 ? biggest / ratio : null

  const rows = [
    {
      label: alerts?.length > 1 ? 'Largest flagged amount' : 'This amount',
      now: naira(biggest),
      normal: usualCeiling ? `usually up to ${naira(usualCeiling)}` : 'no history to compare',
      off: ratio >= 1.5,
    },
    {
      label: 'Transactions this hour',
      now: `${Math.round(Number(features.txn_count_1h_account) || 0)}`,
      normal: 'usually 0–2',
      off: Number(features.txn_count_1h_account) >= 4,
    },
    {
      label: 'Recipients this hour',
      now: `${Math.round(Number(features.distinct_beneficiaries_1h_account) || 0)}`,
      normal: 'usually 1',
      off: Number(features.distinct_beneficiaries_1h_account) >= 3,
    },
    {
      label: 'Device',
      now: Number(features.device_is_new_to_subject) >= 1 ? 'never seen before' : 'known',
      normal: 'usually a known device',
      off: Number(features.device_is_new_to_subject) >= 1,
    },
    {
      label: 'Account last used',
      now: Number(features.days_since_account_activity) < 0
        ? 'unknown'
        : `${Math.round(Number(features.days_since_account_activity))} days ago`,
      normal: 'active accounts are used weekly',
      off: Number(features.days_since_account_activity) >= 21,
    },
  ]

  return (
    <div className="card" style={{ background: 'var(--bg-2)' }}>
      <h3 style={{ marginBottom: 10 }}>
        {alerts?.length > 1 ? 'This case' : 'This transaction'} vs this customer's normal
      </h3>
      <div className="table-scroll">
        <table>
          <tbody>
            {rows.map((r) => (
              <tr key={r.label}>
                <td style={{ color: 'var(--text-2)', width: '38%' }}>{r.label}</td>
                <td style={{ fontWeight: r.off ? 650 : 400,
                             color: r.off ? 'var(--high)' : 'var(--text)' }}>
                  {r.now}
                </td>
                <td className="dim" style={{ fontSize: 12 }}>{r.normal}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="dim" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>
        Highlighted rows are the ones outside this customer's own pattern. Every
        comparison is against <em>this account</em>, not against all customers —
        a trader moving ₦2m a day is normal for a trader.
      </p>
    </div>
  )
}
