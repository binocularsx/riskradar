import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api, nairaShort } from '../lib/api'
import { Banner, RiskBadge } from '../components/ui'
import CaseView from '../components/CaseView'
import { CaseIcon } from '../components/CaseWorkspace'

/**
 * Case detail (Figma "CASE-1042"): the full-page investigation. The queue opens
 * this; the CaseView holds the evidence, the recommendation, the workflow and
 * the disposition. Loads the case summary the queue passed, or fetches it when
 * the URL is opened directly.
 */
export default function CaseDetail({ user }) {
  const { id } = useParams()
  const caseId = Number(id)
  const navigate = useNavigate()
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)
  const [revision, setRevision] = useState(0)

  useEffect(() => {
    let cancelled = false
    setSummary((previous) => previous?.id === caseId ? previous : null)
    setError(null)
    // The worklist item carries the header fields CaseView needs (exposure, SLA,
    // recommendation). Fetch across all scopes so a direct link resolves.
    api.worklist({ scope: 'all', limit: 200 })
      .then(async (d) => {
        if (cancelled) return
        const found = d.items.find((c) => c.id === caseId)
        if (found) setSummary(found)
        else {
          // Completed cases and cases beyond the queue limit still have an
          // authorised detail endpoint. Keep tracker links usable for both.
          const detail = await api.caseDetail(caseId)
          if (cancelled) return
          const transactions = [...new Map(detail.alerts.map((a) => [a.transaction_id, a])).values()]
          setSummary({
            ...detail.case,
            customer_name: transactions[0]?.display_name || 'Unknown customer',
            exposure_minor: transactions.filter((a) => a.auth_result === 'APPROVED').reduce((sum, a) => sum + Number(a.amount_minor), 0),
            attempted_minor: transactions.reduce((sum, a) => sum + Number(a.amount_minor), 0),
            declined_count: transactions.filter((a) => a.auth_result === 'DECLINED').length,
            channels: [...new Set(transactions.map((a) => a.channel).filter(Boolean))],
          })
        }
      })
      .catch((e) => !cancelled && setError(e.message))
    return () => { cancelled = true }
  }, [caseId, revision])

  return (
    <div className="page ops-dashboard cases-workspace case-detail-workspace">
      <header className="investigation-header">
        <div className="investigation-header-main">
          <div>
            <nav className="investigation-breadcrumb" aria-label="Breadcrumb">
              <Link to="/triage">Cases</Link><span aria-hidden="true">/</span><span>CASE-{caseId}</span>
            </nav>
            <h1>Case investigation</h1>
            <p>Review the evidence, record your findings and move the case forward.</p>
          </div>
          <div className="ops-head-actions">
            <Link className="ops-action" to="/triage">Back to queue</Link>
            {summary && <a className="ops-action primary" href="#investigation-resolution">
              {summary.state === 'CLOSED' || summary.outcome ? 'View workflow' : 'Review outcome'}<CaseIcon name="arrow" />
            </a>}
          </div>
        </div>
        {summary && <div className="investigation-header-meta">
          <div className="investigation-case-label"><CaseIcon name="cases" /><strong>CASE-{caseId}</strong>
            <RiskBadge level={summary.risk_level} />
            <span className="pill">{(summary.state || 'open').replace(/_/g, ' ').toLowerCase()}</span>
            {summary.handling === 'MACHINE' && <span className="pill suppress">System handled</span>}
            {summary.watchlisted && <span className="pill escalate">Watchlisted</span>}
          </div>
          <div className="investigation-header-exposure"><span>Money at risk</span><strong>{nairaShort(summary.exposure_minor)}</strong></div>
        </div>}
      </header>
      {error && <Banner kind="error">{error} <button className="ghost" onClick={() => setRevision((r) => r + 1)}>Retry</button></Banner>}
      {!error && !summary && <div className="card cases-empty" role="status"><CaseIcon name="cases" /><strong>Loading investigation…</strong><p>Gathering the case summary and supporting evidence.</p></div>}
      {summary && (
        <CaseView
          summary={summary}
          user={user}
          onDisposed={(result) => {
            if (result?.refreshOnly) { setRevision((r) => r + 1); return }
            // Outcome recorded or case closed → return to the queue.
            navigate('/triage')
          }}
          onSkip={() => navigate('/triage')}
        />
      )}
    </div>
  )
}
