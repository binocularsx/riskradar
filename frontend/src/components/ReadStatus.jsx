import { when } from '../lib/api'
import { Banner } from './ui'

export default function ReadStatus({ resource, label }) {
  if (resource.error) return <Banner kind="error">
    <strong>{label} could not be refreshed.</strong> {resource.error}
    {resource.updatedAt && <> Showing the last successful read from {when(resource.updatedAt)}.</>}
    <button className="ghost" onClick={resource.refresh} disabled={resource.loading}>Retry</button>
  </Banner>
  if (!resource.data) return <p className="muted" role="status">Loading {label.toLowerCase()}…</p>
  return null
}
