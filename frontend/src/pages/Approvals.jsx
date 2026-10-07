import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import { Banner } from '../components/ui'

/**
 * The lead's queue (D93).
 *
 * An analyst proposes a fraud finding; a different lead decides it. This is
 * that decision, and it deliberately shows four things before the buttons:
 * who proposed it, what they say, what it would do to the customer, and how
 * long it has waited — because approving without reading is the failure mode
 * a second pair of eyes exists to prevent.
 *
 * A lead's own proposals never appear here. The server enforces that; this
 * page says so, so nobody wonders where their own submission went.
 */
const DECISIONS = [
  { key: 'APPROVE', label: 'Approve', cls: 'confirm', hint: 'the finding stands; any restriction is authorised' },
  { key: 'REJECT', label: 'Reject', cls: 'dismiss', hint: 'the finding does not stand; the case stays open' },
  { key: 'RETURN', label: 'Return', cls: 'unsure', hint: 'send it back with what is missing' },
]

function money(minor, currency = 'NGN') {
  return `${currency} ${(Number(minor || 0) / 100).toLocaleString()}`
}

function waited(seconds) {
  const m = Math.round((seconds || 0) / 60)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

export default function Approvals() {
  const [queue, setQueue] = useState(null)
  const [reasons, setReasons] = useState({})
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [flash, setFlash] = useState(null)
  // D107a: the lead sets what the bank is finally asked to do. `edits` holds a
  // list only once they have touched it — untouched, the decision omits
  // restrictions entirely and the submission's own list stands.
  const [edits, setEdits] = useState({})
  const [suggesting, setSuggesting] = useState(null)

  const load = useCallback(async () => {
    try { setQueue(await api.approvals()); setError(null) } catch (e) { setError(e.message) }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 10000)
    return () => clearInterval(timer)
  }, [load])

  // The API accepts at most twenty on a decision; going over is a 422 at the
  // moment of approving, so it is caught here while it can still be fixed.
  const MAX_RESTRICTIONS = 20
  const restrictionsFor = (item) => edits[item.id] ?? (item.restrictions ?? [])
  const chosenFor = (item) => restrictionsFor(item).filter((r) => !r._off)
  const clearEdits = (item) => setEdits(({ [item.id]: _drop, ...rest }) => rest)

  const toggleRestriction = (item, index) => setEdits((prev) => {
    const list = (prev[item.id] ?? (item.restrictions ?? [])).map((r) => ({ ...r }))
    list[index]._off = !list[index]._off
    return { ...prev, [item.id]: list }
  })

  const addSuggestions = useCallback(async (item) => {
    setSuggesting(item.id); setError(null)
    try {
      const { items: suggested } = await api.restrictionSuggestions(item.case_id)
      setEdits((prev) => {
        const current = (prev[item.id] ?? (item.restrictions ?? [])).map((r) => ({ ...r }))
        // Identity is the action plus what it targets; a suggestion already on
        // the list must not be added twice.
        const key = (r) => [r.action, r.account_token, r.beneficiary_token,
                            r.transaction_ref, r.channel].join('|')
        const seen = new Set(current.map(key))
        for (const r of suggested) if (!seen.has(key(r))) current.push({ ...r })
        return { ...prev, [item.id]: current }
      })
    } catch (e) { setError(e.message) } finally { setSuggesting(null) }
  }, [])

  const decide = useCallback(async (item, decision) => {
    const reason = (reasons[item.id] || '').trim()
    if (reason.length < 10) {
      setError('Say why in at least 10 characters: your decision is recorded with its reason.')
      return
    }
    setBusy(item.id); setError(null)
    try {
      // Only when the lead actually edited: omitted, the submission's list
      // stands, which is what every existing caller expects.
      const payload = { decision, reason }
      if (decision === 'APPROVE' && edits[item.id]) {
        payload.restrictions = chosenFor(item).map(({ _off, why, ...r }) => r)
      }
      await api.decideSubmission(item.id, payload)
      clearEdits(item)
      setFlash(`Case #${item.case_id}: ${{ APPROVE: 'proposal approved; any bank action still requires delivery and a bank outcome', REJECT: 'proposal rejected; case remains open', RETURN: 'proposal returned for more work' }[decision]}.`)
      setReasons((r) => ({ ...r, [item.id]: '' }))
      await load()
    } catch (e) { setError(e.message) } finally { setBusy(null) }
  }, [reasons, load, edits])

  if (!queue) return error ? <Banner kind="error">Approval queue unavailable: {error} <button onClick={load}>Retry</button></Banner> : <p className="muted" role="status">Loading approvals…</p>
  const { items, summary } = queue

  return (
    <div className="page">
      <header className="page-head">
        <h1>Fraud approvals</h1>
        <p className="dim">
          An analyst proposes; you decide. You cannot decide your own proposals, and only an approval
          writes the outcome onto the case or authorises a restriction.
        </p>
      </header>

      {error && <Banner kind="error">{error} Showing the last loaded queue. <button onClick={load}>Retry</button></Banner>}
      {flash && <Banner kind="ok">{flash}</Banner>}

      <div className="row wrap" style={{ gap: 18, margin: '4px 0 14px' }}>
        <span><strong>{summary.waiting}</strong> waiting in this queue (up to 50 shown)</span>
        <span className="dim">oldest {waited(summary.oldest_seconds)}</span>
        <span className="dim">{summary.with_restrictions} asking to restrict an account</span>
        {summary.mine_awaiting_someone_else > 0 && (
          <span className="dim">{summary.mine_awaiting_someone_else} of yours waiting for another lead</span>
        )}
      </div>

      {items.length === 0 && <p className="muted">Nothing waiting for a decision.</p>}

      {items.map((item) => (
        <section key={item.id} className="card" style={{ marginBottom: 14 }}>
          <div className="between wrap">
            <div>
              <strong>Case #{item.case_id}</strong>{' '}
              <span className={`pill ${String(item.risk_level).toLowerCase()}`}>{item.risk_level}</span>{' '}
              <span className="dim">{item.alert_count} alert(s) · {money(item.exposure_minor)} under investigation</span>
            </div>
            <div className="dim">
              proposed by {item.submitted_by_name} · waiting {waited(item.waiting_seconds)}
            </div>
          </div>

          <p style={{ margin: '10px 0 4px' }}>
            Proposes <strong>{String(item.proposed_outcome).replace(/_/g, ' ').toLowerCase()}</strong>
          </p>
          <p className="dim" style={{ whiteSpace: 'pre-wrap', margin: '0 0 8px' }}>{item.rationale}</p>

          {(item.restrictions?.length > 0 || item.proposed_outcome === 'CONFIRMED_FRAUD') && (
            <div className="warn-box" style={{ marginBottom: 8 }}>
              <strong>What the bank will be asked to do</strong>
              <p className="dim" style={{ margin: '4px 0 8px', fontSize: 12 }}>
                Review the proposed actions before approving. Uncheck anything that should
                not be sent, or pull in what this case's own transactions suggest.
              </p>

              {restrictionsFor(item).length === 0 && (
                <p className="dim" style={{ margin: '0 0 8px', fontSize: 12.5 }}>
                  Nothing proposed. Approving as-is asks the bank for nothing.
                </p>
              )}

              {restrictionsFor(item).map((r, i) => (
                <label key={i} className="row" style={{ gap: 8, alignItems: 'flex-start', marginBottom: 5 }}>
                  <input type="checkbox" checked={!r._off} style={{ marginTop: 3 }}
                         onChange={() => toggleRestriction(item, i)} />
                  <span style={{ fontSize: 12.5 }}>
                    <span className="pill">{String(r.action).replace(/_/g, ' ').toLowerCase()}</span>{' '}
                    <span className="mono dim">
                      {r.transaction_ref || r.account_token || r.beneficiary_token || ''}
                    </span>
                    {r.channel ? <span className="dim"> ({r.channel})</span> : null}
                    {r.why ? <span className="dim"> — {r.why}</span> : null}
                  </span>
                </label>
              ))}

              <div className="row" style={{ gap: 8, marginTop: 8 }}>
                <button className="ghost" disabled={suggesting === item.id}
                        onClick={() => addSuggestions(item)}>
                  {suggesting === item.id ? 'Looking…' : 'Add what this case suggests'}
                </button>
                {edits[item.id] && (
                  <button className="ghost" onClick={() => clearEdits(item)}>
                    Reset to what was proposed
                  </button>
                )}
              </div>

              {chosenFor(item).length > MAX_RESTRICTIONS && (
                <p className="risk risk-HIGH" style={{ margin: '8px 0 0', fontSize: 12 }}>
                  {chosenFor(item).length} selected; a decision carries at most {MAX_RESTRICTIONS}.
                  Uncheck {chosenFor(item).length - MAX_RESTRICTIONS} before approving.
                </p>
              )}
              <p className="dim" style={{ margin: '8px 0 0', fontSize: 12 }}>
                {edits[item.id]
                  ? `On approval, ${chosenFor(item).length} restriction(s) go to the bank as you have set them, and the customer's account manager is told.`
                  : 'On approval these go to the bank as proposed, and the customer’s account manager is told. Risk Radar recommends; the bank applies (D7).'}
              </p>
            </div>
          )}

          <textarea style={{ minHeight: 46 }} value={reasons[item.id] || ''}
                    placeholder="Why? (required, recorded with your name)"
                    onChange={(e) => setReasons((r) => ({ ...r, [item.id]: e.target.value }))} />

          <div className="row wrap" style={{ gap: 10, marginTop: 8 }}>
            {DECISIONS.map((d) => (
              <button key={d.key} className={`big ${d.cls}`}
                      disabled={busy === item.id
                        || (d.key === 'APPROVE' && chosenFor(item).length > MAX_RESTRICTIONS)}
                      title={d.key === 'APPROVE' && chosenFor(item).length > MAX_RESTRICTIONS
                        ? `Too many restrictions selected (max ${MAX_RESTRICTIONS})`
                        : d.hint}
                      onClick={() => decide(item, d.key)}>
                {d.label}
              </button>
            ))}
            <Link className="ghost" to={`/triage?case=${item.case_id}`}>Open the case</Link>
          </div>
        </section>
      ))}
    </div>
  )
}
