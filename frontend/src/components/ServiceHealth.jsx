import { when } from '../lib/api'
import ReadStatus from './ReadStatus'

const CHECK_LABELS = { database: 'Database', model: 'Model registration', ruleset: 'Detection rules', thresholds: 'Risk thresholds', workers: 'Scoring workers', queue: 'Processing queue', dev_credentials: 'Production credentials' }
const ADAPTER_LABELS = { core_resolver: 'Core banking identity', identity_registry: 'Identity verification', industry_connector: 'Industry watchlist' }
export function adapterDescription(name) {
  return ({ none: 'Not connected', fixture: 'Local customer file · demonstration', format: 'Format checks only · identity not verified', loopback: 'Simulated responses · no external bank connection', finacle: 'Finacle adapter · client not implemented', nibss: 'NIBSS adapter · client not implemented' })[name]
    || (name ? `${name} configured · connectivity not verified` : 'Unavailable')
}

export default function ServiceHealth({ resource, integrations }) {
  const d = resource.data
  const problems = Object.entries(d?.checks || {}).filter(([, c]) => !c.ok)
  const status = resource.error ? 'Status unavailable — last read may be stale'
    : !d ? 'Checking service'
      : !d.ready ? 'Processing needs attention'
        : problems.length ? 'Processing available with a warning' : 'Readiness checks passed'
  return <section className="card service-health" aria-labelledby="service-title">
    <div className="between wrap">
      <div><h2 id="service-title">Service status</h2><p className="dim">{status}</p></div>
      {resource.updatedAt && <span className="dim">Checked {when(resource.updatedAt)}</span>}
    </div>
    <ReadStatus resource={resource} label="Service status" />
    {d && <>
      <div className="service-facts">
        <span><strong>{d.checks.workers?.alive ?? '—'}</strong> scoring workers available</span>
        <span><strong>{d.checks.queue?.live ?? '—'}</strong> live transactions queued</span>
        <span><strong>{d.checks.queue?.oldest_live_seconds ?? '—'}s</strong> oldest live queue wait</span>
        <span><strong>{d.checks.queue?.retrying ?? '—'}</strong> transactions retrying</span>
      </div>
      {problems.length > 0 && <ul className="service-problems">{problems.map(([key, c]) => <li key={key}>
        <strong>{CHECK_LABELS[key] || key}:</strong> {c.note || 'Readiness check failed. Investigate the service before relying on new decisions.'}
      </li>)}</ul>}
      <details className="ops-detail"><summary>Service checks and recent alarms</summary>
        <div className="row wrap">{Object.entries(d.checks).map(([key, c]) => <span className={`pill ${c.ok ? 'suppress' : 'escalate'}`} key={key}>{CHECK_LABELS[key] || key}: {c.ok ? 'passed' : 'attention'}</span>)}</div>
        <p className="dim">Last live transaction received: {when(d.stream?.last_live_at)}. Model: {d.in_force?.model?.name || 'none registered'}. These checks do not verify bank connectivity or guarantee model predictions.</p>
        <h3>Recent alarms · last 24 hours</h3>
        {d.recent_alarms?.length ? <ul>{d.recent_alarms.map((a) => <li key={a.id}><strong>{a.code}</strong> · {when(a.created_at)}<p>{a.detail}</p></li>)}</ul> : <p className="dim">No alarms recorded in this period.</p>}
      </details>
    </>}
    <details className="ops-detail">
      <summary>Bank connections and identity coverage</summary>
      <ReadStatus resource={integrations} label="Bank connections" />
      {integrations.data && <>
        <dl className="connection-list">{Object.entries(ADAPTER_LABELS).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{adapterDescription(integrations.data.adapters?.[key])}</dd></div>)}</dl>
        <p className="dim">Customers with a BVN token: {integrations.data.bvn_coverage_30d?.with_bvn ?? '—'} of {integrations.data.bvn_coverage_30d?.customers ?? '—'} active customers in the last 30 days. A token is not proof of verified identity.</p>
        {integrations.data.outbox?.filter((o) => o.status !== 'SENT' && Number(o.n) > 0).map((o) => <p className="service-problems" key={o.status}>{o.n} watchlist messages {o.status.toLowerCase()}. {o.last_error}</p>)}
      </>}
    </details>
  </section>
}
