import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, naira, when } from '../lib/api'
import { CHANNEL, DECISION, DECLINE_REASON, HOLD_REASON, OUTCOME, PAYMENT_RESULT, STAGE, say, sayLower } from '../lib/words'
import { Attributions, Banner, PolicyTrace, RiskBadge, SignalPill } from './ui'
import Why from './Why'

/**
 * One payment's risk check, flagged or not (Payment lookup).
 *
 * A flagged payment's check is already on its case. This is for the payments
 * that never became one: the payment support reports that the detector let
 * through, or one a colleague mentions. It answers "why did it score what it
 * scored?" with the same evidence a case shows, and puts the payment in the
 * customer's own activity around it.
 */
export default function PaymentCheck({ transactionRef, onClose }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let live = true
    setData(null); setError(null)
    api.paymentCheck(transactionRef).then((d) => live && setData(d)).catch((e) => live && setError(e.message))
    return () => { live = false }
  }, [transactionRef])

  if (error) return <section className="card"><Banner kind="error">{error}</Banner><button className="ghost" onClick={onClose}>Close</button></section>
  if (!data) return <section className="card"><p className="muted">Loading the payment's check…</p></section>

  const t = data.transaction
  const flagged = !!t.alert_id
  const fromReport = t.alert_source === 'CUSTOMER_REPORT'
  const final = (t.policy_trace || []).find((s) => s.step === 'final')
  const held = (t.policy_trace || []).find((s) => s.step === 'budget' && s.verdict === 'DEFER')
  const level = sayLower({}, t.risk_level)
  const verdict = !data.scored
    ? 'This payment has not been checked yet. It is waiting in the queue; look again in a moment.'
    : flagged && fromReport
      ? 'Risk Radar did not flag this payment itself. It was added to a case when the customer reported it.'
      : flagged
        ? 'Risk Radar flagged this payment and it is part of a case.'
        : final?.actionable && held
          ? `Not flagged yet. Risk Radar rated it ${level}, serious enough for an alert, but the alert was held back because ${say(HOLD_REASON, held.reason)}.`
          : final && !final.actionable
            ? `Not flagged. Risk Radar rated it ${level}, below the level that raises an alert for the team.`
            : `Not flagged. Risk Radar rated it ${level}.`

  return (
    <section className="card payment-check" id="payment-check" aria-labelledby="payment-check-title">
      <div className="between wrap" style={{ gap: 8 }}>
        <div>
          <div className="ops-eyebrow">Payment check</div>
          <h2 id="payment-check-title" style={{ margin: '2px 0' }}>
            {t.direction === 'INBOUND' ? '+' : ''}{naira(t.amount_minor)} · {say(CHANNEL, t.channel)}
          </h2>
          <p className="dim" style={{ margin: 0 }}>
            {t.display_name || 'Unknown customer'} · {when(t.occurred_at)} · <span className="mono">{t.transaction_ref}</span>
          </p>
        </div>
        <button className="ghost" onClick={onClose}>Close</button>
      </div>

      <div className="row wrap" style={{ gap: 8, margin: '12px 0' }}>
        <RiskBadge level={t.risk_level} />
        <span className="pill">{say(PAYMENT_RESULT, t.auth_result)}</span>
        {t.decline_reason && <span className="pill">{say(DECLINE_REASON, t.decline_reason)}</span>}
        {t.decision && <span className="pill">{say(DECISION, t.decision)}</span>}
        {(t.signals || []).map((s) => <SignalPill key={s.code} signal={s} />)}
      </div>
      <Banner kind={flagged || held ? 'warn' : 'info'}>{verdict}</Banner>

      {data.case && (
        <p style={{ fontSize: 13 }}>
          {data.case.not_on_this_payment ? 'This payment is not on a case, but the customer has an open case: ' : 'Case: '}
          <Link to={`/cases/${data.case.id}`}>CASE-{data.case.id}</Link>{' '}
          <span className="dim">({say(STAGE, data.case.state)}{data.case.outcome ? `, ${sayLower(OUTCOME, data.case.outcome)}` : ''})</span>
        </p>
      )}

      {data.scored && (
        <div className="grid cols-2" style={{ gap: 14, alignItems: 'start' }}>
          <div>
            <Why features={t.features} signals={t.signals} amountMinor={t.amount_minor} authResult={t.auth_result}
                 title={flagged ? 'Why this was flagged' : 'What was unusual about it'}
                 emptyText={flagged ? null : 'Nothing about this payment stood out from the customer\'s normal behaviour.'} />
            <h3 style={{ marginTop: 14 }}>How the risk level was reached</h3>
            <PolicyTrace trace={t.policy_trace} />
          </div>
          <div>
            <h3 style={{ marginTop: 0 }}>What moved the score</h3>
            {t.rule_only_mode
              ? <Banner kind="warn">The risk model was not available, so only the written rules checked this payment.</Banner>
              : <Attributions attributions={t.attributions} limit={6} />}
            {data.recipient && (
              <>
                <h3 style={{ marginTop: 14 }}>The recipient</h3>
                <ul style={{ fontSize: 13, paddingLeft: 18, margin: 0 }}>
                  {data.recipient.known_mule && <li><strong>On the known mule list.</strong></li>}
                  <li>{data.recipient.paid_before_by_customer > 0
                    ? `This customer has paid this recipient ${data.recipient.paid_before_by_customer} time(s) before.`
                    : 'This customer had not paid this recipient before.'}</li>
                  <li>{data.recipient.other_customers_24h > 0
                    ? `${data.recipient.other_customers_24h} other customer(s) paid this recipient in the day before.`
                    : 'No other customers paid this recipient in the day before.'}</li>
                </ul>
              </>
            )}
          </div>
        </div>
      )}

      {data.around.length > 1 && (
        <>
          <h3 style={{ marginTop: 16 }}>The customer's activity, a day either side</h3>
          <div className="table-scroll">
            <table>
              <thead><tr><th>When</th><th className="num">Amount</th><th>Channel</th><th>Result</th><th>Risk</th><th>Case</th></tr></thead>
              <tbody>
                {data.around.map((a) => (
                  <tr key={a.transaction_ref} className={a.transaction_ref === t.transaction_ref ? 'selected' : ''}
                      style={a.transaction_ref === t.transaction_ref ? { fontWeight: 600 } : undefined}>
                    <td className="dim">{when(a.occurred_at)}{a.transaction_ref === t.transaction_ref ? ' · this payment' : ''}</td>
                    <td className="num">{a.direction === 'INBOUND' ? '+' : ''}{naira(a.amount_minor)}</td>
                    <td>{say(CHANNEL, a.channel)}</td>
                    <td>{say(PAYMENT_RESULT, a.auth_result)}</td>
                    <td><RiskBadge level={a.risk_level} /></td>
                    <td>{a.case_id ? <Link to={`/cases/${a.case_id}`}>CASE-{a.case_id}</Link> : <span className="dim">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {data.scored && (
        <details className="dim" style={{ fontSize: 10.5, marginTop: 12 }}>
          <summary>Versions used (for audit)</summary>
          <span className="mono">risk model {t.model_name ?? 'none'} {t.model_version ?? '—'} · rules v{t.ruleset_version}
            {' '}· risk levels v{t.threshold_version} · measurements v{t.feature_spec_version}</span>
        </details>
      )}
    </section>
  )
}
