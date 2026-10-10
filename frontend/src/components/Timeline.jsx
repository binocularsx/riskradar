import { useState } from 'react'

import { naira, when } from '../lib/api'
import { CHANNEL, PAYMENT_RESULT, say } from '../lib/words'

/**
 * The customer's transactions plotted against time.
 *
 * Twelve rows in a table and twelve dots on a timeline contain the same data,
 * but only one of them shows you a *burst*. An account takeover looks like a
 * cluster jammed against the right-hand edge after a long quiet stretch; a mule
 * fan-out looks like a picket fence. That shape is the thing an analyst is
 * actually looking for, and reading it off a table takes thirty seconds of
 * comparing timestamps.
 *
 * Amounts are on a log scale because retail transactions span six orders of
 * magnitude — a linear axis would flatten every ordinary payment onto the floor
 * and show one spike.
 */
export default function Timeline({ items }) {
  if (!items?.length) return null

  const W = 900
  const H = 190
  const PAD = { l: 52, r: 16, t: 16, b: 30 }

  const times = items.map((t) => new Date(t.occurred_at).getTime())
  const t0 = Math.min(...times)
  const t1 = Math.max(...times)
  const span = Math.max(t1 - t0, 60_000)

  // Zoom is a window over time, not a scale factor on the drawing: a burst of
  // nine payments inside forty seconds is invisible on an axis stretched across
  // a month, and stretching the picture would only make fatter dots. Narrowing
  // the window is what separates them.
  const [zoom, setZoom] = useState(1)      // 1 = the whole span
  // Centred on the alerted activity, not on the midpoint of the span. Zooming
  // into the middle of a quiet month shows an empty chart, which reads as a
  // broken control rather than as an honest gap.
  const [centre, setCentre] = useState(() => {
    const alerted = items.filter((t) => t.alerted).map((t) => new Date(t.occurred_at).getTime())
    if (!alerted.length) return 0.5
    const mid = (Math.min(...alerted) + Math.max(...alerted)) / 2
    return Math.min(Math.max((mid - t0) / Math.max(t1 - t0, 1), 0), 1)
  })

  const windowMs = span / zoom
  // Clamped, so panning to either end stops at the data rather than scrolling
  // into empty time.
  const start = Math.min(Math.max(t0 + centre * span - windowMs / 2, t0), t1 - windowMs)
  const end = start + windowMs
  const visible = items.filter((t) => {
    const ms = new Date(t.occurred_at).getTime()
    return ms >= start && ms <= end
  })

  const amounts = items.map((t) => Math.max(Number(t.amount_minor) / 100, 1))
  const maxAmount = Math.max(...amounts)
  const logMax = Math.log10(maxAmount + 1)

  const x = (ms) => PAD.l + ((ms - start) / windowMs) * (W - PAD.l - PAD.r)
  const y = (naira_) => {
    const frac = Math.log10(naira_ + 1) / (logMax || 1)
    return H - PAD.b - frac * (H - PAD.t - PAD.b)
  }

  // Gridlines at powers of ten, labelled with real amounts so the axis means
  // something rather than being decoration.
  const ticks = []
  for (let p = 2; p <= Math.ceil(logMax); p += 1) {
    const value = 10 ** p
    if (value <= maxAmount * 1.4) ticks.push(value)
  }

  const label = (ms) => {
    const mins = Math.round(ms / 60000)
    if (mins >= 2880) return `${Math.round(mins / 1440)} days`
    if (mins >= 120) return `${Math.round(mins / 60)} hours`
    if (mins >= 1) return `${mins} minutes`
    return `${Math.round(ms / 1000)} seconds`
  }
  const spanLabel = label(span)
  const zoomed = zoom > 1

  return (
    <div className="card" style={{ padding: '14px 16px' }}>
      <div className="between" style={{ marginBottom: 4 }}>
        <h3 style={{ margin: 0 }}>
          Activity — {zoomed
            ? `${visible.length} of ${items.length} transactions, ${label(windowMs)} shown`
            : `${items.length} transactions over ${spanLabel}`}
        </h3>
        <div className="row" style={{ gap: 14, fontSize: 11 }}>
          <span className="dim">
            <svg width="9" height="9" style={{ verticalAlign: -1 }}>
              <circle cx="4.5" cy="4.5" r="4" fill="var(--critical)" />
            </svg>{' '}
            alerted
          </span>
          <span className="dim">
            <svg width="9" height="9" style={{ verticalAlign: -1 }}>
              <circle cx="4.5" cy="4.5" r="3.4" fill="none" stroke="var(--text-3)" strokeWidth="1.3" />
            </svg>{' '}
            not alerted
          </span>
          <span className="dim">
            <svg width="11" height="9" style={{ verticalAlign: -1 }}>
              <path d="M1 8 L5.5 1 L10 8" fill="none" stroke="var(--warn)" strokeWidth="1.4" />
            </svg>{' '}
            declined
          </span>
        </div>
      </div>

      {/* Zoom narrows the window; pan moves it. Pan is disabled at 1x because
          there is nothing either side of the whole span to move to. */}
      <div className="tl-controls">
        <label>
          <span>Zoom</span>
          <input type="range" min="1" max="40" step="0.5" value={zoom}
                 onChange={(e) => setZoom(Number(e.target.value))}
                 aria-label="Zoom the activity window" />
          <span className="tl-readout mono">{zoom === 1 ? 'all' : `${zoom}×`}</span>
        </label>
        <label className={zoomed ? '' : 'tl-off'}>
          <span>Pan</span>
          <input type="range" min="0" max="1" step="0.001" value={centre} disabled={!zoomed}
                 onChange={(e) => setCentre(Number(e.target.value))}
                 aria-label="Move the activity window through time" />
          <span className="tl-readout mono">{zoomed ? label(windowMs) : '—'}</span>
        </label>
        {zoomed && (
          <button className="ghost" onClick={() => { setZoom(1); setCentre(0.5) }}>Reset</button>
        )}
      </div>

      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }}
           role="img"
           aria-label={`Transaction timeline: ${items.length} transactions over ${spanLabel}, ${items.filter((i) => i.alerted).length} of them alerted.`}>
        {/* the envelope of alerted activity */}
        {(() => {
          const alerted = visible.filter((t) => t.alerted)
          if (alerted.length < 2) return null
          const pts = alerted.map((t) => [
            x(new Date(t.occurred_at).getTime()),
            y(Math.max(Number(t.amount_minor) / 100, 1)),
          ])
          const d = [
            `M ${pts[0][0]} ${H - PAD.b}`,
            ...pts.map(([px, py]) => `L ${px} ${py}`),
            `L ${pts[pts.length - 1][0]} ${H - PAD.b}`,
            'Z',
          ].join(' ')
          return <path d={d} fill="var(--critical)" fillOpacity="0.1" />
        })()}

        {/* amount gridlines */}
        {ticks.map((value) => (
          <g key={value}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(value)} y2={y(value)}
                  stroke="var(--line)" strokeWidth="1" />
            <text x={PAD.l - 8} y={y(value) + 3.5} textAnchor="end"
                  fontSize="9.5" fill="var(--text-3)" fontFamily="var(--mono)">
              {value >= 1e6 ? `₦${value / 1e6}m` : `₦${value / 1e3}k`}
            </text>
          </g>
        ))}

        {/* baseline */}
        <line x1={PAD.l} x2={W - PAD.r} y1={H - PAD.b} y2={H - PAD.b}
              stroke="var(--line-2)" strokeWidth="1" />

        {/* stems, so a cluster reads as density even where dots overlap */}
        {visible.map((t, i) => {
          const cx = x(new Date(t.occurred_at).getTime())
          const cy = y(Math.max(Number(t.amount_minor) / 100, 1))
          return (
            <line key={`s${i}`} x1={cx} x2={cx} y1={cy} y2={H - PAD.b}
                  stroke={t.alerted ? 'var(--critical)' : 'var(--line-2)'}
                  strokeWidth={t.alerted ? 1.2 : 0.8}
                  opacity={t.alerted ? 0.5 : 0.35} />
          )
        })}

        {visible.map((t, i) => {
          const cx = x(new Date(t.occurred_at).getTime())
          const cy = y(Math.max(Number(t.amount_minor) / 100, 1))
          const declined = t.auth_result !== 'APPROVED'
          const title = `${when(t.occurred_at)} · ${naira(t.amount_minor)} · ${say(CHANNEL, t.channel)} · ${say(PAYMENT_RESULT, t.auth_result)}${t.alerted ? ' · flagged' : ''}`

          if (declined) {
            return (
              <g key={i}>
                <title>{title}</title>
                <path d={`M ${cx - 4.5} ${cy + 4} L ${cx} ${cy - 3.5} L ${cx + 4.5} ${cy + 4}`}
                      fill="none" stroke={t.alerted ? 'var(--critical)' : 'var(--warn)'}
                      strokeWidth="1.6" strokeLinejoin="round" />
              </g>
            )
          }
          return (
            <g key={i}>
              <title>{title}</title>
              <circle cx={cx} cy={cy} r={t.alerted ? 4.6 : 3.4}
                      fill={t.alerted ? 'var(--critical)' : 'none'}
                      stroke={t.alerted ? 'var(--critical)' : 'var(--text-3)'}
                      strokeWidth="1.3" />
            </g>
          )
        })}

        {visible.length === 0 && (
          <text x={W / 2} y={H / 2} textAnchor="middle" fontSize="11" fill="var(--text-3)">
            Nothing in this window — pan to find the activity, or reset.
          </text>
        )}

        {/* time axis: just the ends, which is all anyone reads */}
        <text x={PAD.l} y={H - 10} fontSize="9.5" fill="var(--text-3)" fontFamily="var(--mono)">
          {when(new Date(start).toISOString())}
        </text>
        <text x={W - PAD.r} y={H - 10} textAnchor="end" fontSize="9.5"
              fill="var(--text-3)" fontFamily="var(--mono)">
          {when(new Date(end).toISOString())}
        </text>
      </svg>
    </div>
  )
}
