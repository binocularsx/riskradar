import { useCallback, useEffect, useState } from 'react'

import { ago, api, clock, naira, nairaShort, when } from '../lib/api'
import { Attributions, Banner, PolicyTrace, RiskBadge, SignalPill } from './ui'
import Timeline from './Timeline'

/**
 * The investigation panel — everything needed to decide, on one screen.
 *
 * The first version of this screen listed the evidence and made the analyst
 * work out what to do with it. This one leads with the recommendation, puts the
 * disposition controls permanently within reach, and keeps the evidence one
 * click below rather than three scrolls down.
 *
 * The order is the order an analyst actually thinks in:
 *
 *   who and how much  →  what should I do  →  what does the activity look like
 *   →  why did it fire  →  record the answer
 */

const OUTCOMES = [
  { key: 'CONFIRMED_FRAUD', label: 'Confirm fraud', cls: 'confirm', hint: '1' },
  { key: 'FALSE_POSITIVE', label: 'False positive', cls: 'dismiss', hint: '2' },
  { key: 'INCONCLUSIVE', label: 'Inconclusive', cls: 'unsure', hint: '3' },
]

export default function CaseView({ summary, user, onDisposed, onSkip }) {
  const [detail, setDetail] = useState(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [flash, setFlash] = useState(null)

  const can = (p) => user.permissions.includes(p)
  const caseId = summary?.id

  useEffect(() => {
    let cancelled = false
    setDetail(null)
    setNote('')
    setError(null)
    if (!caseId) return undefined
    api.caseDetail(caseId)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setError(e.message))
    return () => { cancelled = true }
  }, [caseId])

  const dispose = useCallback(async (outcome) => {
    if (!caseId || busy) return
    setBusy(true)
    setError(null)
    try {
      const followed = summary.recommendation?.disposition_hint === outcome
      const result = await api.disposition(caseId, {
        outcome,
        note: note.trim() || null,
        close: can('cases:close'),
        followed_recommendation: followed,
      })
      setFlash(
        result.closed
          ? `Case #${caseId} closed as ${outcome.replace(/_/g, ' ').toLowerCase()}.`
          : `Outcome recorded. ${result.steps.includes('left open — closing is a Fraud Ops Lead action')
              ? 'A Fraud Ops Lead will close it.' : ''}`
      )
      onDisposed?.(result)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }, [caseId, busy, note, summary, onDisposed, can])

  // Keyboard disposition. A desk working forty cases a day should not be
  // reaching for a mouse three times per case.
  useEffect(() => {
    function onKey(e) {
      if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const found = OUTCOMES.find((o) => o.hint === e.key)
      if (found && can('cases:set_outcome')) { e.preventDefault(); dispose(found.key) }
      if (e.key === 'n' || e.key === 'N') { e.preventDefault(); onSkip?.() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [dispose, onSkip, can])

  if (!summary) {
    return (
      <div className="empty">
        <div className="big">Nothing selected</div>
        Pick a case from the list, or press <kbd>Start reviewing</kbd> to be handed
        the highest-priority one.
      </div>
    )
  }

  const rec = summary.recommendation || {}
  const closed = summary.state === 'CLOSED'

  return (
    <>
      {flash && <Banner kind="ok">{flash}</Banner>}
      {error && <Banner kind="error">{error}</Banner>}

      {/* ---------------------------------------------- who and how much */}
      <div className="casehead">
        <div>
          <div className="who">{summary.customer_name || 'Unknown customer'}</div>
          <div className="sub">
            Case #{summary.id} · opened {ago(summary.opened_at)} ·{' '}
            {summary.alert_count} alert{summary.alert_count === 1 ? '' : 's'} ·{' '}
            {summary.distinct_beneficiaries} destination
            {summary.distinct_beneficiaries === 1 ? '' : 's'}
            {summary.assignee_name ? ` · with ${summary.assignee_name}` : ''}
          </div>
          <div className="row wrap" style={{ marginTop: 9 }}>
            <RiskBadge level={summary.risk_level} />
            <span className={`sla sla-${summary.sla_state}`}>
              {clock(summary.sla_remaining_minutes)}
            </span>
            <span className="pill">{summary.state.replace(/_/g, ' ')}</span>
            {summary.new_device && <span className="pill escalate">new device</span>}
            {summary.declined_count > 0 && (
              <span className="pill">{summary.declined_count} declined</span>
            )}
            {(summary.channels || []).map((c) => (
              <span className="tag" key={c}>{c.replace(/_/g, ' ').toLowerCase()}</span>
            ))}
          </div>
        </div>

        <div className="exposure">
          <div className="k">Money at risk</div>
          <div className="v">{naira(summary.exposure_minor)}</div>
          <div className="n">
            {summary.attempted_minor > summary.exposure_minor
              ? `${nairaShort(summary.attempted_minor)} attempted, rest declined`
              : 'all of it went through'}
          </div>
        </div>
      </div>

      {/* -------------------------------------------- what should I do */}
      <div className={`recommend ${rec.urgency || ''}`}>
        <div className="k">
          Recommended {rec.urgency === 'now' ? '· act now' : rec.urgency === 'soon' ? '· soon' : '· routine'}
        </div>
        <div className="action">{rec.action}</div>
        <div className="because">{rec.because}</div>
      </div>

      {/* ------------------------------------------------ what happened */}
      {detail?.timeline?.length > 0 && <Timeline items={detail.timeline} />}

      {/* ------------------------------------------------- why it fired */}
      <div className="grid cols-2" style={{ marginTop: 14 }}>
        <div className="card">
          <h3>What tripped</h3>
          {!detail && <p className="dim">Loading evidence…</p>}
          {detail?.alerts?.map((a) => (
            <div key={a.id} style={{ padding: '10px 0', borderBottom: '1px solid var(--surface-2)' }}>
              <div className="between">
                <div className="row wrap">
                  <RiskBadge level={a.risk_level} />
                  <strong>{naira(a.amount_minor)}</strong>
                  <span className="muted">{a.channel.replace(/_/g, ' ')}</span>
                  {a.auth_result !== 'APPROVED' && (
                    <span className="pill">{a.auth_result}</span>
                  )}
                </div>
                <span className="mono dim" style={{ fontSize: 11 }}>{when(a.occurred_at)}</span>
              </div>
              <div className="row wrap" style={{ marginTop: 7 }}>
                {(a.signals || []).length
                  ? a.signals.map((s) => <SignalPill key={s.code} signal={s} />)
                  : <span className="dim" style={{ fontSize: 12 }}>
                      No rule fired — this is the model's own judgement.
                    </span>}
              </div>
              <details style={{ marginTop: 8 }}>
                <summary className="muted" style={{ fontSize: 12 }}>Why this decision</summary>
                <div style={{ marginTop: 10 }}>
                  <PolicyTrace trace={a.policy_trace} />
                  <h3 style={{ marginTop: 14 }}>What moved the score</h3>
                  {a.rule_only_mode
                    ? <Banner kind="warn">Scored in rule-only mode — the model was unavailable.</Banner>
                    : <Attributions attributions={a.attributions} limit={6} />}
                  <div className="mono dim" style={{ fontSize: 10.5, marginTop: 12 }}>
                    model {a.model_name ?? 'none'}:{a.model_version ?? '—'} · ruleset v{a.ruleset_version}
                    {' '}· thresholds v{a.threshold_version} · features {a.feature_spec_version}
                  </div>
                </div>
              </details>
            </div>
          ))}
        </div>

        <div>
          <div className="card" style={{ marginBottom: 12 }}>
            <h3>This customer normally</h3>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr><th>Account</th><th>Product</th><th className="num">30d txns</th><th className="num">30d value</th></tr>
                </thead>
                <tbody>
                  {detail?.baseline?.map((b) => (
                    <tr key={b.account_token}>
                      <td className="mono dim">{b.account_token.slice(0, 12)}…</td>
                      <td className="muted">{b.product_type}</td>
                      <td className="num">{b.txn_30d}</td>
                      <td className="num">{nairaShort(b.approved_value_30d_minor)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="dim" style={{ fontSize: 11, marginBottom: 0, marginTop: 8 }}>
              One customer, {detail?.baseline?.length ?? '—'} account
              {detail?.baseline?.length === 1 ? '' : 's'}. The case follows the
              customer; the baselines are per account.
            </p>
          </div>

          <div className="card">
            <h3>Notes and history</h3>
            {detail?.notes?.map((n) => (
              <div key={n.id} style={{ padding: '7px 0', borderBottom: '1px solid var(--surface-2)' }}>
                <div className="dim" style={{ fontSize: 11 }}>{n.author} · {when(n.created_at)}</div>
                <div style={{ fontSize: 13 }}>{n.body}</div>
              </div>
            ))}
            {detail?.history?.map((h, i) => (
              <div key={i} style={{ padding: '5px 0', fontSize: 12 }}>
                <span className="mono dim">{when(h.occurred_at)}</span>{' '}
                {h.action.replace(/_/g, ' ').toLowerCase()}{' '}
                <span className="muted">by {h.actor}</span>
              </div>
            ))}
            {!detail?.notes?.length && !detail?.history?.length && (
              <p className="dim" style={{ fontSize: 12, marginBottom: 0 }}>Nothing recorded yet.</p>
            )}
          </div>
        </div>
      </div>

      {/* -------------------------------------------- record the answer */}
      {!closed && (
        <div className="disposition">
          <div className="between wrap" style={{ gap: 14 }}>
            <div className="row wrap">
              {can('cases:set_outcome') ? (
                OUTCOMES.map((o) => (
                  <button key={o.key} className={`big ${o.cls}`} disabled={busy}
                          onClick={() => dispose(o.key)}>
                    {o.label} <kbd>{o.hint}</kbd>
                  </button>
                ))
              ) : (
                <span className="dim" style={{ fontSize: 12.5 }}>
                  Your role can investigate and escalate, but not set a fraud outcome.
                </span>
              )}
              {can('cases:escalate') && (
                <button disabled={busy}
                        onClick={async () => {
                          setBusy(true)
                          try { await api.escalate(summary.id, 'INFOSEC', note.trim() || null); onDisposed?.({}) }
                          catch (e) { setError(e.message) } finally { setBusy(false) }
                        }}>
                  Escalate to InfoSec
                </button>
              )}
              <button className="ghost" onClick={() => onSkip?.()}>
                Skip <kbd>n</kbd>
              </button>
            </div>
            {!can('cases:close') && can('cases:set_outcome') && (
              <span className="dim" style={{ fontSize: 11.5 }}>
                Recording an outcome leaves the case open for a Fraud Ops Lead to close.
              </span>
            )}
          </div>
          <div style={{ marginTop: 10 }}>
            <textarea placeholder="Investigation note (optional — saved with your decision)"
                      value={note} onChange={(e) => setNote(e.target.value)}
                      style={{ minHeight: 52 }} />
          </div>
        </div>
      )}
    </>
  )
}
