import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, nairaShort, ago } from '../lib/api'

/**
 * Who else is connected to this case (D84).
 *
 * The customer sits in the middle; the destinations they paid and the devices
 * they used sit around them; other customers who paid the same destinations or
 * used the same devices sit on the outside, next to what they share. Red means
 * something is already known against it: a confirmed fraud case, a list entry,
 * or several customers paying a destination in the same day.
 *
 * Beside the picture, every identifier gets a reputation line in words, because
 * a graph shows that a link exists and the line says whether it matters.
 */

const W = 560
const H = 420
const CX = W / 2
const CY = H / 2

function polar(r, angle) {
  return [CX + r * Math.cos(angle), CY + r * Math.sin(angle)]
}

function short(token) {
  return token ? `…${String(token).slice(-6)}` : '—'
}

export default function LinkGraph({ caseId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [hover, setHover] = useState(null)

  useEffect(() => {
    setData(null); setError(null)
    api.caseLinks(caseId).then(setData).catch((e) => setError(e.message))
  }, [caseId])

  const layout = useMemo(() => {
    if (!data) return null
    const pos = { customer: [CX, CY] }
    const inner = data.nodes.filter((n) => n.kind === 'destination' || n.kind === 'device')
    inner.forEach((n, i) => { pos[n.id] = polar(120, (2 * Math.PI * i) / Math.max(inner.length, 1) - Math.PI / 2) })
    const outer = data.nodes.filter((n) => n.kind === 'other_customer')
    // Place each other customer near the first identifier it shares.
    const angleOf = (id) => {
      const p = pos[id]
      return p ? Math.atan2(p[1] - CY, p[0] - CX) : 0
    }
    const crowd = {}
    outer.forEach((n) => {
      const edge = data.edges.find((e) => e.from === n.id)
      const base = edge ? angleOf(edge.to) : 0
      const k = crowd[edge?.to] = (crowd[edge?.to] || 0) + 1
      pos[n.id] = polar(190, base + (k - 1) * 0.22 - 0.11)
    })
    return pos
  }, [data])

  if (error) return <div className="card"><h3>Connections</h3><p className="dim">{error}</p></div>
  if (!data || !layout) return <div className="card"><h3>Connections</h3><p className="dim">Loading connections…</p></div>

  const s = data.summary
  const destinations = data.nodes.filter((n) => n.kind === 'destination')
  const devices = data.nodes.filter((n) => n.kind === 'device')
  const others = data.nodes.filter((n) => n.kind === 'other_customer')

  return (
    <div className="card">
      <div className="between wrap">
        <h3 style={{ margin: 0 }}>Connections · last {data.days} days</h3>
        <span className="dim" style={{ fontSize: 12 }}>
          {s.shared_destinations} of {s.destinations} destinations and {s.shared_devices} of {s.devices} devices shared with
          {' '}{s.linked_customers} other customer{s.linked_customers === 1 ? '' : 's'}
          {s.linked_confirmed_fraud ? ` · ${s.linked_confirmed_fraud} with confirmed fraud` : ''}
        </span>
      </div>
      <div className="linkgraph">
        <div className="table-scroll">
          <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
               aria-label="Graph of this customer, the destinations they paid, the devices they used, and other customers sharing them">
            {data.edges.map((e, i) => {
              const a = layout[e.from]; const b = layout[e.to]
              if (!a || !b) return null
              const hot = hover && (e.from === hover || e.to === hover)
              return <line key={i} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]}
                           className={`lg-edge ${e.kind} ${hot ? 'hot' : ''}`} />
            })}
            {data.nodes.map((n) => {
              const [x, y] = layout[n.id] || [CX, CY]
              const r = n.kind === 'customer' ? 18 : n.kind === 'other_customer' ? 9 : 12
              const label = n.kind === 'customer' ? n.label
                : n.kind === 'other_customer' ? n.label : short(n.token)
              return (
                <g key={n.id} onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)}>
                  {n.kind === 'device'
                    ? <rect x={x - r} y={y - r} width={2 * r} height={2 * r} rx={3} className={`lg-node ${n.kind} ${n.risky ? 'risky' : ''}`} />
                    : <circle cx={x} cy={y} r={r} className={`lg-node ${n.kind} ${n.risky ? 'risky' : ''}`} />}
                  <text x={x} y={y + r + 12} textAnchor="middle" className="lg-label">{label}</text>
                </g>
              )
            })}
          </svg>
        </div>
        <div className="lg-legend">
          <span><i className="lg-key customer" /> this customer</span>
          <span><i className="lg-key destination" /> destination paid</span>
          <span><i className="lg-key device square" /> device used</span>
          <span><i className="lg-key other_customer" /> another customer</span>
          <span><i className="lg-key risky" /> something known against it</span>
        </div>
      </div>

      {destinations.length > 0 && (
        <>
          <h3 style={{ marginTop: 14 }}>Destinations this customer paid</h3>
          <div className="reputation">
            {destinations.map((d) => (
              <div key={d.id} className={`rep ${d.risky ? 'risky' : ''} ${hover === d.id ? 'hot' : ''}`}
                   onMouseEnter={() => setHover(d.id)} onMouseLeave={() => setHover(null)}>
                <span className="mono">{short(d.token)}</span>
                <span className="badges">
                  <span className="badge-chip">first seen {ago(d.first_seen)}</span>
                  <span className="badge-chip">{d.payments} payment{d.payments === 1 ? '' : 's'} · {nairaShort(d.amount_minor)}</span>
                  {d.other_customers > 0 && <span className={`badge-chip ${d.other_customers_24h >= 2 ? 'warn' : ''}`}>
                    paid by {d.other_customers} other customer{d.other_customers === 1 ? '' : 's'}{d.other_customers_24h ? `, ${d.other_customers_24h} today` : ''}</span>}
                  {d.alerts > 0 && <span className="badge-chip">{d.alerts} alert{d.alerts === 1 ? '' : 's'} on payments to it</span>}
                  {d.confirmed_fraud_cases > 0 && <span className="badge-chip danger">{d.confirmed_fraud_cases} confirmed fraud case{d.confirmed_fraud_cases === 1 ? '' : 's'}</span>}
                  {(d.lists || []).map((l) => <span key={l} className="badge-chip danger">{l.replace('_', ' ').toLowerCase()} list</span>)}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
      {devices.length > 0 && (
        <>
          <h3 style={{ marginTop: 14 }}>Devices this customer used</h3>
          <div className="reputation">
            {devices.map((d) => (
              <div key={d.id} className={`rep ${d.risky ? 'risky' : ''}`}
                   onMouseEnter={() => setHover(d.id)} onMouseLeave={() => setHover(null)}>
                <span className="mono">{short(d.token)}</span>
                <span className="badges">
                  <span className="badge-chip">first used by this customer {ago(d.first_used_by_customer)}</span>
                  <span className="badge-chip">{d.uses} use{d.uses === 1 ? '' : 's'}</span>
                  {d.other_customers > 0 && <span className={`badge-chip ${d.other_customers >= 2 ? 'warn' : ''}`}>
                    also used by {d.other_customers} other customer{d.other_customers === 1 ? '' : 's'}</span>}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
      {others.length > 0 && (
        <>
          <h3 style={{ marginTop: 14 }}>Other customers linked to this one</h3>
          <div className="reputation">
            {others.map((o) => (
              <div key={o.id} className={`rep ${o.risky ? 'risky' : ''}`}
                   onMouseEnter={() => setHover(o.id)} onMouseLeave={() => setHover(null)}>
                <span>{o.label}</span>
                <span className="badges">
                  <span className="badge-chip">shares a {o.via.join(' and a ')}</span>
                  {o.case_id
                    ? <Link className={`badge-chip ${o.case_outcome === 'CONFIRMED_FRAUD' ? 'danger' : ''}`} to={`/triage?case=${o.case_id}`}>
                        case #{o.case_id} · {o.case_outcome ? o.case_outcome.replace('_', ' ').toLowerCase() : (o.case_state || '').replace('_', ' ').toLowerCase()} →
                      </Link>
                    : <span className="badge-chip">no case</span>}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
      {!destinations.length && !devices.length && (
        <p className="dim" style={{ margin: '10px 0 0' }}>No destinations or devices recorded for this customer in the window.</p>
      )}
      <p className="dim" style={{ fontSize: 11.5, margin: '12px 0 0' }}>
        Identifiers are hashed; the same hash means the same account or device. A shared destination is normal for a
        school or a utility; it matters when the destination is new and several customers paid it the same day.
      </p>
    </div>
  )
}
