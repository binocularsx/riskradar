import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, naira, when } from '../lib/api'
import { Banner, Empty, RiskBadge } from '../components/ui'

/**
 * The searchable transaction table (FR-032).
 *
 * This is the other half of D14b: alerts stream because they are few, and
 * transactions are searched because they are many. Nobody monitors a scrolling
 * wall of every payment — they look one up when a case makes them want to.
 */
export default function Transactions() {
  const [filters, setFilters] = useState({ q: '', channel: '', auth_result: '', risk_level: '' })
  const [items, setItems] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  const search = useCallback(async () => {
    setLoading(true)
    try {
      const params = Object.fromEntries(Object.entries(filters).filter(([, v]) => v))
      const data = await api.searchTransactions({ ...params, limit: 100 })
      setItems(data.items)
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [filters])

  useEffect(() => {
    search()
  }, [search])

  return (
    <>
      {error && <Banner kind="error">{error}</Banner>}

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="row wrap">
          <input
            style={{ flex: 2, minWidth: 220 }}
            placeholder="Transaction reference or customer name"
            value={filters.q}
            onChange={(e) => setFilters({ ...filters, q: e.target.value })}
          />
          <select style={{ width: 160 }} value={filters.channel}
                  onChange={(e) => setFilters({ ...filters, channel: e.target.value })}>
            <option value="">All channels</option>
            {['MOBILE_APP', 'WEB', 'USSD', 'POS', 'ATM', 'AGENT', 'BRANCH', 'API']
              .map((c) => <option key={c} value={c}>{c.replace(/_/g, ' ')}</option>)}
          </select>
          <select style={{ width: 150 }} value={filters.auth_result}
                  onChange={(e) => setFilters({ ...filters, auth_result: e.target.value })}>
            <option value="">Any result</option>
            {['APPROVED', 'DECLINED', 'FAILED', 'REVERSED']
              .map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <select style={{ width: 150 }} value={filters.risk_level}
                  onChange={(e) => setFilters({ ...filters, risk_level: e.target.value })}>
            <option value="">Any risk level</option>
            {['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </div>
        <p className="dim" style={{ fontSize: 12, marginBottom: 0, marginTop: 10 }}>
          Declined and reversed transactions are first-class here. A declined
          authorisation never reaches a core banking ledger, which is why Risk
          Radar consumes the channel and switch layer rather than the core — and
          why card testing is visible at all.
        </p>
      </div>

      <div className="card" style={{ padding: 0 }}>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>When</th><th>Reference</th><th>Customer</th>
                <th className="num">Amount</th><th>Channel</th><th>Rail</th>
                <th>Result</th><th className="num">Score</th><th>Risk</th><th>Case</th>
              </tr>
            </thead>
            <tbody>
              {items.map((t) => (
                <tr key={t.id}>
                  <td className="mono">{when(t.occurred_at)}</td>
                  <td className="mono dim">{t.transaction_ref.slice(0, 16)}…</td>
                  <td>{t.display_name || <span className="dim">—</span>}</td>
                  <td className="num">
                    {t.direction === 'INBOUND' ? '+' : ''}{naira(t.amount_minor)}
                    {t.direction === 'INBOUND' && <div className="dim" style={{ fontSize: 11 }}>incoming</div>}
                  </td>
                  <td className="muted">{t.channel.replace(/_/g, ' ')}</td>
                  <td className="muted">{t.rail}</td>
                  <td className="muted">
                    {t.auth_result}
                    {t.decline_reason && <div className="dim" style={{ fontSize: 11 }}>{t.decline_reason}</div>}
                  </td>
                  <td className="num mono">{t.score_0_100 ?? '—'}</td>
                  <td><RiskBadge level={t.risk_level} /></td>
                  <td>{t.case_id ? <Link to={`/cases/${t.case_id}`}>#{t.case_id}</Link> : <span className="dim">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!items.length && !loading && <Empty>No transactions match.</Empty>}
        {loading && <Empty>Searching…</Empty>}
      </div>
    </>
  )
}
