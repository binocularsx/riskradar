import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, naira, nairaShort, when } from '../lib/api'
import { BANK_ACTION, CHANNEL, DELIVERY, EVENT, OUTCOME, PROPOSAL_STATE, ROLE, SUPPORT_MESSAGE, say, sayLower } from '../lib/words'
import { Banner, Empty, RiskBadge } from '../components/ui'
import { CaseIcon } from '../components/CaseWorkspace'

/**
 * Completed cases and their decision records, for leads.
 *
 * A lead answers for what the desk decided, often weeks later and to someone
 * outside it: an auditor, the regulator, a complaint, the support team. The
 * log lists every closed case; opening one shows its decision record — who
 * proposed the outcome and why, who approved it and why, what support was
 * asked and what it answered, and the case's entries in the audit log.
 */

const TABS = [
  { key: '', label: 'All' },
  { key: 'CONFIRMED_FRAUD', label: 'Fraud confirmed' },
  { key: 'FALSE_POSITIVE', label: 'No fraud found' },
  { key: 'INCONCLUSIVE', label: 'More review needed' },
]
const PAGE = 25
const ACK = { APPLIED: 'Done by support', NOT_APPLIED: 'Not done by support', REJECTED: 'Refused by support' }
const outcomeTone = (o) => (o === 'CONFIRMED_FRAUD' ? 'escalate' : o === 'FALSE_POSITIVE' ? 'suppress' : '')

function duration(from, to) {
  if (!from || !to) return '—'
  const hours = (new Date(to) - new Date(from)) / 36e5
  return hours < 48 ? `${Math.max(1, Math.round(hours))}h` : `${Math.round(hours / 24)} days`
}

export default function CompletedCases() {
  const [outcome, setOutcome] = useState('')
  const [q, setQ] = useState('')
  const [term, setTerm] = useState('')
  const [page, setPage] = useState(0)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)

  useEffect(() => { const t = setTimeout(() => { setTerm(q.trim()); setPage(0) }, 250); return () => clearTimeout(t) }, [q])

  const load = useCallback(() => {
    const params = { limit: PAGE, offset: page * PAGE }
    if (outcome) params.outcome = outcome
    if (term) params.q = term
    setError(null)
    api.completedCases(params).then(setData).catch((e) => setError(e.message))
  }, [outcome, term, page])
  useEffect(() => { load() }, [load])

  const open = (id) => {
    setSelected(id)
    setTimeout(() => document.getElementById('decision-record')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50)
  }
  const totals = data?.totals || {}
  const all = Object.values(totals).reduce((a, b) => a + b, 0)

  return (
    <div className="page ops-dashboard cases-workspace">
      <div className="page-head ops-page-head">
        <div>
          <div className="ops-eyebrow">Case work</div>
          <h1>Completed cases</h1>
          <p className="page-sub">Every closed case, and the record of how it was decided.</p>
        </div>
      </div>

      {error && <Banner kind="error">{error} <button className="ghost" onClick={load}>Retry</button></Banner>}

      {selected && <DecisionRecord key={selected} caseId={selected} onClose={() => setSelected(null)} />}

      <section className="card cases-panel" aria-labelledby="completed-title">
        <div className="cases-panel-head">
          <div><h2 id="completed-title">Case log</h2>
            <p>{data ? `${all} closed · ${totals.CONFIRMED_FRAUD || 0} fraud confirmed · ${totals.FALSE_POSITIVE || 0} no fraud found` : 'Loading…'}</p></div>
        </div>
        <div className="case-tabs" role="group" aria-label="Outcome">
          {TABS.map((t) => <button key={t.key} className={outcome === t.key ? 'on' : ''} aria-pressed={outcome === t.key}
            onClick={() => { setOutcome(t.key); setPage(0) }}>{t.label}</button>)}
        </div>
        <div className="cases-filterbar">
          <div className="searchbar cases-search">
            <CaseIcon name="search" />
            <input type="search" aria-label="Search completed cases" placeholder="Case number or support ticket…"
              value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
        </div>
        <div className="table-scroll">
          <table className="rowtable cases-table" aria-label="Completed cases">
            <thead><tr>
              <th>Case</th><th>Result</th><th className="num">Value</th><th>Proposed by</th><th>Approved by</th>
              <th>Closed</th><th>Open for</th>
            </tr></thead>
            <tbody>
              {(data?.items || []).map((c) => (
                <tr key={c.id} className={`clickable ${selected === c.id ? 'selected' : ''}`} tabIndex={0}
                    title="Open the decision record" onClick={() => open(c.id)} onKeyDown={(e) => e.key === 'Enter' && open(c.id)}>
                  <td><strong>CASE-{c.id}</strong> <RiskBadge level={c.risk_level} />
                    <span className="case-cell-sub">{c.reported_by_support ? `Reported by support${c.support_ticket_ref ? ` · ${c.support_ticket_ref}` : ''}` : 'Raised by Risk Radar'}</span></td>
                  <td><span className={`pill ${outcomeTone(c.outcome)}`}>{say(OUTCOME, c.outcome) || 'No result recorded'}</span></td>
                  <td className="num">{nairaShort(c.exposure_minor)}</td>
                  <td>{c.proposed_by || c.recorded_by || <span className="dim">—</span>}</td>
                  <td>{c.approved_by || <span className="dim" title="Decided before proposals needed a second person">Recorded directly</span>}</td>
                  <td className="dim">{when(c.closed_at)}<span className="case-cell-sub">by {c.closed_by || '—'}</span></td>
                  <td className="dim">{duration(c.opened_at, c.closed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {data && !data.items.length && <Empty>{term || outcome ? 'No closed cases match.' : 'No case has been closed yet.'}</Empty>}
        </div>
        <div className="cases-table-foot">
          <span>{data?.items.length ? `Showing ${page * PAGE + 1}–${page * PAGE + data.items.length}` : ''}</span>
          <div className="cases-pagination">
            <button aria-label="Previous page" disabled={page === 0} onClick={() => setPage(page - 1)}>‹</button>
            <span>Page {page + 1}</span>
            <button aria-label="Next page" disabled={!data?.has_more} onClick={() => setPage(page + 1)}>›</button>
          </div>
        </div>
      </section>
    </div>
  )
}

function DecisionRecord({ caseId, onClose }) {
  const [rec, setRec] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => { api.decisionRecord(caseId).then(setRec).catch((e) => setError(e.message)) }, [caseId])

  if (error) return <section className="card"><Banner kind="error">{error}</Banner><button className="ghost" onClick={onClose}>Close</button></section>
  if (!rec) return <section className="card"><p className="muted">Loading the decision record…</p></section>
  const c = rec.case
  const d = rec.decision
  const milestones = [
    ['Customer reported it', c.first_reported_at, c.report_channel ? `via ${sayLower(CHANNEL, c.report_channel)}` : null],
    ['Complaint acknowledged', c.acknowledged_at],
    ['Receiving bank told', c.counterparty_notified_at, c.counterparty_institution],
    ['Investigation concluded', c.investigation_concluded_at],
    ['Customer reimbursed', c.reimbursed_at],
  ].filter(([, at]) => at)

  return (
    <section className="card payment-check" id="decision-record" aria-labelledby="record-title">
      <div className="between wrap" style={{ gap: 8 }}>
        <div>
          <div className="ops-eyebrow">Decision record</div>
          <h2 id="record-title" style={{ margin: '2px 0' }}>CASE-{c.id}: {say(OUTCOME, c.outcome) || 'No result recorded'}</h2>
          <p className="dim" style={{ margin: 0 }}>
            Opened {when(c.opened_at)} · closed {when(c.closed_at)} by {c.closed_by || '—'} · {naira(c.exposure_minor)} in {c.alert_count} flagged payment(s)
            {c.support_ticket_ref ? ` · support ticket ${c.support_ticket_ref}` : ''}
          </p>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <Link className="ops-action" to={`/cases/${c.id}`}>Open the case <CaseIcon name="arrow" /></Link>
          <button className="ghost" onClick={onClose}>Close</button>
        </div>
      </div>

      <div className="grid cols-2" style={{ gap: 14, alignItems: 'start', marginTop: 14 }}>
        <div>
          <h3 style={{ marginTop: 0 }}>The decision</h3>
          {d.how === 'APPROVED_PROPOSAL' ? (
            <ol style={{ paddingLeft: 18, fontSize: 13, margin: 0 }}>
              <li style={{ marginBottom: 10 }}>
                <strong>{d.proposed_by}</strong> proposed <strong>{sayLower(OUTCOME, d.outcome)}</strong> on {when(d.proposed_at)}.
                <div className="dim" style={{ marginTop: 2 }}>“{d.rationale}”</div>
              </li>
              <li>
                <strong>{d.approved_by}</strong> approved it on {when(d.approved_at)}.
                {d.approval_reason && <div className="dim" style={{ marginTop: 2 }}>“{d.approval_reason}”</div>}
              </li>
            </ol>
          ) : (
            <p style={{ fontSize: 13, margin: 0 }}>
              <strong>{d.recorded_by || 'Unknown'}</strong> recorded <strong>{sayLower(OUTCOME, d.outcome)}</strong>
              {d.recorded_at ? ` on ${when(d.recorded_at)}` : ''}, in one step, before a second person's approval was required.
              <span className="dim"> {d.rationale ? `“${d.rationale}”` : 'No reason was written down.'}</span>
            </p>
          )}
          {c.missed_by_detector > 0 && (
            <p className="dim" style={{ fontSize: 12.5 }}>{c.missed_by_detector} payment(s) on this case were reported by the customer, not flagged by Risk Radar.</p>
          )}
          {c.escalated_at && (
            <p className="dim" style={{ fontSize: 12.5 }}>Sent to a specialist on {when(c.escalated_at)}{c.escalation_reason ? `: “${c.escalation_reason}”` : ''}.</p>
          )}

          {rec.proposals.length > 1 && (
            <>
              <h3>Every proposal</h3>
              <ul style={{ paddingLeft: 18, fontSize: 12.5, margin: 0 }}>
                {rec.proposals.map((p) => (
                  <li key={p.id} style={{ marginBottom: 6 }}>
                    {when(p.submitted_at)}: {p.proposed_by} proposed {sayLower(OUTCOME, p.proposed_outcome)} ·{' '}
                    <strong>{say(PROPOSAL_STATE, p.state)}</strong>{p.decided_by ? ` by ${p.decided_by}` : ''}
                    {p.decision_reason && <span className="dim"> — “{p.decision_reason}”</span>}
                  </li>
                ))}
              </ul>
            </>
          )}

          {milestones.length > 0 && (
            <>
              <h3>Regulatory milestones</h3>
              <ul style={{ paddingLeft: 18, fontSize: 12.5, margin: 0 }}>
                {milestones.map(([label, at, note]) => <li key={label}>{label}: {when(at)}{note ? ` (${note})` : ''}</li>)}
              </ul>
            </>
          )}
        </div>

        <div>
          <h3 style={{ marginTop: 0 }}>What support was asked</h3>
          {rec.actions.length ? (
            <ul style={{ paddingLeft: 18, fontSize: 12.5, margin: 0 }}>
              {rec.actions.map((a) => (
                <li key={a.restriction_ref} style={{ marginBottom: 6 }}>
                  <strong>{a.kind === 'RELEASE' ? 'Lift: ' : ''}{say(BANK_ACTION, a.action)}</strong>
                  {a.transaction_ref ? <span className="dim"> · payment {a.transaction_ref}</span> : null}
                  <div className="dim">
                    {a.acknowledged_at ? `${ACK[a.ack_outcome] || a.ack_outcome}, ${when(a.acknowledged_at)}` : a.first_delivered_at ? 'With support, no answer yet' : 'Not yet sent'}
                    {a.ack_reason ? ` — “${a.ack_reason}”` : ''}
                  </div>
                </li>
              ))}
            </ul>
          ) : <p className="dim" style={{ fontSize: 12.5 }}>Nothing was asked of support on this case.</p>}

          <h3>Messages to support</h3>
          {rec.messages.length ? (
            <ul style={{ paddingLeft: 18, fontSize: 12.5, margin: 0 }}>
              {rec.messages.map((m) => (
                <li key={m.report_ref}>{say(SUPPORT_MESSAGE, m.kind)}{m.outcome ? `: ${sayLower(OUTCOME, m.outcome)}` : ''} ·{' '}
                  <span className="dim">{say(DELIVERY, m.status)}, {when(m.sent_at || m.created_at)}</span></li>
              ))}
            </ul>
          ) : <p className="dim" style={{ fontSize: 12.5 }}>Nothing was sent to support about this case.</p>}
        </div>
      </div>

      <details style={{ marginTop: 14 }}>
        <summary><strong>Full activity log</strong> <span className="dim">({rec.trail.length} entries, from the tamper-evident audit log)</span></summary>
        <div className="table-scroll" style={{ marginTop: 8 }}>
          <table>
            <thead><tr><th>When</th><th>Who</th><th>What happened</th></tr></thead>
            <tbody>
              {rec.trail.map((e, i) => (
                <tr key={i}>
                  <td className="dim">{when(e.occurred_at)}</td>
                  <td>{e.actor_role === 'SYSTEM' ? 'Risk Radar (automatic)' : <>{e.actor || '—'} <span className="dim">({sayLower(ROLE, e.actor_role)})</span></>}</td>
                  <td>{say(EVENT, e.action)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </section>
  )
}
