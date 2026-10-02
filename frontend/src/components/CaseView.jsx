import { useCallback, useEffect, useState } from 'react'

import { api, clock, naira, nairaShort, when } from '../lib/api'
import { Attributions, Banner, PolicyTrace, RiskBadge, SignalPill } from './ui'
import RegulatoryClocks from './RegulatoryClocks'
import WatchlistFlag from './WatchlistFlag'
import Timeline from './Timeline'
import Why, { VersusNormal } from './Why'
import { CONSEQUENCES, OwnershipBanner, RoleCapability } from './Actions'
import CaseFlow from './CaseFlow'
import LinkGraph from './LinkGraph'
import { CaseIcon } from './CaseWorkspace'

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

// D93: these propose. Nothing here decides anything until a lead approves it,
// and the wording has to say so — a button that reads "Confirm fraud" while it
// files a recommendation is the kind of lie a control dies of.
const OUTCOMES = [
  { key: 'CONFIRMED_FRAUD', label: 'Confirmed fraud', sub: 'The customer did not make these',
    cls: 'confirm', hint: '1',
    icon: ['M12 9v4', 'M12 17h.01', 'M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z'] },
  { key: 'FALSE_POSITIVE', label: 'No fraud found', sub: 'The activity is legitimate',
    cls: 'dismiss', hint: '2',
    icon: ['M20 6 9 17l-5-5'] },
  { key: 'INCONCLUSIVE', label: "Can't tell", sub: 'Not enough evidence either way',
    cls: 'unsure', hint: '3',
    icon: ['M12 17h.01', 'M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3', 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z'] },
]
const MIN_RATIONALE = 20

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
    if (!caseId || busy || summary.assignee_id !== user.id || summary.state === 'CLOSED' || summary.outcome || !can('cases:submit_outcome')) return
    if (note.trim().length < MIN_RATIONALE) {
      setError(`Say why in at least ${MIN_RATIONALE} characters: a lead has to decide on something.`)
      return
    }
    setBusy(true); setError(null)
    try {
      const result = await api.disposition(caseId, {
        outcome,
        note: note.trim(),
        close: can('cases:close'),
        followed_recommendation: summary.recommendation?.disposition_hint === outcome,
        expected_case_version: summary.version ?? null,
      })
      setFlash(`${outcome.replace(/_/g, ' ').toLowerCase()} proposed for case #${caseId}. `
        + 'A Fraud Ops Lead who did not write it decides; it has left your queue.')
      onDisposed?.({ ...result, outcome })
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }, [caseId, busy, note, summary, onDisposed, can])

  const take = useCallback(async () => {
    setBusy(true)
    try { await api.startReview(caseId); onDisposed?.({ refreshOnly: true }) }
    catch (e) { setError(e.message) } finally { setBusy(false) }
  }, [caseId, onDisposed])

  // Keyboard disposition. A desk working forty cases a day should not reach for
  // a mouse three times per case.
  useEffect(() => {
    function onKey(e) {
      if (e.target.closest('input, textarea, select, button, a, [contenteditable]')) return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const found = OUTCOMES.find((o) => o.hint === e.key)
      if (found && can('cases:submit_outcome')) { e.preventDefault(); dispose(found.key) }
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
      <div className="investigation-layout">
        <div className="investigation-main">
          <section className="card investigation-overview" aria-labelledby="overview-title">
            <div className="investigation-section-head"><h3 id="overview-title">Case overview</h3><span className="investigation-section-icon"><CaseIcon name="person" /></span></div>
            <div className="investigation-customer">
              <span className="investigation-avatar">{(summary.customer_name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join('')}</span>
              <div><strong>{summary.customer_name || 'Unknown customer'}</strong><span>Customer under review</span></div>
            </div>
            <dl className="investigation-fields">
              <div><dt>Opened</dt><dd title={when(summary.opened_at)}>{summary.opened_at ? when(summary.opened_at) : '—'}</dd></div>
              <div><dt>Review deadline</dt><dd>{closed ? <span className="pill">Case completed</span> : summary.sla_remaining_minutes == null ? 'Not available' : <span className={`sla sla-${summary.sla_state}`}>{clock(summary.sla_remaining_minutes)}</span>}</dd></div>
              <div><dt>Flagged transactions</dt><dd>{summary.alert_count ?? '—'}<span className="investigation-field-hint">transactions to review</span></dd></div>
              <div><dt>Destinations</dt><dd>{summary.distinct_beneficiaries ?? '—'}<span className="investigation-field-hint">linked beneficiaries</span></dd></div>
              <div><dt>Money at risk</dt><dd className="investigation-field-money">{naira(summary.exposure_minor)}</dd></div>
              <div><dt>Assigned to</dt><dd>{mine ? user.display_name || 'You' : summary.assignee_name || (summary.assignee_id ? 'Another investigator' : 'Unassigned')}</dd></div>
            </dl>
            <div className="investigation-overview-tags">
              {summary.new_device && <span className="pill escalate">New device</span>}
              {summary.declined_count > 0 && <span className="pill">{summary.declined_count} declined</span>}
              {(summary.channels || []).map((channel) => <span className="tag" key={channel}>{channel.replace(/_/g, ' ').toLowerCase()}</span>)}
              {summary.attempted_minor > summary.exposure_minor && <span className="tag">{nairaShort(summary.attempted_minor)} attempted</span>}
            </div>
          </section>
          <Why alerts={detail?.alerts} features={first?.features} signals={first?.signals}
               amountMinor={first?.amount_minor} authResult={detail?.alerts?.length === 1 ? first?.auth_result : null} />
      <section className="card investigation-transactions" id="investigation-evidence" aria-labelledby="evidence-title">
        <div className="investigation-section-head"><h3 id="evidence-title">Flagged transactions</h3><span className="pill">{summary.alert_count} alerts</span></div>
        <p className="investigation-section-sub">The activity behind this case, with its signals and decision trail.</p>
        {!detail && <p className="dim">{error ? 'Evidence could not be loaded.' : 'Loading evidence…'}</p>}
        {detail && !detail.alerts?.length && <p className="dim">No flagged transactions are available for this case.</p>}
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
      </section>

          {detail?.timeline?.length > 0 && <Timeline items={detail.timeline} />}
      <div className="investigation-comparison">
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


          <details className="investigation-connections">
            <summary><span><CaseIcon name="transfer" /> Connections &amp; related activity</span><span className="investigation-expand-hint">Explore network</span></summary>
      <div style={{ marginBottom: 14 }}>
        <LinkGraph caseId={summary.id} />
      </div>


          </details>
        </div>
        <aside className="investigation-sidebar" aria-label="Case management and safeguards">
          <section className="card investigation-management">
            <div className="investigation-section-head"><h3>Case management</h3><span className="investigation-section-icon"><CaseIcon name="cases" /></span></div>
            {rec.action && !closed && <div className={`recommend ${rec.urgency || ''}`}>
              <div className="k">Recommended next step{rec.urgency === 'now' ? ' · Act now' : rec.urgency === 'soon' ? ' · Within the hour' : ''}</div>
              <div className="action">{rec.action}</div><div className="because">{rec.because}</div>
            </div>}
            {!closed ? <OwnershipBanner summary={summary} user={user} onTake={take} busy={busy} />
              : <div className="investigation-completed"><CaseIcon name="check" /><div><strong>Investigation completed</strong><p>{summary.outcome ? summary.outcome.replace(/_/g, ' ').toLowerCase() : 'This case has been closed.'}</p></div></div>}
          </section>
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


          <Restrictions caseId={summary.id} />
      {(detail?.notes?.length > 0 || detail?.history?.length > 0) && (
        <div className="card investigation-history">
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


        </aside>
        <section className="investigation-resolution" id="investigation-resolution" aria-label="Workflow and outcome">
          <div className="investigation-workflow">
        <CaseFlow key={`${summary.id}:${summary.assignee_id}`} caseId={summary.id} user={user} onChanged={() => onDisposed?.({ refreshOnly: true })} />
          </div>
      {!closed && !summary.outcome && (
        <div className="disposition" aria-labelledby="outcome-title">
          <div className="disposition-head">
            <div>
              <h3 id="outcome-title">Investigation outcome</h3>
              <span className="dim" style={{ fontSize: 12 }}>
                A different team lead reviews your proposal.
              </span>
            </div>
            <button className="ghost" onClick={() => onSkip?.()}>Skip <kbd>n</kbd></button>
          </div>

          <RoleCapability user={user} />
<label className="investigation-note-label" htmlFor="investigation-rationale">Your findings</label>
          <textarea id="investigation-rationale" className="decision-note"
                    placeholder={`Why? The lead decides on this — at least ${MIN_RATIONALE} characters.`}
                    value={note} onChange={(e) => setNote(e.target.value)} />


          {can('cases:submit_outcome') ? (
            <div className="decisions">
              {OUTCOMES.map((o) => (
                <button key={o.key} className={`decision ${o.cls}`} disabled={busy || !mine}
                        title={!mine ? 'Take the case first' : CONSEQUENCES[o.key]}
                        onMouseEnter={() => setHovered(o.key)}
                        onMouseLeave={() => setHovered(null)}
                        onClick={() => dispose(o.key)}>
                  <svg className="decision-ico" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"
                       strokeLinejoin="round" aria-hidden="true">
                    {o.icon.map((d, i) => <path key={i} d={d} />)}
                  </svg>
                  <span className="decision-text">
                    <span className="decision-label">{o.label}</span>
                    <span className="decision-sub">{o.sub}</span>
                  </span>
                  <kbd>{o.hint}</kbd>
                </button>
              ))}
            </div>
          ) : (
            <p className="dim" style={{ fontSize: 12.5, margin: '6px 0 0' }}>
              Your role can investigate and send a case for specialist help, but cannot propose a final result.
            </p>
          )}

          <p className="decision-hint">
            {hovered
              ? CONSEQUENCES[hovered]
              : mine
                ? 'Hover over a decision to see what it does. To ask a specialist for help, use Send for help in the steps above.'
                : 'Take the case above before proposing an outcome.'}
          </p>

          <div className="decision-foot">
            <span className={note.trim().length >= MIN_RATIONALE ? 'ok-text' : 'dim'}>
              {note.trim().length}/{MIN_RATIONALE}
            </span>
            <span className="dim">Your name and reason are recorded with the proposal.</span>
          </div>
        </div>
      )}
        </section>
      </div>
    </>
  )
}

/**
 * D97: the restrictions a lead approved on this case, and what the bank did.
 * Risk Radar restricts nothing itself — it recommends, dispatches to the bank,
 * and records the outcome — so the wording says "recommended", never "applied".
 */
const RESTRICTION_LABELS = {
  DEBIT_RESTRICTION: 'Stop debits',
  CHANNEL_RESTRICTION: 'Block a channel',
  CARD_FREEZE: 'Freeze the card',
  BENEFICIARY_RESTRICTION: 'Block a destination',
}
const RESTRICTION_STATUS = {
  RECOMMENDED: ['queued for the bank', ''],
  DELIVERED: ['sent to the bank, awaiting confirmation', ''],
  APPLIED: ['the bank applied it', 'suppress'],
  NOT_APPLIED: ['the bank did not apply it', 'escalate'],
  REJECTED: ['the bank declined it', 'override'],
}

function Restrictions({ caseId }) {
  const [items, setItems] = useState(null)
  useEffect(() => {
    let cancelled = false
    api.caseRestrictions(caseId)
      .then((d) => !cancelled && setItems(d.items))
      .catch(() => !cancelled && setItems([]))
    return () => { cancelled = true }
  }, [caseId])

  if (!items || !items.length) return null
  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <h3>Restrictions asked of the bank</h3>
      <p className="dim" style={{ fontSize: 12, marginTop: 0 }}>
        Risk Radar suggests the action and records it. The bank applies and confirms it.
      </p>
      {items.map((r) => {
        const [text, cls] = RESTRICTION_STATUS[r.status] || [r.status, '']
        return (
          <div key={r.restriction_ref} className="between" style={{ padding: '8px 0', borderBottom: '1px solid var(--bg-2)' }}>
            <div>
              <strong>{RESTRICTION_LABELS[r.action] || r.action}</strong>
              {r.channel && <span className="muted"> · {r.channel.replace(/_/g, ' ').toLowerCase()}</span>}
              {r.reason && <div className="dim" style={{ fontSize: 12 }}>{r.reason}</div>}
            </div>
            <span className={`pill ${cls}`}>{text}</span>
          </div>
        )
      })}
    </div>
  )
}
