import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, naira, when } from '../lib/api'
import {
  Attributions,
  Banner,
  Empty,
  PolicyTrace,
  RiskBadge,
  SignalPill,
} from '../components/ui'

/**
 * Case detail (FR-024).
 *
 * Four things have to be on this screen for the system's claims to hold:
 *
 * 1. every correlated alert, because the case is the incident (D13a);
 * 2. the named signals with their evidence, and the model's feature
 *    attributions — an analyst handed a bare score cannot act on it (G3);
 * 3. the model, ruleset and threshold versions that produced the decision, so
 *    it can be reproduced months later (G4);
 * 4. the subject's **whole timeline**, alerted and un-alerted alike (D24b) —
 *    without which the incident-level recall claim would be unearned, because
 *    the analyst would only ever see the slice that tripped a threshold.
 */
export default function CaseDetail({ user }) {
  const { id } = useParams()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  const can = (p) => user.permissions.includes(p)

  const load = useCallback(async () => {
    try {
      setData(await api.caseDetail(id))
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  async function act(fn) {
    setBusy(true)
    setError(null)
    try {
      await fn()
      setNote('')
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (error && !data) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>

  const { case: kase, alerts, timeline, baseline, notes, history } = data
  const closed = kase.state === 'CLOSED'

  return (
    <>
      {error && <Banner kind="error">{error}</Banner>}

      <div className="between" style={{ marginBottom: 16 }}>
        <div className="row">
          <Link to="/queue">← Queue</Link>
          <h1 style={{ marginLeft: 8 }}>Case #{kase.id}</h1>
          <RiskBadge level={kase.risk_level} />
          <span className="pill">{kase.state.replace(/_/g, ' ')}</span>
          {kase.outcome && <span className="pill">{kase.outcome.replace(/_/g, ' ')}</span>}
          {kase.escalated_to && <span className="pill escalate">→ {kase.escalated_to}</span>}
        </div>
        <div className="muted" style={{ fontSize: 12 }}>
          opened {when(kase.opened_at)} · {kase.alert_count} alert{kase.alert_count === 1 ? '' : 's'}
        </div>
      </div>

      {/* ------------------------------------------------------- actions */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="row wrap">
          {can('cases:review') && !closed && kase.state !== 'UNDER_REVIEW' && (
            <button className="primary" disabled={busy} onClick={() => act(() => api.startReview(kase.id))}>
              Take for review
            </button>
          )}
          {can('cases:set_outcome') && !closed && kase.state !== 'OPEN' && (
            <>
              <button disabled={busy} onClick={() => act(() => api.setOutcome(kase.id, 'CONFIRMED_FRAUD', note))}>
                Confirmed fraud
              </button>
              <button disabled={busy} onClick={() => act(() => api.setOutcome(kase.id, 'FALSE_POSITIVE', note))}>
                False positive
              </button>
              {/* Offered with equal weight, deliberately (D13d). Forcing a binary
                  under time pressure poisons the training labels these outcomes
                  become. */}
              <button disabled={busy} onClick={() => act(() => api.setOutcome(kase.id, 'INCONCLUSIVE', note))}>
                Inconclusive
              </button>
            </>
          )}
          {can('cases:escalate') && !closed && (
            <>
              <button disabled={busy} onClick={() => act(() => api.escalate(kase.id, 'INFOSEC', note))}>
                Escalate to InfoSec
              </button>
              <button disabled={busy} onClick={() => act(() => api.escalate(kase.id, 'FRAUD_OPS', note))}>
                Escalate to Fraud Ops
              </button>
            </>
          )}
          {can('cases:close') && !closed && (
            <button className="danger" disabled={busy || !kase.outcome}
                    title={kase.outcome ? '' : 'Record an outcome first'}
                    onClick={() => act(() => api.closeCase(kase.id))}>
              Close case
            </button>
          )}
          {!can('cases:close') && !closed && (
            <span className="dim" style={{ fontSize: 12 }}>
              Closing is a Fraud Ops Lead action.
            </span>
          )}
        </div>

        {can('cases:review') && !closed && (
          <div style={{ marginTop: 12 }}>
            <label htmlFor="note">Investigation note (attached to the next action)</label>
            <textarea id="note" value={note} onChange={(e) => setNote(e.target.value)} />
            <button style={{ marginTop: 8 }} disabled={busy || !note.trim()}
                    onClick={() => act(() => api.addNote(kase.id, note))}>
              Add note
            </button>
          </div>
        )}
      </div>

      <div className="grid cols-2">
        {/* ------------------------------------------------- alerts */}
        <div className="card">
          <h3>Correlated alerts</h3>
          {alerts.map((a) => (
            <div key={a.id} style={{ borderBottom: '1px solid var(--bg-inset)', padding: '12px 0' }}>
              <div className="between">
                <div className="row">
                  <RiskBadge level={a.risk_level} />
                  <strong>{naira(a.amount_minor)}</strong>
                  <span className="muted">{a.channel.replace(/_/g, ' ')} · {a.rail}</span>
                  {a.auth_result !== 'APPROVED' && (
                    <span className="pill">{a.auth_result}{a.decline_reason ? ` · ${a.decline_reason}` : ''}</span>
                  )}
                </div>
                <span className="mono dim">{when(a.occurred_at)}</span>
              </div>

              <div className="row wrap" style={{ marginTop: 8 }}>
                {(a.signals || []).length
                  ? a.signals.map((s) => <SignalPill key={s.code} signal={s} />)
                  : <span className="dim" style={{ fontSize: 12 }}>No rule fired — this is the model's own call.</span>}
              </div>

              <details style={{ marginTop: 10 }}>
                <summary className="muted" style={{ cursor: 'pointer', fontSize: 12 }}>
                  Why this decision
                </summary>
                <div style={{ marginTop: 10 }}>
                  <h3>Policy trace</h3>
                  <PolicyTrace trace={a.policy_trace} />

                  <h3 style={{ marginTop: 14 }}>Model attribution</h3>
                  {a.rule_only_mode
                    ? <Banner kind="warn">Scored in rule-only mode — the model was unavailable.</Banner>
                    : <Attributions attributions={a.attributions} />}

                  <h3 style={{ marginTop: 14 }}>Reproducibility</h3>
                  <div className="mono dim" style={{ fontSize: 11 }}>
                    model {a.model_name ?? 'none'}:{a.model_version ?? '—'} ·
                    ruleset v{a.ruleset_version} · thresholds v{a.threshold_version} ·
                    features {a.feature_spec_version} · p={Number(a.p_fraud).toFixed(6)}
                  </div>
                </div>
              </details>
            </div>
          ))}
          {!alerts.length && <Empty>No alerts on this case.</Empty>}
        </div>

        {/* ------------------------------------------ baseline + notes */}
        <div>
          <div className="card" style={{ marginBottom: 14 }}>
            <h3>Behavioural baseline</h3>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr><th>Account</th><th>Product</th><th>Branch</th><th className="num">30d txns</th><th className="num">30d approved</th></tr>
                </thead>
                <tbody>
                  {baseline.map((b) => (
                    <tr key={b.account_token}>
                      <td className="mono">{b.account_token.slice(0, 14)}…</td>
                      <td className="muted">{b.product_type}</td>
                      <td className="muted">{b.origin_sol_id}</td>
                      <td className="num">{b.txn_30d}</td>
                      <td className="num">{naira(b.approved_value_30d_minor)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="dim" style={{ fontSize: 11, marginBottom: 0 }}>
              One customer, {baseline.length} account{baseline.length === 1 ? '' : 's'}. The case
              correlates on the customer; baselines are per account, because a salary
              current account and a dormant domiciliary account have nothing in common.
            </p>
          </div>

          <div className="card">
            <h3>Notes and history</h3>
            {notes.map((n) => (
              <div key={n.id} style={{ padding: '8px 0', borderBottom: '1px solid var(--bg-inset)' }}>
                <div className="dim" style={{ fontSize: 11 }}>{n.author} · {when(n.created_at)}</div>
                <div style={{ fontSize: 13 }}>{n.body}</div>
              </div>
            ))}
            {history.map((h, i) => (
              <div key={i} style={{ padding: '6px 0', fontSize: 12 }}>
                <span className="mono dim">{when(h.occurred_at)}</span>{' '}
                <strong>{h.action.replace(/_/g, ' ').toLowerCase()}</strong>{' '}
                <span className="muted">by {h.actor}</span>
                {h.from_state && h.to_state && (
                  <span className="dim"> ({h.from_state} → {h.to_state})</span>
                )}
              </div>
            ))}
            {!notes.length && !history.length && <Empty>Nothing recorded yet.</Empty>}
          </div>
        </div>
      </div>

      {/* --------------------------------------------------- timeline */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3>Subject timeline — every transaction in the correlation window</h3>
        <p className="dim" style={{ fontSize: 12, marginTop: -4 }}>
          Alerted rows are highlighted. The un-alerted rows are the point: an
          extraction burst looks nothing like a one-off, and you can only see the
          difference if the quiet transactions are here too.
        </p>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>When</th><th>Reference</th><th className="num">Amount</th>
                <th>Channel</th><th>Rail</th><th>Result</th>
                <th className="num">Score</th><th>Risk</th><th>Alerted</th>
              </tr>
            </thead>
            <tbody>
              {timeline.map((t) => (
                <tr key={t.id} className={`timeline-row ${t.alerted ? 'alerted' : ''}`}>
                  <td className="mono">{when(t.occurred_at)}</td>
                  <td className="mono dim">{t.transaction_ref.slice(0, 14)}…</td>
                  <td className="num">{naira(t.amount_minor)}</td>
                  <td className="muted">{t.channel.replace(/_/g, ' ')}</td>
                  <td className="muted">{t.rail}</td>
                  <td className="muted">
                    {t.auth_result}
                    {t.decline_reason && <span className="dim"> · {t.decline_reason}</span>}
                  </td>
                  <td className="num mono">{t.score_0_100 ?? '—'}</td>
                  <td><RiskBadge level={t.risk_level} /></td>
                  <td>{t.alerted ? 'yes' : <span className="dim">no</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!timeline.length && <Empty>No transactions in the window.</Empty>}
      </div>
    </>
  )
}
