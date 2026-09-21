import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { api } from '../lib/api'
import { Banner } from '../components/ui'
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
    api.worklist({ scope: 'all', limit: 300 })
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
      <button className="ghost backlink" onClick={() => navigate('/triage')}>← Back to Case Queue</button>
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
