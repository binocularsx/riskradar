import { useCallback, useEffect, useState } from 'react'

import { ago, api, clock, naira, nairaShort, when } from '../lib/api'
import { Attributions, Banner, PolicyTrace, RiskBadge, SignalPill } from './ui'
import RegulatoryClocks from './RegulatoryClocks'
import WatchlistFlag from './WatchlistFlag'
import Timeline from './Timeline'
import Why, { VersusNormal } from './Why'
import { CONSEQUENCES, NextSteps, OwnershipBanner, RoleCapability } from './Actions'

/**
 * The investigation panel — everything needed to decide, on one screen, in the
 * order an analyst actually thinks:
 *
 *   whose is it        →  who owns it       →  how much is at risk
 *   →  what should I do →  why did it fire  →  what does the activity look like
 *   →  record the answer
 *
 * The first version showed the evidence and left the analyst to work out what
 * to do with it. Everything added since is aimed at one complaint: *my actions
 * are not clear and I cannot tell why the alert happened.*
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
  const [hovered, setHovered] = useState(null)

  const can = (p) => user.permissions.includes(p)
  const caseId = summary?.id

  useEffect(() => {
    let cancelled = false
    setDetail(null); setNote(''); setError(null); setFlash(null)
    if (!caseId) return undefined
    api.caseDetail(caseId)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setError(e.message))
    return () => { cancelled = true }
  }, [caseId])

  const dispose = useCallback(async (outcome) => {
    if (!caseId || busy) return
    setBusy(true); setError(null)
    try {
      const result = await api.disposition(caseId, {
        outcome,
        note: note.trim() || null,
        close: can('cases:close'),
        followed_recommendation: summary.recommendation?.disposition_hint === outcome,
      })
      setFlash(result.closed
        ? `Case #${caseId} closed as ${outcome.replace(/_/g, ' ').toLowerCase()}.`
        : `Outcome recorded. A Fraud Ops Lead will close it.`)
      onDisposed?.(result)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }, [caseId, busy, note, summary, onDisposed, can])

  const take = useCallback(async () => {
    setBusy(true)
    try { await api.startReview(caseId); onDisposed?.({}) }
    catch (e) { setError(e.message) } finally { setBusy(false) }
  }, [caseId, onDisposed])

  // Keyboard disposition. A desk working forty cases a day should not reach for
  // a mouse three times per case.
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
        Press <strong>Start reviewing</strong> above and the system will hand you
        the case that matters most right now.
      </div>
    )
  }

  const rec = summary.recommendation || {}
  const closed = summary.state === 'CLOSED'
  const first = detail?.alerts?.[0]
  const mine = summary.assignee_id === user.id

  return (
    <>
      {flash && <Banner kind="ok">{flash}</Banner>}
      {error && <Banner kind="error">{error}</Banner>}

      {/* ------------------------------------------------- 1. who owns it */}
      {!closed && (
        <OwnershipBanner summary={summary} user={user} onTake={take} busy={busy} />
      )}

      {/* ------------------------------- 2. whose case, and how much money */}
      <div className="casehead">
        <div>
          <div className="who">{summary.customer_name || 'Unknown customer'}</div>
          <div className="sub">
            Case #{summary.id} · opened {ago(summary.opened_at)} ·{' '}
            {summary.alert_count} flagged transaction
            {summary.alert_count === 1 ? '' : 's'} ·{' '}
            {summary.distinct_beneficiaries} destination
            {summary.distinct_beneficiaries === 1 ? '' : 's'}
          </div>
          <div className="row wrap" style={{ marginTop: 10 }}>
            <RiskBadge level={summary.risk_level} />
            <span className={`sla sla-${summary.sla_state}`}>
              {clock(summary.sla_remaining_minutes)}
            </span>
            <span className="pill">{summary.state.replace(/_/g, ' ').toLowerCase()}</span>
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
              ? `${nairaShort(summary.attempted_minor)} attempted — the rest was declined`
              : 'all of this went through'}
          </div>
        </div>
      </div>

      {/* --------------------------------------- 3. what should I do now */}
      <div className={`recommend ${rec.urgency || ''}`}>
        <div className="k">
          Recommended
          {rec.urgency === 'now' ? ' · act now'
            : rec.urgency === 'soon' ? ' · within the hour' : ' · routine'}
        </div>
        <div className="action">{rec.action}</div>
        <div className="because">{rec.because}</div>
      </div>

      <div className="grid cols-2" style={{ marginBottom: 14 }}>
        <NextSteps recommendation={rec} />
        <Why
          alerts={detail?.alerts}
          features={first?.features}
          signals={first?.signals}
          amountMinor={first?.amount_minor}
          authResult={detail?.alerts?.length === 1 ? first?.auth_result : null}
        />
      </div>

      {/* ------------------------- what the bank owes, once the customer reports */}
      {detail && (
        <RegulatoryClocks
          caseRow={detail.case}
          clocks={detail.clocks || []}
          user={user}
          onUpdated={(r) => setDetail((d) => ({ ...d, case: r.case, clocks: r.clocks }))}
        />
      )}
      {detail && (
        <WatchlistFlag
          caseRow={detail.case}
          flags={detail.watchlist || []}
          identity={detail.identity}
          industryFlags={detail.industry_flags || []}
          user={user}
          onUpdated={() => api.caseDetail(caseId).then(setDetail).catch((e) => setError(e.message))}
        />
      )}

      {/* ------------------------------------ 4. what the activity looks like */}
      {detail?.timeline?.length > 0 && <Timeline items={detail.timeline} />}

      <div className="grid cols-2" style={{ marginTop: 14 }}>
        <VersusNormal alerts={detail?.alerts} features={first?.features}
                        amountMinor={first?.amount_minor} />

        <div className="card">
          <h3>This customer's accounts</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>Account</th><th>Product</th><th className="num">30d count</th><th className="num">30d value</th></tr>
              </thead>
              <tbody>
                {detail?.baseline?.map((b) => (
                  <tr key={b.account_token}>
                    <td className="mono dim">…{b.account_token.slice(-8)}</td>
                    <td className="muted">{b.product_type.toLowerCase()}</td>
                    <td className="num">{b.txn_30d}</td>
                    <td className="num">{nairaShort(b.approved_value_30d_minor)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="dim" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>
            One customer, {detail?.baseline?.length ?? '—'} account
            {detail?.baseline?.length === 1 ? '' : 's'}. The case follows the
            <strong> customer</strong>, so an attacker moving between their own
            accounts stays one investigation rather than becoming three.
          </p>
        </div>
      </div>

      {/* --------------------------------------------- 5. the detail, on demand */}
      <div className="card" style={{ marginTop: 14 }}>
        <h3>Every flagged transaction on this case</h3>
        {!detail && <p className="dim">Loading…</p>}
        {detail?.alerts?.map((a) => (
          <div key={a.id} style={{ padding: '11px 0', borderBottom: '1px solid var(--bg-2)' }}>
            <div className="between">
              <div className="row wrap">
                <RiskBadge level={a.risk_level} />
                <strong>{naira(a.amount_minor)}</strong>
                {a.direction === 'INBOUND' && (
                  <span className="pill suppress" title={`Credit from bank ${a.remitter_bank_code || 'unknown'}`}>incoming credit</span>
                )}
                <span className="muted">{a.channel.replace(/_/g, ' ').toLowerCase()}</span>
                {a.auth_result !== 'APPROVED' && <span className="pill">{a.auth_result.toLowerCase()}</span>}
              </div>
              <span className="mono dim" style={{ fontSize: 11 }}>{when(a.occurred_at)}</span>
            </div>
            <div className="row wrap" style={{ marginTop: 7 }}>
              {(a.signals || []).length
                ? a.signals.map((s) => <SignalPill key={s.code} signal={s} />)
                : <span className="dim" style={{ fontSize: 12 }}>
                    No written rule fired — this is the model's own judgement.
                  </span>}
            </div>
            <details style={{ marginTop: 8 }}>
              <summary className="muted" style={{ fontSize: 12 }}>
                Show the full decision trail
              </summary>
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

      {(detail?.notes?.length > 0 || detail?.history?.length > 0) && (
        <div className="card" style={{ marginTop: 14 }}>
          <h3>Notes and history</h3>
          {detail.notes.map((n) => (
            <div key={n.id} style={{ padding: '7px 0', borderBottom: '1px solid var(--bg-2)' }}>
              <div className="dim" style={{ fontSize: 11 }}>{n.author} · {when(n.created_at)}</div>
              <div style={{ fontSize: 13 }}>{n.body}</div>
            </div>
          ))}
          {detail.history.map((h, i) => (
            <div key={i} style={{ padding: '5px 0', fontSize: 12 }}>
              <span className="mono dim">{when(h.occurred_at)}</span>{' '}
              {h.action.replace(/_/g, ' ').toLowerCase()}{' '}
              <span className="muted">by {h.actor}</span>
            </div>
          ))}
        </div>
      )}

      {/* --------------------------------------------- 6. record the answer */}
      {!closed && (
        <div className="disposition">
          <RoleCapability user={user} />
          <div className="between wrap" style={{ gap: 14 }}>
            <div className="row wrap">
              {can('cases:set_outcome') ? OUTCOMES.map((o) => (
                <button key={o.key} className={`big ${o.cls}`} disabled={busy || !mine}
                        title={!mine ? 'Take the case first' : CONSEQUENCES[o.key]}
                        onMouseEnter={() => setHovered(o.key)}
                        onMouseLeave={() => setHovered(null)}
                        onClick={() => dispose(o.key)}>
                  {o.label} <kbd>{o.hint}</kbd>
                </button>
              )) : (
                <span className="dim" style={{ fontSize: 12.5 }}>
                  Your role can investigate and escalate, but not set a fraud outcome.
                </span>
              )}
              {can('cases:escalate') && (
                <button disabled={busy} onClick={async () => {
                  setBusy(true)
                  try { await api.escalate(summary.id, 'INFOSEC', note.trim() || null); onDisposed?.({}) }
                  catch (e) { setError(e.message) } finally { setBusy(false) }
                }}>Escalate to InfoSec</button>
              )}
              <button className="ghost" onClick={() => onSkip?.()}>Skip <kbd>n</kbd></button>
            </div>
          </div>

          <p className="dim" style={{ fontSize: 12, margin: '10px 0 0', minHeight: 32 }}>
            {hovered
              ? CONSEQUENCES[hovered]
              : mine
                ? 'Hover a button to see exactly what it does. Your answer becomes training data for the next model.'
                : 'Take the case above before recording an outcome.'}
          </p>

          <textarea style={{ marginTop: 8, minHeight: 52 }}
                    placeholder="Investigation note (optional — saved with your decision)"
                    value={note} onChange={(e) => setNote(e.target.value)} />
        </div>
      )}
    </>
  )
}
