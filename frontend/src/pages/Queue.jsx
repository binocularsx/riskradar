import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { ago, api, when } from '../lib/api'
import { useAlertStream } from '../lib/useStream'
import { Banner, Empty, RiskBadge, SignalPill } from '../components/ui'

const LEVELS = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']
const CHANNELS = ['MOBILE_APP', 'WEB', 'USSD', 'POS', 'ATM', 'AGENT', 'BRANCH', 'API']
const STATES = ['OPEN', 'UNDER_REVIEW', 'ESCALATED', 'CLOSED']

/**
 * The alert queue (FR-030, FR-033).
 *
 * Cases, not alerts, are the unit of work — an incident that produced eight
 * alerts is one investigation, not eight (D13a). The live ticker on the right
 * shows individual alerts arriving so the desk can see the system working, but
 * the queue people actually work is the case list.
 */
export default function Queue({ user }) {
  const navigate = useNavigate()
  const [cases, setCases] = useState([])
  const [total, setTotal] = useState(0)
  const [ticker, setTicker] = useState([])
  const [alarms, setAlarms] = useState([])
  const [filters, setFilters] = useState({ state: '', risk_level: '', channel: '', sort: 'score' })
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const params = Object.fromEntries(Object.entries(filters).filter(([, v]) => v))
      const data = await api.cases({ ...params, limit: 100 })
      setCases(data.items)
      setTotal(data.total)
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [filters])

  useEffect(() => {
    load()
  }, [load])

  const { connected } = useAlertStream({
    onAlert: (alert) => {
      setTicker((prev) => [{ ...alert, at: new Date().toISOString() }, ...prev].slice(0, 40))
      load()
    },
    // FR-017: rule-only mode is never a silent degradation. If the model goes
    // away the desk finds out on the screen they are already looking at.
    onAlarm: (alarm) => setAlarms((prev) => [alarm, ...prev].slice(0, 5)),
  })

  return (
    <>
      {alarms.map((alarm, i) => (
        <Banner key={i} kind="warn">
          <strong>System alarm — {alarm.code}.</strong> {alarm.detail}
        </Banner>
      ))}
      {error && <Banner kind="error">{error}</Banner>}

      <div className="between" style={{ marginBottom: 14 }}>
        <div className="row wrap">
          <select
            style={{ width: 160 }}
            value={filters.state}
            onChange={(e) => setFilters({ ...filters, state: e.target.value })}
          >
            <option value="">Open cases</option>
            {STATES.map((s) => <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>)}
          </select>
          <select
            style={{ width: 150 }}
            value={filters.risk_level}
            onChange={(e) => setFilters({ ...filters, risk_level: e.target.value })}
          >
            <option value="">All risk levels</option>
            {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
          <select
            style={{ width: 150 }}
            value={filters.channel}
            onChange={(e) => setFilters({ ...filters, channel: e.target.value })}
          >
            <option value="">All channels</option>
            {CHANNELS.map((c) => <option key={c} value={c}>{c.replace(/_/g, ' ')}</option>)}
          </select>
          <select
            style={{ width: 150 }}
            value={filters.sort}
            onChange={(e) => setFilters({ ...filters, sort: e.target.value })}
          >
            <option value="score">Highest score</option>
            <option value="recent">Most recent alert</option>
            <option value="opened">Newest case</option>
          </select>
        </div>
        <span className="live">
          <span className={`live-dot ${connected ? 'on' : 'off'}`} />
          {connected ? 'live' : 'reconnecting'}
        </span>
      </div>

      <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 2.4fr) minmax(260px, 1fr)' }}>
        <div className="card" style={{ padding: 0 }}>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Case</th>
                  <th>Risk</th>
                  <th className="num">Score</th>
                  <th className="num">Alerts</th>
                  <th>Subject</th>
                  <th>State</th>
                  <th>Assignee</th>
                  <th>Last alert</th>
                </tr>
              </thead>
              <tbody>
                {cases.map((c) => (
                  <tr
                    key={c.id}
                    className="clickable"
                    onClick={() => navigate(`/cases/${c.id}`)}
                  >
                    <td className="mono">#{c.id}</td>
                    <td><RiskBadge level={c.risk_level} /></td>
                    <td className="num mono">{c.max_score ?? '—'}</td>
                    <td className="num">{c.alert_count}</td>
                    <td>
                      <div>{c.subject_display_name || 'Unknown'}</div>
                      <div className="mono dim">{c.subject_token.slice(0, 18)}…</div>
                    </td>
                    <td className="muted">{c.state.replace(/_/g, ' ')}</td>
                    <td className="muted">{c.assignee_name || <span className="dim">unassigned</span>}</td>
                    <td className="muted">{ago(c.last_alert_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!cases.length && <Empty>No cases match these filters.</Empty>}
          <div className="dim" style={{ padding: '10px 14px', fontSize: 12 }}>
            {cases.length} of {total} cases
          </div>
        </div>

        <div className="card">
          <h3>Live alerts</h3>
          {!ticker.length && (
            <p className="dim" style={{ fontSize: 12 }}>
              Alerts appear here as they are raised. Transactions are not streamed —
              they are aggregated on the metrics page, because a wall of every
              payment is unreadable.
            </p>
          )}
          {ticker.map((alert, i) => (
            <div key={`${alert.alert_id}-${i}`} style={{ padding: '7px 0', borderBottom: '1px solid var(--bg-inset)' }}>
              <div className="between">
                <RiskBadge level={alert.risk_level} />
                <span className="mono dim">{alert.score}</span>
              </div>
              <div style={{ fontSize: 12, marginTop: 3 }}>
                <Link to={`/cases/${alert.case_id}`}>Case #{alert.case_id}</Link>
                <span className="dim"> · {when(alert.at)}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </>
  )
}
