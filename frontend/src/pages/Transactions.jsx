import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, naira, when } from '../lib/api'
import { Banner, RiskBadge } from '../components/ui'

/**
 * Transactions (Figma "Transactions"): the searchable ledger (FR-032, D14b).
 * Alerts stream because they are few; transactions are searched because they are
 * many. Declined and reversed are first-class — a declined authorisation never
 * reaches a core ledger, which is why card testing is visible here at all.
 */

const CHANNELS = ['MOBILE_APP', 'WEB', 'USSD', 'POS', 'ATM', 'AGENT', 'BRANCH', 'API']
const RESULTS = ['APPROVED', 'DECLINED', 'FAILED', 'REVERSED']

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
      setItems(data.items); setError(null)
    } catch (err) { setError(err.message) } finally { setLoading(false) }
  }, [filters])

  useEffect(() => { search() }, [search])

  const approved = items.filter((t) => t.auth_result === 'APPROVED').length
  const declined = items.filter((t) => t.auth_result === 'DECLINED' || t.auth_result === 'FAILED').length
  const set = (k) => (e) => setFilters({ ...filters, [k]: e.target.value })
  const dot = (r) => (r === 'APPROVED' ? 'ok' : r === 'DECLINED' || r === 'FAILED' ? 'danger' : 'warn')

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Transactions</h1>
          <p className="page-sub">Every movement of value the channel and switch layer saw — approved, declined and reversed alike.</p>
        </div>
      </div>

      {error && <Banner kind="error">{error}</Banner>}

      <div className="statrow">
        <div className="statcard"><div className="statcard-k">In view</div><div className="statcard-v">{items.length.toLocaleString()}</div></div>
        <div className="statcard"><div className="statcard-k">Approved</div><div className="statcard-v" style={{ color: 'var(--ok)' }}>{approved.toLocaleString()}</div></div>
        <div className="statcard"><div className="statcard-k">Declined / failed</div><div className="statcard-v danger">{declined.toLocaleString()}</div></div>
      </div>

      <div className="card" style={{ padding: 0 }}>
        <div className="toolbar">
          <div className="searchbar sm" style={{ width: 300 }}>
            <svg className="nav-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
              <circle cx="11" cy="11" r="7" /><path d="M20 20l-4-4" strokeLinecap="round" /></svg>
            <input value={filters.q} onChange={set('q')} placeholder="Reference or customer…" />
          </div>
          <div className="row wrap" style={{ gap: 8 }}>
            <select className="mini" value={filters.channel} onChange={set('channel')}>
              <option value="">All channels</option>
              {CHANNELS.map((c) => <option key={c} value={c}>{c.replace(/_/g, ' ')}</option>)}
            </select>
            <select className="mini" value={filters.auth_result} onChange={set('auth_result')}>
              <option value="">Any result</option>
              {RESULTS.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
            <select className="mini" value={filters.risk_level} onChange={set('risk_level')}>
              <option value="">Any risk</option>
              {['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          </div>
        </div>

        <div className="table-scroll">
          <table className="rowtable">
            <thead>
              <tr>
                <th>Time</th><th>Reference</th><th>Customer</th><th className="num">Amount</th>
                <th>Channel</th><th>Result</th><th className="num">Score</th><th>Risk</th><th>Case</th>
              </tr>
            </thead>
            <tbody>
              {items.map((t) => (
                <tr key={t.id}>
                  <td className="mono dim" style={{ fontSize: 11.5 }}>{when(t.occurred_at)}</td>
                  <td className="mono" style={{ color: 'var(--accent)' }}>{t.transaction_ref.slice(0, 16)}…</td>
                  <td>{t.display_name || <span className="dim">—</span>}</td>
                  <td className="num" style={{ fontWeight: 600 }}>
                    {t.direction === 'INBOUND' ? '+' : ''}{naira(t.amount_minor)}
                  </td>
                  <td className="muted">{t.channel.replace(/_/g, ' ').toLowerCase()}</td>
                  <td><span className="authdot"><span className={`live-dot ${dot(t.auth_result)}`} />{t.auth_result.toLowerCase()}</span></td>
                  <td className="num mono">{t.score_0_100 ?? '—'}</td>
                  <td><RiskBadge level={t.risk_level} /></td>
                  <td>{t.case_id ? <Link className="link" to={`/cases/${t.case_id}`}>CASE-{t.case_id}</Link> : <span className="dim">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!items.length && <div className="empty">{loading ? 'Searching…' : 'No transactions match.'}</div>}
        </div>
        {items.length > 0 && <div className="table-foot dim">Showing {items.length} transaction{items.length === 1 ? '' : 's'}.</div>}
      </div>
    </div>
  )
}
