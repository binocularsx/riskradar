import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { api, nairaShort } from '../lib/api'
import { Banner, RiskBadge } from '../components/ui'
import CaseView from '../components/CaseView'

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

  useEffect(() => {
    let cancelled = false
    // The worklist item carries the header fields CaseView needs (exposure, SLA,
    // recommendation). Fetch across all scopes so a direct link resolves.
    api.worklist({ scope: 'all', limit: 200 })
      .then((d) => {
        if (cancelled) return
        const found = d.items.find((c) => c.id === caseId)
        if (found) setSummary(found)
        else setError('That case is not on the desk, or is outside what you may see.')
      })
      .catch((e) => !cancelled && setError(e.message))
    return () => { cancelled = true }
  }, [caseId])

  return (
    <div className="page">
      {/* The breadcrumb did go back, but as faint grey text nobody reads as a
          control. A case is opened from the queue and returned to it dozens of
          times a shift, so the way back is a button that looks like one. */}
      <div className="crumb">
        <button className="backbtn" onClick={() => navigate('/triage')}
                title="Back to the case queue">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
               strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M19 12H5" /><path d="m12 19-7-7 7-7" />
          </svg>
          Case queue
        </button>
        <span className="dim"> / </span><span className="mono">CASE-{caseId}</span>
      </div>
      {summary && (
        <div className="case-topbar">
          <div className="case-id-row">
            <h1 className="mono">CASE-{caseId}</h1>
            <RiskBadge level={summary.risk_level} />
            <span className="pill">{(summary.state || 'open').replace(/_/g, ' ').toLowerCase()}</span>
            {summary.handling === 'MACHINE' && <span className="pill suppress">machine action</span>}
            {summary.watchlisted && <span className="pill escalate">flagged</span>}
          </div>
          <div className="case-exposure">
            <span className="dim">Exposure</span>
            <strong>{nairaShort(summary.exposure_minor)}</strong>
          </div>
        </div>
      )}
      {error && <Banner kind="error">{error}</Banner>}
      {!error && !summary && <p className="muted">Loading case…</p>}
      {summary && (
        <CaseView
          summary={summary}
          user={user}
          onDisposed={(result) => {
            if (result?.refreshOnly) return
            // Outcome recorded or case closed → return to the queue.
            navigate('/triage')
          }}
          onSkip={() => navigate('/triage')}
        />
      )}
    </div>
  )
}
