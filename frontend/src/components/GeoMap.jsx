import { useEffect, useState } from 'react'

import { api, nairaShort } from '../lib/api'

/**
 * Where the activity came from (D109).
 *
 * `transactions.ip_region` is an ISO 3166-2 state code for the session's IP
 * address. That is deliberately *not* the customer's registered address, and
 * the panel says so: a cluster here means several sessions resolved to one
 * state, which is what a cash-out ring looks like — not where anybody lives.
 *
 * Circles are placed at each state's real centroid on an equirectangular
 * projection of Nigeria's bounding box, so the positions are geography rather
 * than decoration. There are no coordinates in the data and no boundary file in
 * the repo, so this is a proportional-symbol map, not a choropleth: honest
 * about being state-level, and it draws with no map dependency at all.
 */

// Approximate centroids, degrees. Only the states the simulator produces.
const STATES = {
  'NG-LA': { name: 'Lagos', lat: 6.52, lon: 3.38 },
  'NG-OY': { name: 'Oyo', lat: 8.15, lon: 3.60 },
  'NG-KD': { name: 'Kaduna', lat: 10.52, lon: 7.44 },
  'NG-KN': { name: 'Kano', lat: 11.75, lon: 8.52 },
  'NG-EN': { name: 'Enugu', lat: 6.45, lon: 7.50 },
  'NG-AB': { name: 'Abia', lat: 5.45, lon: 7.52 },
  'NG-RI': { name: 'Rivers', lat: 4.85, lon: 6.95 },
  'NG-DE': { name: 'Delta', lat: 5.70, lon: 5.93 },
  'NG-FC': { name: 'FCT Abuja', lat: 9.07, lon: 7.40 },
}

// Nigeria's bounding box, so the plot is a projection rather than a scatter.
const BOUNDS = { west: 2.6, east: 14.7, south: 4.0, north: 13.9 }
const W = 460
const H = 380

// Inset, because a circle is drawn around its centre: Lagos sits within a
// degree of the western edge and its symbol is the largest on the map, so an
// edge-to-edge projection clipped the most important state in half.
const M = 46
const px = (lon) => M + ((lon - BOUNDS.west) / (BOUNDS.east - BOUNDS.west)) * (W - 2 * M)
const py = (lat) => (H - M) - ((lat - BOUNDS.south) / (BOUNDS.north - BOUNDS.south)) * (H - 2 * M)

const HOURS = [
  { key: 24, label: '24 hours' },
  { key: 168, label: '7 days' },
  { key: 720, label: '30 days' },
]

export default function GeoMap() {
  const [hours, setHours] = useState(168)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [picked, setPicked] = useState(null)

  useEffect(() => {
    let cancelled = false
    setData(null)
    api.geography(hours)
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(e.message))
    return () => { cancelled = true }
  }, [hours])

  if (error) return <div className="card"><h2>Where activity came from</h2><p className="dim">{error}</p></div>
  if (!data) return <div className="card"><h2>Where activity came from</h2><p className="muted">Loading…</p></div>

  const items = data.items.filter((i) => STATES[i.ip_region])
  const unplaceable = data.items.filter((i) => !STATES[i.ip_region])
  const maxTx = Math.max(...items.map((i) => Number(i.transactions)), 1)
  const maxRate = Math.max(...items.map((i) => Number(i.alert_rate)), 0.0001)
  // Area, not radius, carries the count — a radius-proportional circle
  // overstates a big number by its square.
  const radius = (n) => 6 + Math.sqrt(Number(n) / maxTx) * 30
  const heat = (rate) => Math.min(Number(rate) / maxRate, 1)
  const selected = items.find((i) => i.ip_region === picked) || null

  return (
    <div className="card">
      <div className="between wrap" style={{ marginBottom: 2 }}>
        <h2 style={{ margin: 0 }}>Where activity came from</h2>
        <div className="row" style={{ gap: 6 }}>
          {HOURS.map((h) => (
            <button key={h.key} className={hours === h.key ? 'primary' : ''}
                    style={{ fontSize: 11.5, padding: '3px 10px' }}
                    onClick={() => setHours(h.key)}>{h.label}</button>
          ))}
        </div>
      </div>
      <p className="dim" style={{ fontSize: 12, margin: '4px 0 10px', maxWidth: '68ch' }}>
        {data.note} Circle size is transaction volume; colour is the share of that volume
        that raised an alert.
      </p>

      <div className="geo-wrap">
        <svg viewBox={`0 0 ${W} ${H}`} className="geo-map" role="img"
             aria-label={`Map of Nigeria showing transaction volume by state over the last ${hours} hours`}>
          {/* A graticule rather than a fake coastline: an inaccurate outline
              drawn from memory would be worse than none. */}
          {[4, 6, 8, 10, 12].map((lat) => (
            <line key={`la${lat}`} x1="0" x2={W} y1={py(lat)} y2={py(lat)} className="geo-grid" />
          ))}
          {[4, 6, 8, 10, 12, 14].map((lon) => (
            <line key={`lo${lon}`} y1="0" y2={H} x1={px(lon)} x2={px(lon)} className="geo-grid" />
          ))}

          {items.map((i) => {
            const st = STATES[i.ip_region]
            const r = radius(i.transactions)
            const on = picked === i.ip_region
            return (
              <g key={i.ip_region} className="geo-g"
                 onClick={() => setPicked((p) => (p === i.ip_region ? null : i.ip_region))}>
                <title>{`${st.name}: ${Number(i.transactions).toLocaleString()} transactions, ${i.alerts} alerts`}</title>
                <circle cx={px(st.lon)} cy={py(st.lat)} r={r}
                        className={`geo-dot ${on ? 'picked' : ''}`}
                        style={{ '--heat': heat(i.alert_rate) }} />
                <text x={px(st.lon)} y={py(st.lat) - r - 5} textAnchor="middle" className="geo-label">
                  {st.name}
                </text>
              </g>
            )
          })}
        </svg>

        <div className="geo-side">
          <table className="rowtable geo-table">
            <thead>
              <tr><th>State</th><th className="num">Transactions</th><th className="num">Alerts</th>
                  <th className="num">Alert rate</th></tr>
            </thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.ip_region} className={picked === i.ip_region ? 'hot' : ''}
                    onClick={() => setPicked((p) => (p === i.ip_region ? null : i.ip_region))}>
                  <td>{STATES[i.ip_region].name}</td>
                  <td className="num">{Number(i.transactions).toLocaleString()}</td>
                  <td className="num">{i.alerts}</td>
                  <td className="num">{(Number(i.alert_rate) * 100).toFixed(2)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
          {selected && (
            <div className="geo-detail">
              <strong>{STATES[selected.ip_region].name}</strong>
              <div className="lg-kv" style={{ marginTop: 6 }}>
                <div><dt>Customers active</dt><dd>{selected.customers}</dd></div>
                <div><dt>Declined or failed</dt><dd>{selected.declined}</dd></div>
                <div><dt>Approved value</dt><dd>{nairaShort(selected.approved_value_minor)}</dd></div>
                <div><dt>Cases opened</dt><dd>{selected.cases}</dd></div>
              </div>
            </div>
          )}
        </div>
      </div>

      <p className="dim" style={{ fontSize: 11.5, margin: '10px 0 0' }}>
        {data.total.toLocaleString()} transactions placed
        {data.no_region ? ` · ${data.no_region.toLocaleString()} had no region and are not on the map` : ''}
        {unplaceable.length ? ` · ${unplaceable.length} region code(s) not mapped: ${unplaceable.map((u) => u.ip_region).join(', ')}` : ''}
      </p>
    </div>
  )
}
