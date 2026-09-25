import { useCallback, useEffect, useState } from 'react'

import { api, when } from '../lib/api'
import { Banner } from './ui'

/**
 * Where this case is on its way to resolution, and the work that gets it there (D83).
 *
 *   stage bar      NEW → IN REVIEW → (ESCALATED) → AWAITING CLOSE → CLOSED
 *   next           one sentence: what happens next and who does it
 *   steps          the recommendation as a checklist; each step records what was
 *                  done in the bank's systems, with its result
 *   escalate       when to, what happens when you do, and a required reason
 *   hand back      for a lead holding an escalated case
 *   close          for a lead, once the outcome is recorded
 *   history        who did what, when
 *
 * Risk Radar never blocks a card or holds money itself (D7). "Record" means
 * "I did this in the bank's system, and here is what happened".
 */

export default function CaseFlow({ caseId, user, onChanged }) {
  const [flow, setFlow] = useState(null)
  const [catalog, setCatalog] = useState(null)
  const [open, setOpen] = useState(null)          // step action being recorded
  const [result, setResult] = useState('')
  const [detail, setDetail] = useState('')
  const [escalating, setEscalating] = useState(false)
  const [target, setTarget] = useState('FRAUD_OPS')
  const [reason, setReason] = useState('')
  const [returning, setReturning] = useState(false)
  const [findings, setFindings] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [done, setDone] = useState(null)
  const can = (p) => user.permissions.includes(p)

  const load = useCallback(() => api.caseWorkflow(caseId).then(setFlow).catch((e) => setError(e.message)), [caseId])
  useEffect(() => { setFlow(null); setOpen(null); setEscalating(false); setReturning(false); setDone(null); load() }, [load])
  useEffect(() => { api.workflowCatalog().then(setCatalog).catch(() => {}) }, [])

  const act = async (fn, message) => {
    setBusy(true); setError(null)
    try {
      await fn()
      setDone(message)
      await load()
      onChanged?.()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (!flow || !catalog) return <div className="card"><p className="dim" style={{ margin: 0 }}>Loading the case workflow…</p></div>

  const mine = flow.assignee_id === user.id
  const stage = flow.stage
  const closedOrWaiting = stage === 'CLOSED' || stage === 'AWAITING_CLOSE'
  const canReturn = can('cases:escalate') && flow.escalation && flow.escalation.by_id !== user.id
                    && mine && stage === 'IN_REVIEW'
  const esc = catalog.escalation[target]

  return (
    <div className="card caseflow">
      {error && <Banner kind="error">{error}</Banner>}
      {done && <Banner kind="ok">{done}</Banner>}

      <div className="stagebar">
        {flow.stages.map((s, i) => (
          <div key={s.key} className={`stage ${s.state}`}>
            <span className="dot">{s.state === 'done' ? '✓' : i + 1}</span>
            <span className="name">{s.label}</span>
          </div>
        ))}
      </div>
      <p className="next"><strong>Next:</strong> {flow.next}
        {flow.assignee && <span className="dim"> · with {flow.assignee}</span>}</p>
      {flow.escalation && (
        <div className="escalation-note">
          Escalated to <strong>{flow.escalation.to === 'INFOSEC' ? 'InfoSec (retired)' : flow.escalation.to === 'FRAUD_OPS' ? 'Fraud Ops' : 'a team, since handed back'}</strong> by{' '}
          {flow.escalation.by} · {when(flow.escalation.at)}
          <div className="muted">“{flow.escalation.reason}”</div>
        </div>
      )}

      <h3 style={{ marginTop: 16 }}>What to do, step by step</h3>
      <ol className="steps">
        {flow.steps.map((s, i) => {
          const spec = catalog.actions[s.action]
          const recorded = flow.actions.filter((a) => a.action_code === s.action)
          return (
            <li key={i} className={`step ${s.status}`}>
              <div className="between wrap" style={{ gap: 8 }}>
                <span>
                  <span className="tick">{s.status === 'done' ? '✓' : s.status === 'info' ? 'i' : '○'}</span>
                  {s.text}
                </span>
                {!closedOrWaiting && s.status !== 'info' && (
                  s.action === 'OUTCOME' ? (
                    <span className="dim" style={{ fontSize: 12 }}>{s.status === 'done' ? 'recorded' : 'use the buttons at the bottom'}</span>
                  ) : s.action === 'ESCALATE' ? (
                    s.status === 'done' ? <span className="dim" style={{ fontSize: 12 }}>escalated</span> :
                      can('cases:escalate') && <button onClick={() => { setEscalating(true); setTarget('FRAUD_OPS') }} disabled={!mine || busy}
                                                       title={mine ? '' : 'Take the case first'}>Escalate…</button>
                  ) : spec && (
                    <button onClick={() => { setOpen(s.action); setResult(''); setDetail('') }}
                            disabled={!mine || busy} title={mine ? spec.label : 'Take the case first'}>
                      {recorded.length ? 'Record again' : 'Record'}
                    </button>
                  )
                )}
              </div>
              {recorded.map((a) => (
                <div key={a.id} className="recorded">
                  {spec?.results[a.result] || a.result}{a.detail ? ` — ${a.detail}` : ''} · {a.actor}, {when(a.created_at)}
                </div>
              ))}
              {open === s.action && spec && (
                <div className="record-form">
                  {spec.hint && <p className="dim" style={{ margin: '0 0 8px', fontSize: 12 }}>{spec.hint}</p>}
                  <div className="row wrap" style={{ gap: 6 }}>
                    {Object.entries(spec.results).map(([k, label]) => (
                      <button key={k} className={result === k ? 'primary' : ''} onClick={() => setResult(k)}>{label}</button>
                    ))}
                  </div>
                  <input style={{ marginTop: 8, width: '100%' }} placeholder="Detail (optional): reference number, who you spoke to"
                         value={detail} onChange={(e) => setDetail(e.target.value)} />
                  <div className="row" style={{ marginTop: 8 }}>
                    <button className="primary" disabled={!result || busy}
                            onClick={() => act(() => api.recordAction(caseId, { action_code: s.action, result, detail: detail || null }),
                                               `Recorded: ${spec.label.toLowerCase()} — ${spec.results[result].toLowerCase()}.`)
                              .then(() => setOpen(null))}>
                      Save
                    </button>
                    <button className="ghost" onClick={() => setOpen(null)}>Cancel</button>
                  </div>
                </div>
              )}
            </li>
          )
        })}
      </ol>

      {stage === 'AWAITING_APPROVAL' && (
        <div className="dialog">
          <strong>Filed. Waiting for a lead.</strong> A lead who did not file this has to
          approve it (D93). Nothing has been sent to the bank and no account manager has
          been told until they do.
        </div>
      )}

      {!closedOrWaiting && stage !== 'ESCALATED' && can('cases:escalate') && !escalating && (
        <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
          <button onClick={() => setEscalating(true)} disabled={!mine || busy} title={mine ? '' : 'Take the case first'}>
            Escalate…
          </button>
          {canReturn && <button onClick={() => setReturning(true)} disabled={busy}>Hand back with findings…</button>}
        </div>
      )}

      {escalating && (
        <div className="dialog">
          <h3>Escalate this case</h3>
          <div className="segmented" style={{ marginBottom: 10 }}>
            {Object.entries(catalog.escalation).map(([k, v]) => (
              <button key={k} className={target === k ? 'on' : ''} onClick={() => setTarget(k)}>{v.label}</button>
            ))}
          </div>
          <div className="grid cols-2">
            <div>
              <div className="stat-label">Escalate to {esc.label} when</div>
              <ul className="tight">{esc.when.map((t) => <li key={t}>{t}</li>)}</ul>
            </div>
            <div>
              <div className="stat-label">What happens when you do</div>
              <ul className="tight">{esc.what_happens.map((t) => <li key={t}>{t}</li>)}</ul>
            </div>
          </div>
          <textarea style={{ width: '100%', minHeight: 60, marginTop: 8 }} value={reason} onChange={(e) => setReason(e.target.value)}
                    placeholder={`Why ${esc.label} needs this case (required — they read this first)`} />
          <div className="row" style={{ marginTop: 8 }}>
            <button className="primary" disabled={reason.trim().length < 5 || busy}
                    onClick={() => act(() => api.escalate(caseId, target, reason.trim()),
                                       `Escalated to ${esc.label}. It has left your queue; you will get it back with their findings.`)
                      .then(() => { setEscalating(false); setReason('') })}>
              Escalate to {esc.label}
            </button>
            <button className="ghost" onClick={() => setEscalating(false)}>Cancel</button>
          </div>
        </div>
      )}

      {returning && (
        <div className="dialog">
          <h3>Hand back to {flow.escalation?.by}</h3>
          <p className="dim" style={{ fontSize: 12, marginTop: 0 }}>
            The case goes back to the analyst who escalated it, with your findings as a note. They record the outcome.
          </p>
          <textarea style={{ width: '100%', minHeight: 60 }} value={findings} onChange={(e) => setFindings(e.target.value)}
                    placeholder="What you found (required)" />
          <div className="row" style={{ marginTop: 8 }}>
            <button className="primary" disabled={findings.trim().length < 5 || busy}
                    onClick={() => act(() => api.returnCase(caseId, findings.trim()), 'Handed back with your findings.')
                      .then(() => { setReturning(false); setFindings('') })}>
              Hand back
            </button>
            <button className="ghost" onClick={() => setReturning(false)}>Cancel</button>
          </div>
        </div>
      )}

      {stage === 'AWAITING_CLOSE' && can('cases:close') && (
        <div className="dialog">
          <strong>The outcome is recorded.</strong> Check the steps and notes above, then close the case.
          {' '}Closing confirmed fraud adds its destinations to the known-mule list.
          <div className="row" style={{ marginTop: 8 }}>
            <button className="primary" disabled={busy}
                    onClick={() => act(() => api.closeCase(caseId), `Case #${caseId} closed.`)}>Close case</button>
          </div>
        </div>
      )}

      <details style={{ marginTop: 14 }}>
        <summary className="muted" style={{ fontSize: 12.5 }}>History: {flow.lifecycle.length} events</summary>
        <ol className="lifecycle">
          {flow.lifecycle.map((m, i) => (
            <li key={i}>
              <span className="mono dim">{when(m.at)}</span> {m.label}
              {m.detail ? <span className="muted"> — {m.detail}</span> : null}
              <span className="dim"> · {m.by}</span>
            </li>
          ))}
        </ol>
      </details>
    </div>
  )
}
