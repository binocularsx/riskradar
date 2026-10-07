import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, when } from '../lib/api'
import { Banner, RiskBadge } from '../components/ui'
import { CaseIcon, CaseMetric } from '../components/CaseWorkspace'
import { DECLINE_REASON, say } from '../lib/words'

const CHANNELS = { MOBILE_APP: 'Mobile app', WEB: 'Web', USSD: 'USSD', POS: 'POS', ATM: 'ATM', AGENT: 'Agent', BRANCH: 'Branch', API: 'API' }
const RESULTS = [
  { key: '', label: 'All transactions' },
  { key: 'APPROVED', label: 'Approved' },
  { key: 'DECLINED', label: 'Declined' },
  { key: 'FAILED', label: 'Failed' },
  { key: 'REVERSED', label: 'Reversed' },
]
const EMPTY_FILTERS = { q: '', channel: '', auth_result: '', risk_level: '' }
const PAGE_SIZE = 15
const resultTone = (result) => result === 'APPROVED' ? 'ok' : ['DECLINED', 'FAILED'].includes(result) ? 'danger' : 'warn'
const label = (value) => value ? value[0] + value.slice(1).toLowerCase() : 'Unknown'
const amount = (t) => new Intl.NumberFormat('en-NG', {
  style: 'currency', currency: t.currency || 'NGN', maximumFractionDigits: 2,
}).format(Number(t.amount_minor) / 100)

export default function Transactions() {
  const [filters, setFilters] = useState(EMPTY_FILTERS)
  const [searchTerm, setSearchTerm] = useState('')
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const requestId = useRef(0)

  useEffect(() => {
    const timer = setTimeout(() => { setSearchTerm(filters.q.trim()); setPage(1) }, 250)
    return () => clearTimeout(timer)
  }, [filters.q])

  const search = useCallback(async () => {
    const id = ++requestId.current
    setLoading(true); setError(null)
    try {
      const params = Object.fromEntries(Object.entries({
        q: searchTerm, channel: filters.channel, auth_result: filters.auth_result, risk_level: filters.risk_level,
      }).filter(([, value]) => value))
      // Fetch one extra row to find out whether there is another page. The API
      // does not return a total, so do not imply that this page is the full ledger.
      const result = await api.searchTransactions({ ...params, limit: PAGE_SIZE + 1, offset: (page - 1) * PAGE_SIZE })
      if (id !== requestId.current) return
      setData({ items: result.items.slice(0, PAGE_SIZE), hasNext: result.items.length > PAGE_SIZE, page })
    } catch (err) { if (id === requestId.current) setError(err.message) }
    finally { if (id === requestId.current) setLoading(false) }
  }, [searchTerm, filters.channel, filters.auth_result, filters.risk_level, page])

  useEffect(() => {
    search()
    return () => { requestId.current += 1 }
  }, [search])

  const set = (key, value) => { setFilters((previous) => ({ ...previous, [key]: value })); setPage(1) }
  const reset = () => { setFilters(EMPTY_FILTERS); setSearchTerm(''); setPage(1) }
  const items = data?.items ?? []
  const updating = loading || filters.q.trim() !== searchTerm
  const filtered = Object.values(filters).some(Boolean)
  const approved = items.filter((t) => t.auth_result === 'APPROVED').length
  const declined = items.filter((t) => ['DECLINED', 'FAILED'].includes(t.auth_result)).length
  const reversed = items.filter((t) => t.auth_result === 'REVERSED').length
  const shownPage = data?.page ?? page
  const start = (shownPage - 1) * PAGE_SIZE

  return (
    <div className="page ops-dashboard cases-workspace transactions-workspace">
      <div className="page-head ops-page-head">
        <div>
          <div className="ops-eyebrow">Transaction monitoring</div>
          <h1>Transactions</h1>
          <p className="page-sub">Trace every payment. Find the activity behind each investigation.</p>
        </div>
        <div className="ops-head-actions">
          <Link className="ops-action" to="/triage">Review cases <CaseIcon name="arrow" /></Link>
          <button className="ops-action primary" onClick={search} disabled={updating}>
            <CaseIcon name="refresh" /> Refresh
          </button>
        </div>
      </div>

      {error && <Banner kind="error">{error}{data && ' Previous results are still shown.'}
        <button className="ghost" onClick={search} disabled={updating}>Retry</button>
      </Banner>}

      <div className="statrow">
        <CaseMetric label="Transactions in view" value={data ? items.length.toLocaleString() : null}
          note="Current page · newest first" icon="transfer" featured />
        <CaseMetric label="Approved" value={data ? approved.toLocaleString() : null}
          note="Successful authorisations on this page" icon="check" tone="transaction-success" />
        <CaseMetric label="Declined / failed" value={data ? declined.toLocaleString() : null}
          note="Unsuccessful attempts on this page" icon="declined" tone={declined ? 'danger' : ''} />
        <CaseMetric label="Reversed" value={data ? reversed.toLocaleString() : null}
          note="Reversed transactions on this page" icon="reversed" />
      </div>

      <section className="card cases-panel" aria-labelledby="transactions-title">
        <div className="cases-panel-head">
          <div><h2 id="transactions-title">Transaction history</h2><p>Search across customers, channels and payment outcomes.</p></div>
          <span className="transaction-load-status" role="status"><CaseIcon name="clock" />
            {updating ? 'Updating results…' : error ? 'Update unavailable' : 'Newest first'}
          </span>
        </div>
        <div className="case-tabs" role="group" aria-label="Transaction result">
          {RESULTS.map((result) => <button key={result.key} className={filters.auth_result === result.key ? 'on' : ''}
            aria-pressed={filters.auth_result === result.key} onClick={() => set('auth_result', result.key)}>{result.label}</button>)}
        </div>
        <div className="cases-filterbar transaction-filterbar">
          <div className="searchbar cases-search">
            <CaseIcon name="search" />
            <input type="search" aria-label="Search transactions" placeholder="Search reference or customer…"
              value={filters.q} onChange={(e) => setFilters((previous) => ({ ...previous, q: e.target.value }))} />
          </div>
          <div className="cases-filter-actions">
            <select aria-label="Filter by channel" value={filters.channel} onChange={(e) => set('channel', e.target.value)}>
              <option value="">All channels</option>
              {Object.entries(CHANNELS).map(([value, text]) => <option key={value} value={value}>{text}</option>)}
            </select>
            <select aria-label="Filter by risk" value={filters.risk_level} onChange={(e) => set('risk_level', e.target.value)}>
              <option value="">All risk levels</option>
              {['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map((level) => <option key={level} value={level}>{label(level)}</option>)}
            </select>
          </div>
        </div>
        {filtered && <div className="transaction-active-filters">
          <span>Filtered by</span>
          {filters.q && <span className="tag">Search: {filters.q}</span>}
          {filters.auth_result && <span className="tag">{label(filters.auth_result)}</span>}
          {filters.channel && <span className="tag">{CHANNELS[filters.channel]}</span>}
          {filters.risk_level && <span className="tag">{label(filters.risk_level)} risk</span>}
          <button className="ghost" onClick={reset}>Clear filters</button>
        </div>}

        <div className="table-scroll" role="region" aria-label="Scrollable transaction history" tabIndex={0} aria-busy={updating}>
          <table className="rowtable cases-table transactions-table" aria-label="Transaction history">
            <thead><tr>
              <th scope="col">Transaction / Customer</th><th scope="col">Date & time</th><th scope="col" className="num">Amount</th>
              <th scope="col">Channel</th><th scope="col">Result</th><th scope="col">Risk / Score</th><th scope="col">Linked case</th>
            </tr></thead>
            <tbody>
              {items.map((t) => <tr key={t.id}>
                <td><div className="case-customer">
                  <span className={`case-customer-icon transaction-direction ${t.direction === 'INBOUND' ? 'inbound' : 'outbound'}`} aria-hidden="true"><CaseIcon name="arrow" /></span>
                  <div className="transaction-identity">
                    <span className="transaction-customer">{t.display_name || 'Unknown customer'}</span>
                    <span className="transaction-reference" title={t.transaction_ref}>{t.transaction_ref}</span>
                  </div>
                </div></td>
                <td><time className="transaction-time" dateTime={t.occurred_at} title={when(t.occurred_at)}>
                  {t.occurred_at ? new Date(t.occurred_at).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }) : '—'}
                  <span className="case-cell-sub">{t.occurred_at ? new Date(t.occurred_at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : ''}</span>
                </time></td>
                <td className="num"><span className="transaction-amount">{t.direction === 'INBOUND' ? '+' : ''}{amount(t)}</span>
                  <span className="case-cell-sub transaction-amount-note">{t.direction === 'INBOUND' ? 'Incoming' : t.direction === 'OUTBOUND' ? 'Outgoing' : '—'}</span></td>
                <td><span className="transaction-channel">{CHANNELS[t.channel] || t.channel || '—'}</span></td>
                <td><span className="authdot"><span className={`live-dot ${resultTone(t.auth_result)}`} />{label(t.auth_result)}</span>
                  {t.decline_reason && <span className="transaction-decline-reason" title={t.decline_reason}>{say(DECLINE_REASON, t.decline_reason)}</span>}</td>
                <td><RiskBadge level={t.risk_level} /><span className="case-cell-sub">{t.score_0_100 == null ? 'Not scored' : `Score ${t.score_0_100} / 100`}</span></td>
                <td>{t.case_id ? <Link className="transaction-case-link" to={`/cases/${t.case_id}`}>CASE-{t.case_id}<CaseIcon name="arrow" /></Link>
                  : <span className="transaction-no-case">No linked case</span>}</td>
              </tr>)}
            </tbody>
          </table>
          {!items.length && <div className="cases-empty" role="status">
            <span className="cases-empty-icon"><CaseIcon name={filtered ? 'search' : 'transfer'} /></span>
            <strong>{updating ? 'Loading transactions…' : error ? 'Unable to load transactions' : filtered ? 'No matching transactions' : 'No transactions yet'}</strong>
            <p>{updating ? 'Finding the latest activity for this view.' : error ? 'Use Retry above to load the transaction history.' : filtered ? 'Try another reference, customer or filter.' : 'Transaction activity will appear here when it is available.'}</p>
            {filtered && !updating && !error && <button className="ops-action" onClick={reset}>Clear filters</button>}
          </div>}
        </div>
        <div className="cases-table-foot">
          <span>{data ? items.length ? `Showing ${start + 1}–${start + items.length} transactions` : '0 transactions' : 'Waiting for transactions'}</span>
          <div className="cases-pagination" aria-label="Transaction pagination">
            <button aria-label="Previous page" disabled={updating || !!error || shownPage === 1} onClick={() => setPage(shownPage - 1)}>‹</button>
            <span>Page {shownPage}</span>
            <button aria-label="Next page" disabled={updating || !!error || !data?.hasNext} onClick={() => setPage(shownPage + 1)}>›</button>
          </div>
        </div>
      </section>
      <p className="cases-footnote"><CaseIcon name="transfer" /> Summary counts reflect the current page. Approved, declined, failed and reversed activity is included.</p>
    </div>
  )
}
