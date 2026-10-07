import { useCallback, useEffect, useState } from 'react'

import { adapterDescription } from '../components/ServiceHealth'
import { api, when } from '../lib/api'
import { Banner, Empty, RiskBadge } from '../components/ui'

const TABS = ['System overview', 'Rules', 'Thresholds', 'Pending', 'Models', 'Lists', 'Enforcement', 'Integrations', 'Access & audit']

/**
 * Administration (FR-040 to FR-042).
 *
 * Every change here creates a **new version** rather than editing the active one.
 * Decisions record the ruleset and threshold version that applied, so mutating a
 * version decisions already point at would make those decisions unreproducible —
 * quietly, and only discoverable months later when somebody asks why an alert
 * fired.
 *
 * Note what this screen cannot do: open a case. ADMIN holds no case permission
 * at all (D12b). The account that tunes detection is not the account that clears
 * what detection misses.
 */
export default function Admin() {
  const [tab, setTab] = useState('System overview')
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Administration</h1>
          <p className="page-sub">Detection, oversight and access — every change versioned and audited.</p>
        </div>
      </div>
      <div className="tabs">
        {TABS.map((t) => (
          <button key={t} className={`tab ${tab === t ? 'active' : ''}`} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </div>
      {tab === 'System overview' && <SystemOverview go={setTab} />}
      {tab === 'Rules' && <Rules />}
      {tab === 'Thresholds' && <Thresholds />}
      {tab === 'Pending' && <PendingChanges />}
      {tab === 'Models' && <Models />}
      {tab === 'Lists' && <Lists />}
      {tab === 'Enforcement' && <Enforcement />}
      {tab === 'Integrations' && <Integrations />}
      {tab === 'Access & audit' && <Audit />}
    </div>
  )
}

/* -------------------------------------------------- system overview (Figma) */

/**
 * The Administration landing (Figma "System Overview"): the state of detection at
 * a glance — the live model, the active ruleset and thresholds, the alert budget
 * — and the recent configuration changes, which are the audited, maker-checked
 * changes to any of it (D96–D99).
 */
function SystemOverview({ go }) {
  const { data, error } = useAsync(() =>
    Promise.all([api.models(), api.rules(), api.thresholds(), api.audit({ limit: 12 })]))
  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>
  const [models, rules, thresholds, audit] = data
  const active = models.items.find((m) => m.is_active)
  const activeThresh = thresholds.versions?.find((v) => v.is_active)
  const enabledRules = rules.rules.filter((r) => r.enabled).length
  const overdue = rules.rules.filter((r) => r.review_overdue).length

  return (
    <>
      <div className="statrow" style={{ marginTop: 4 }}>
        <div className="statcard">
          <div className="statcard-k">Active model</div>
          <div className="statcard-v" style={{ fontSize: 20 }}>{active?.name ?? 'none'}</div>
          <div className="dim mono" style={{ fontSize: 11, marginTop: 4 }}>{active?.version ?? '—'}</div>
        </div>
        <div className="statcard">
          <div className="statcard-k">Active rules</div>
          <div className="statcard-v">{enabledRules}</div>
          <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>ruleset v{rules.ruleset.version}</div>
        </div>
        <div className="statcard">
          <div className="statcard-k">Thresholds</div>
          <div className="statcard-v" style={{ fontSize: 20 }}>v{activeThresh?.version ?? '—'}</div>
          <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>alert ≥ {activeThresh?.alert_min_level ?? '—'}</div>
        </div>
        <div className="statcard">
          <div className="statcard-k">Reviews overdue</div>
          <div className={`statcard-v ${overdue ? 'warn' : ''}`}>{overdue}</div>
          <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>of {rules.rules.length} rules</div>
        </div>
      </div>

      <div className="card" style={{ padding: 0 }}>
        <div className="toolbar">
          <strong style={{ fontSize: 14 }}>Recent configuration changes</strong>
          <button onClick={() => go('Pending')}>Pending approvals</button>
        </div>
        <div className="table-scroll">
          <table className="rowtable">
            <thead>
              <tr><th>When</th><th>Actor</th><th>Change</th><th>Object</th></tr>
            </thead>
            <tbody>
              {audit.items.map((a) => (
                <tr key={a.id}>
                  <td className="mono dim" style={{ fontSize: 11.5 }}>{when(a.occurred_at)}</td>
                  <td>{a.actor} <span className="dim">({a.actor_role})</span></td>
                  <td className="mono">{a.action}</td>
                  <td className="muted">{a.object_type} {a.object_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!audit.items.length && <div className="empty">No changes recorded yet.</div>}
        </div>
      </div>
    </>
  )
}

function useAsync(fn, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const load = useCallback(() => {
    fn().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }, deps) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { load() }, [load])
  return { data, error, reload: load, setError }
}

/* ----------------------------------------------------------- integrations */

/**
 * D75: the three outside systems identity depends on, and what is waiting on
 * them. A message that cannot be sent yet is shown with its reason, not hidden.
 */
function Integrations() {
  const { data, error, reload } = useAsync(api.integrations)
  if (error) return <Banner kind="error">{error} <button onClick={reload}>Retry</button></Banner>
  if (!data) return <p className="muted">Loading…</p>
  const integrations = data
  const cov = integrations.bvn_coverage_30d
  return (
    <>
      <div className="grid cols-4" style={{ marginBottom: 14 }}>
        {[
          ['Core banking (BVN lookup)', integrations.adapters.core_resolver],
          ['Identity registry', integrations.adapters.identity_registry],
          ['Industry watch-list', integrations.adapters.industry_connector],
        ].map(([label, value]) => (
          <div className="card" key={label}>
            <div className="stat-label">{label}</div>
            <div style={{ marginTop: 10 }}>{adapterDescription(value)}</div>
          </div>
        ))}
      </div>
      <div className="card" style={{ marginBottom: 14 }}>
        <h2>Identity</h2>
        <p style={{ fontSize: 13, marginTop: 0 }}>
          {cov.with_bvn} of {cov.customers} customers seen in 30 days have a known BVN.
          {' '}Institution code <span className="mono">{integrations.adapters.institution_code}</span>.
        </p>
        <div className="row wrap" style={{ gap: 8 }}>
          {integrations.verification.map((v) => <span key={v.status} className="pill">{v.status.toLowerCase()} {v.n}</span>)}
        </div>
      </div>
      <div className="card" style={{ marginBottom: 14 }}>
        <h2>Industry watch-list</h2>
        {integrations.outbox.length ? (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Outbox</th><th className="num">Messages</th><th>Oldest</th><th>Last reason</th></tr></thead>
              <tbody>
                {integrations.outbox.map((o) => (
                  <tr key={o.status}>
                    <td className="mono">{o.status}</td><td className="num">{o.n}</td>
                    <td className="mono dim">{when(o.oldest)}</td><td className="dim">{o.last_error || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>No flags placed yet.</Empty>}
        <p className="dim" style={{ fontSize: 12, marginBottom: 0 }}>
          Received from other institutions: {integrations.inbound.active} in force, {integrations.inbound.total} in all
          {integrations.inbound.last_received ? `, last ${when(integrations.inbound.last_received)}` : ''}.
        </p>
      </div>
      <p className="dim">Bank action delivery is monitored by case-authorised staff in Operations.</p>
    </>
  )
}

/* ------------------------------------------------------------ enforcement */

/**
 * WP-07 (D74): the directive contract a bank would wire in. Read-only here on
 * purpose: publishing a policy is an API call with a signature attached, and
 * LIVE is refused by the database without one (D69a).
 */
function Enforcement() {
  const { data, error } = useAsync(() => Promise.all([api.enforcementPolicy(), api.directiveMetrics(7)]))
  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>
  const [policy, metrics] = data
  const active = policy.active
  return (
    <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="between">
          <h2 style={{ margin: 0 }}>Enforcement policy v{active?.version ?? '—'}</h2>
          <span className={`pill ${active?.mode === 'LIVE' ? 'suppress' : ''}`}>{active?.mode ?? 'none'}</span>
        </div>
        {active && (
          <>
            <p className="dim" style={{ fontSize: 12.5 }}>
              A directive may be acted on for {active.ttl_seconds} seconds after it is issued. Late, expired or
              missing: {active.fail_open_action.replace(/_/g, ' ').toLowerCase()} (fail open).
              {active.signed_by ? ` Signed by ${active.signed_by}, ${when(active.signed_at)}.` : ' Not signed: LIVE is unavailable.'}
            </p>
            <p style={{ fontSize: 13, whiteSpace: 'pre-wrap', marginBottom: 0 }}>{active.policy_text}</p>
          </>
        )}
      </div>
      <div className="card">
        <h2>Directives, last {metrics.window_days} days</h2>
        {metrics.by_action.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>Action</th><th>Mode</th><th className="num">Issued</th><th className="num">Delivered</th>
                    <th className="num">In time</th><th className="num">Acknowledged</th></tr>
              </thead>
              <tbody>
                {metrics.by_action.map((r) => (
                  <tr key={`${r.action}-${r.mode}`}>
                    <td className="mono">{r.action}</td><td>{r.mode}</td>
                    <td className="num">{r.issued}</td><td className="num">{r.delivered}</td>
                    <td className="num">{r.delivered_in_time}</td><td className="num">{r.acknowledged}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>No directives issued in this window. They are written for live, not replayed, payments.</Empty>}
        <p className="dim" style={{ fontSize: 11.5, marginBottom: 0 }}>
          Median time to first delivery {metrics.timing.median_seconds_to_delivery ?? '—'}s ·
          p95 {metrics.timing.p95_seconds_to_delivery ?? '—'}s. Risk Radar never enforces a directive; the bank does,
          and only under a signed LIVE policy.
        </p>
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ rules */

const prettyRule = (code) => code.split('_').map((w) => w[0] + w.slice(1).toLowerCase()).join(' ')
const paramSummary = (params) => Object.entries(params || {})
  .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join('/') : v}`).join(' · ') || '—'

/**
 * Detection Configuration › Rule Engine (Figma): the rules as a table. Every
 * change is *proposed* and a second administrator approves it (D96); the toggle
 * asks for the reason it records, since that reason is what the approver reads.
 */
function Rules() {
  const { data, error, reload, setError } = useAsync(() => api.rules())
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState(null)

  async function toggle(rule) {
    const reason = window.prompt(
      `${rule.enabled ? 'Disable' : 'Enable'} ${rule.code}?\nReason (a second administrator approves it):`)
    if (!reason || !reason.trim()) return
    setBusy(true); setNotice(null)
    try {
      const res = await api.updateRule(rule.code, { enabled: !rule.enabled, rationale: reason.trim() })
      if (res?.status === 'pending') {
        setNotice(`Proposed change #${res.request.id} to ${rule.code}. It applies once a different administrator approves it (Pending tab).`)
      }
      reload()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>
  const enabled = data.rules.filter((r) => r.enabled).length
  const overdue = data.rules.filter((r) => r.review_overdue).length

  return (
    <>
      {notice && <Banner kind="ok">{notice}</Banner>}
      <div className="statrow" style={{ marginTop: 4 }}>
        <div className="statcard"><div className="statcard-k">Active rules</div><div className="statcard-v" style={{ color: 'var(--ok)' }}>{enabled}</div><div className="dim" style={{ fontSize: 11, marginTop: 4 }}>ruleset v{data.ruleset.version}</div></div>
        <div className="statcard"><div className="statcard-k">Disabled</div><div className="statcard-v">{data.rules.length - enabled}</div></div>
        <div className="statcard"><div className="statcard-k">Reviews overdue</div><div className={`statcard-v ${overdue ? 'warn' : ''}`}>{overdue}</div></div>
        <div className="statcard"><div className="statcard-k">Total rules</div><div className="statcard-v">{data.rules.length}</div></div>
      </div>

      <div className="card" style={{ padding: 0 }}>
        <div className="toolbar">
          <strong style={{ fontSize: 14 }}>Rule engine</strong>
          <span className="dim" style={{ fontSize: 12 }}>A change is proposed; a second administrator approves it (D96).</span>
        </div>
        <div className="table-scroll">
          <table className="rowtable">
            <thead>
              <tr><th>Rule</th><th>Type</th><th>Severity</th><th>Parameters</th><th>Status</th><th>Last modified</th><th></th></tr>
            </thead>
            <tbody>
              {data.rules.map((rule) => (
                <tr key={rule.code}>
                  <td>
                    <div style={{ fontWeight: 560 }}>{prettyRule(rule.code)}</div>
                    <div className="mono dim" style={{ fontSize: 11 }}>{rule.code}</div>
                  </td>
                  <td><span className={`pill ${rule.power.toLowerCase()}`}>{rule.power.toLowerCase()}</span></td>
                  <td><RiskBadge level={rule.severity} /></td>
                  <td className="reco-cell mono" style={{ fontSize: 11 }}>{paramSummary(rule.params)}</td>
                  <td>
                    <span className="authdot"><span className={`live-dot ${rule.enabled ? 'ok' : ''}`} style={!rule.enabled ? { background: 'var(--text-3)', boxShadow: 'none' } : {}} />{rule.enabled ? 'Active' : 'Disabled'}</span>
                    {(rule.review_overdue || rule.orphaned || rule.dormant) && (
                      <div className="cellflags">
                        {rule.orphaned && <span className="risk risk-HIGH">no owner</span>}
                        {rule.review_overdue && <span className="risk risk-HIGH">review overdue</span>}
                        {rule.dormant && <span className="risk risk-MEDIUM">dormant</span>}
                      </div>
                    )}
                  </td>
                  <td className="dim" style={{ fontSize: 11.5 }}>
                    {rule.approved_at ? when(rule.approved_at) : '—'}
                    {rule.approved_by && <div className="mono" style={{ fontSize: 10.5 }}>{rule.approved_by}</div>}
                  </td>
                  <td className="num"><button disabled={busy} onClick={() => toggle(rule)}>{rule.enabled ? 'Disable' : 'Enable'}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="table-foot dim">{data.rules.length} rules in ruleset v{data.ruleset.version}. Threshold and list changes are the other tabs.</div>
      </div>
    </>
  )
}

/* ------------------------------------------------------------- thresholds */

function Thresholds() {
  const { data, error, reload, setError } = useAsync(() => api.thresholds())
  const [form, setForm] = useState(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState(null)

  useEffect(() => {
    const active = data?.versions?.find((v) => v.is_active)
    if (active && !form) {
      setForm({
        p_monitor: Number(active.p_monitor),
        p_review: Number(active.p_review),
        p_hold: Number(active.p_hold),
        alert_min_level: active.alert_min_level,
        notes: '',
      })
    }
  }, [data]) // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data || !form) return <p className="muted">Loading…</p>

  async function save() {
    if (!(form.notes || '').trim()) {
      setError('A reason is required — a second administrator reads it before approving.')
      return
    }
    setBusy(true)
    setNotice(null)
    try {
      const res = await api.createThresholds(form)
      if (res?.status === 'pending') {
        setNotice(`Proposed threshold change #${res.request.id}. It publishes once a different administrator approves it (Pending tab).`)
      }
      reload()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <>
      <Banner kind="info">
        Risk limits are calculated from the <strong>daily review limit</strong> —
        analysts multiplied by reviewable alerts per day — then checked against
        recall. Never from "80 sounds high". <code>scripts/derive_thresholds.py</code>{' '}
        computes them from a scored sample. Publishing is <strong>proposed</strong> to a
        second administrator and applied only on approval (D96).
      </Banner>
      {notice && <Banner kind="ok">{notice}</Banner>}

      <div className="card" style={{ marginBottom: 14 }}>
        <h2>Publish a new threshold version</h2>
        <div className="grid cols-4">
          {['p_monitor', 'p_review', 'p_hold'].map((key) => (
            <div key={key}>
              <label>{key.replace('p_', 'P(fraud) → ')}</label>
              <input type="number" step="0.000001" min="0" max="1" value={form[key]}
                     onChange={(e) => setForm({ ...form, [key]: Number(e.target.value) })} />
            </div>
          ))}
          <div>
            <label>Alert at or above</label>
            <select value={form.alert_min_level}
                    onChange={(e) => setForm({ ...form, alert_min_level: e.target.value })}>
              {['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map((l) => <option key={l}>{l}</option>)}
            </select>
          </div>
        </div>
        <div style={{ marginTop: 10 }}>
          <label>Why (recorded in the audit trail)</label>
          <input value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
        </div>
        <button className="primary" style={{ marginTop: 10 }} disabled={busy} onClick={save}>
          Publish version
        </button>
      </div>

      <div className="card" style={{ padding: 0 }}>
        <div className="table-scroll">
          <table>
            <thead>
              <tr><th>Version</th><th className="num">Monitor</th><th className="num">Review</th>
                  <th className="num">Hold</th><th>Alert at</th><th>Created</th><th>Notes</th></tr>
            </thead>
            <tbody>
              {data.versions.map((v) => (
                <tr key={v.id}>
                  <td className="mono">v{v.version} {v.is_active && <span className="pill">active</span>}</td>
                  <td className="num mono">{Number(v.p_monitor).toFixed(4)}</td>
                  <td className="num mono">{Number(v.p_review).toFixed(4)}</td>
                  <td className="num mono">{Number(v.p_hold).toFixed(4)}</td>
                  <td className="muted">{v.alert_min_level}</td>
                  <td className="muted">{when(v.created_at)}</td>
                  <td className="dim">{v.notes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}

/* -------------------------------------------------- pending config changes */

/**
 * D96: detection tuning is maker-checker. A rule or threshold change proposed on
 * the Rules/Thresholds tabs waits here for a *different* administrator to approve
 * it. The server refuses a proposer approving their own change (403); the button
 * is disabled here too, but the control is the 403, not the disabled button.
 */
function PendingChanges() {
  const { data, error, reload, setError } = useAsync(() =>
    Promise.all([api.configChanges('PENDING'), api.me()]))
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState(null)

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>
  const [pending, me] = data

  async function decide(r, action) {
    let reason = null
    if (action !== 'APPROVE') {
      reason = window.prompt(`Reason to ${action.toLowerCase()} this change:`)
      if (!reason) return
    }
    setBusy(true); setNotice(null)
    try {
      const res = await api.decideConfigChange(r.id, { action, reason })
      // D99: a created user's TOTP provisioning URI is returned once, here, to
      // the approver — it is stored nowhere it can be read again. Hand it over.
      if (res?.totp_uri) {
        const what = r.change_type === 'USER_MFA_RESET' ? 'Authenticator re-issued' : 'User created'
        setNotice(`${what}. Give them this setup link, shown once and stored nowhere: ${res.totp_uri}`)
      }
      reload()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <>
      <Banner kind="info">
        Changes to detection and to accounts are proposed by one administrator and
        approved by another (D96, D98, D99). Even the account that tunes detection
        cannot tune it alone.
      </Banner>
      {notice && <Banner kind="ok"><span style={{ wordBreak: 'break-all' }}>{notice}</span></Banner>}
      {!pending.items.length && <Empty>No changes awaiting approval.</Empty>}
      {pending.items.map((r) => {
        const mine = r.proposed_by_id === me.id
        return (
          <div className="card" key={r.id} style={{ marginBottom: 12 }}>
            <div className="between">
              <div>
                <span className="pill">{r.change_type.replace('_', ' ')}</span>{' '}
                <strong>{r.summary}</strong>
                <div className="dim" style={{ fontSize: 12, marginTop: 4 }}>
                  Proposed by {r.proposed_by} · {when(r.proposed_at)}
                </div>
              </div>
              <div className="row" style={{ gap: 6 }}>
                <button className="primary" disabled={busy || mine}
                        title={mine ? 'You proposed this — a different administrator must approve it' : ''}
                        onClick={() => decide(r, 'APPROVE')}>Approve</button>
                <button disabled={busy || mine} onClick={() => decide(r, 'RETURN')}>Return</button>
                <button className="danger" disabled={busy || mine} onClick={() => decide(r, 'REJECT')}>Reject</button>
              </div>
            </div>
            <p style={{ fontSize: 13, marginTop: 8, marginBottom: 0 }}>{r.rationale}</p>
            {mine && <p className="dim" style={{ fontSize: 12, marginBottom: 0 }}>
              You proposed this change; it needs a second administrator.
            </p>}
          </div>
        )
      })}
    </>
  )
}

/* ----------------------------------------------------------------- models */

function Models() {
  const { data, error, reload, setError } = useAsync(() => api.models())
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState(null)

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>

  async function propose(m) {
    const reason = window.prompt('Why promote this model? A second administrator approves it (D98).')
    if (!reason) return
    setBusy(true); setNotice(null)
    try {
      const res = await api.promoteModel(m.id, reason)
      if (res?.status === 'pending') {
        setNotice(`Promotion of ${m.name}:${m.version} proposed (#${res.request.id}). It goes live once a different administrator approves it (Pending tab).`)
      }
      reload()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <>
      <Banner kind="info">
        Promotion repoints a pointer and is audited. It is <strong>proposed</strong> to a
        second administrator and goes live only on approval (D98). Rollback is proposing the
        previous version back — not a redeploy.
      </Banner>
      {notice && <Banner kind="ok">{notice}</Banner>}
      {data.items.map((m) => {
        const held = m.metrics?.held_out?.incident_level
        return (
          <div className="card" key={m.id} style={{ marginBottom: 12 }}>
            <div className="between">
              <div>
                <strong>{m.name}</strong> <span className="mono dim">{m.version}</span>{' '}
                {m.is_active && <span className="pill">active</span>}
                <div className="dim mono" style={{ fontSize: 11, marginTop: 4 }}>
                  {m.calibration} · features {m.feature_spec_version} ·
                  sha256 {String(m.artifact_hash).slice(0, 16)}…
                </div>
              </div>
              {!m.is_active && (
                <button disabled={busy} onClick={() => propose(m)}>Propose promotion</button>
              )}
            </div>
            {held && (
              <p style={{ fontSize: 13, marginBottom: 0, marginTop: 10 }}>
                Held-out typology <strong>{m.metrics.held_out_typology}</strong>:{' '}
                incident recall <strong>{held.incident_recall}</strong>{' '}
                ({held.incidents_caught}/{held.incidents} incidents) at the daily review limit.{' '}
                <span className="dim">
                  In-distribution PR-AUC {m.metrics.in_distribution?.pr_auc} — far higher, and
                  far less meaningful.
                </span>
              </p>
            )}
          </div>
        )
      })}
      {!data.items.length && <Empty>No models registered.</Empty>}
    </>
  )
}

/* ------------------------------------------------------------------ lists */

function Lists() {
  const { data, error, reload, setError } = useAsync(() => api.lists())
  const [form, setForm] = useState({ kind: 'SANCTIONED', beneficiary_account_id: '', account_id: '', note: '' })
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState(null)

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>

  async function add() {
    if (!(form.note || '').trim()) {
      setError('A reason (note) is required — a second administrator reads it before approving.')
      return
    }
    setBusy(true); setNotice(null)
    try {
      const res = await api.addListEntry({
        kind: form.kind,
        beneficiary_account_id: form.beneficiary_account_id,
        account_id: form.kind === 'ALLOWLIST' ? form.account_id : null,
        note: form.note,
      })
      setForm({ ...form, beneficiary_account_id: '', account_id: '', note: '' })
      if (res?.status === 'pending') {
        setNotice(`Entry proposed (#${res.request.id}). It joins the list once a different administrator approves it (Pending tab).`)
      }
      reload()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  async function remove(entry) {
    const reason = window.prompt('Why remove this entry? A second administrator approves it (D98).')
    if (!reason) return
    setBusy(true); setNotice(null)
    try {
      const res = await api.removeListEntry(entry.id, reason)
      if (res?.status === 'pending') {
        setNotice(`Removal proposed (#${res.request.id}). It is removed once a different administrator approves it (Pending tab).`)
      }
      reload()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <>
      <Banner kind="info">
        Entries are supplied as account numbers and stored as one-way HMAC tokens.
        Adding or removing one is <strong>proposed</strong> to a second administrator and
        applied only on approval (D98). Risk Radar can check membership; it can never
        resolve an entry back to an account.
      </Banner>
      {notice && <Banner kind="ok">{notice}</Banner>}

      <div className="card" style={{ marginBottom: 14 }}>
        <h2>Propose an entry</h2>
        <div className="row wrap">
          <select style={{ width: 180 }} value={form.kind}
                  onChange={(e) => setForm({ ...form, kind: e.target.value })}>
            <option value="SANCTIONED">Sanctioned (override)</option>
            <option value="KNOWN_MULE">Known mule (override)</option>
            <option value="ALLOWLIST">Allowlist (suppress)</option>
          </select>
          <input style={{ flex: 1, minWidth: 200 }} placeholder="Beneficiary account number"
                 value={form.beneficiary_account_id}
                 onChange={(e) => setForm({ ...form, beneficiary_account_id: e.target.value })} />
          {form.kind === 'ALLOWLIST' && (
            <input style={{ flex: 1, minWidth: 200 }} placeholder="Scoped to account number"
                   value={form.account_id}
                   onChange={(e) => setForm({ ...form, account_id: e.target.value })} />
          )}
          <input style={{ flex: 1, minWidth: 160 }} placeholder="Reason (read by the approver)"
                 value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
          <button className="primary" disabled={busy || !form.beneficiary_account_id}
                  onClick={add}>Propose</button>
        </div>
      </div>

      <div className="card" style={{ padding: 0 }}>
        <div className="table-scroll">
          <table>
            <thead><tr><th>Kind</th><th>Token</th><th>Scope</th><th>Note</th><th>Added</th><th></th></tr></thead>
            <tbody>
              {data.items.map((e) => (
                <tr key={e.id}>
                  <td><span className={`pill ${e.kind === 'ALLOWLIST' ? 'suppress' : 'override'}`}>{e.kind}</span></td>
                  <td className="mono dim">{e.token.slice(0, 22)}…</td>
                  <td className="mono dim">{e.account_token ? `${e.account_token.slice(0, 14)}…` : 'global'}</td>
                  <td className="muted">{e.note}</td>
                  <td className="muted">{when(e.added_at)}</td>
                  <td>
                    <button className="danger" disabled={busy} onClick={() => remove(e)}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!data.items.length && <Empty>No list entries.</Empty>}
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ audit */

const roleLabel = (r) => ({ ANALYST: 'Fraud Analyst', FRAUD_OPS_LEAD: 'Fraud Ops Lead',
  INFOSEC_ANALYST: 'InfoSec Analyst', ADMIN: 'Administrator' }[r] || r)
const roleInitials = (name) => (name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((s) => s[0].toUpperCase()).join('')

function Audit() {
  const [sub, setSub] = useState('Users')
  return (
    <>
      <div className="tabs" style={{ marginBottom: 14 }}>
        {['Users', 'Audit log'].map((t) => (
          <button key={t} className={`tab ${sub === t ? 'active' : ''}`} onClick={() => setSub(t)}>{t}</button>
        ))}
      </div>
      {sub === 'Users' ? <Users /> : <AuditLog />}
    </>
  )
}

const ROLES = ['ANALYST', 'FRAUD_OPS_LEAD', 'INFOSEC_ANALYST', 'ADMIN']

/**
 * Users and access (D99, D104, D105).
 *
 * Every control here *proposes*. Nothing about an account changes until a second
 * administrator approves it under Pending. Creating an account, moving somebody
 * between roles, disabling them, re-issuing or waiving their second factor,
 * resetting their password and correcting the email that identifies them are all
 * changes to who can get into what — and the role *is* the permission set
 * (D12b), so one administrator acting alone could quietly grant themselves the
 * lot.
 *
 * An administrator sees no action on their own row: the server refuses a change
 * proposed against your own access, and offering the button would only teach
 * people to expect it to work.
 */
function Users() {
  const { data, error, reload, setError } = useAsync(() => Promise.all([api.users(), api.me()]))
  const [notice, setNotice] = useState(null)
  const [busy, setBusy] = useState(false)
  const [adding, setAdding] = useState(false)
  const blank = { email: '', display_name: '', role: 'ANALYST', password: '', reason: '' }
  const [form, setForm] = useState(blank)
  // Which row has an expanded editor open, and which editor it is.
  const [panel, setPanel] = useState(null)
  const [edit, setEdit] = useState({ email: '', display_name: '', password: '', reason: '' })

  if (error && !data) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>
  const [users, me] = data

  function askReason(what) {
    const reason = window.prompt(`${what} — why? At least 20 characters; it goes in the audit trail.`)
    if (reason === null) return null
    if (reason.trim().length < 20) {
      setError('A rationale of at least 20 characters is required.')
      return null
    }
    return reason.trim()
  }

  async function propose(label, fn) {
    setBusy(true); setNotice(null); setError(null)
    try {
      await fn()
      setNotice(`${label} proposed. Nothing changes until a different administrator approves it under Pending.`)
      reload()
      return true
    } catch (e) {
      setError(e.message)
      return false
    } finally { setBusy(false) }
  }

  function openPanel(u, mode) {
    setNotice(null); setError(null)
    setPanel({ id: u.id, mode })
    setEdit({ email: u.email, display_name: u.display_name, password: '', reason: '' })
  }

  function changeRole(u, role) {
    if (!role || role === u.role) return
    const reason = askReason(`Move ${u.email} to ${roleLabel(role)}`)
    if (reason) propose(`Role change for ${u.email}`, () => api.changeUserRole(u.id, { role, reason }))
  }

  function toggleActive(u) {
    const verb = u.active ? 'Disable' : 'Restore'
    const reason = askReason(`${verb} ${u.email}`)
    if (reason) propose(`${verb} ${u.email}`, () => api.setUserActive(u.id, { active: !u.active, reason }))
  }

  function resetMfa(u) {
    const reason = askReason(`Re-issue the authenticator for ${u.email}`)
    if (reason) propose(`Authenticator reset for ${u.email}`, () => api.resetUserMfa(u.id, { reason }))
  }

  function toggleMfa(u) {
    const off = u.totp_enabled
    const what = off ? `Stop asking ${u.email} for a code` : `Require a code from ${u.email}`
    const reason = askReason(what)
    if (reason) propose(what, () => api.setUserMfa(u.id, { enabled: !off, reason }))
  }

  async function saveProfile(u, e) {
    e.preventDefault()
    const ok = await propose(`Details for ${u.email}`, () => api.updateUserProfile(u.id, {
      email: edit.email, display_name: edit.display_name, reason: edit.reason,
    }))
    if (ok) setPanel(null)
  }

  async function savePassword(u, e) {
    e.preventDefault()
    const ok = await propose(`Password reset for ${u.email}`, () => api.resetUserPassword(u.id, {
      password: edit.password, reason: edit.reason,
    }))
    if (ok) setPanel(null)
  }

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const setEditField = (k) => (e) => setEdit({ ...edit, [k]: e.target.value })

  async function submitNew(e) {
    e.preventDefault()
    const ok = await propose(`New ${roleLabel(form.role)} ${form.email}`, () => api.createUser(form))
    if (ok) { setAdding(false); setForm(blank) }
  }

  return (
    <>
      {error && <Banner kind="error">{error}</Banner>}
      {notice && <Banner kind="ok">{notice}</Banner>}

      {adding && (
        <form className="card" style={{ marginBottom: 14 }} onSubmit={submitNew}>
          <h2 style={{ fontSize: 14, marginTop: 0 }}>Propose a new account</h2>
          <div className="grid cols-2" style={{ gap: 12 }}>
            <label>Email
              <input type="email" required value={form.email} onChange={set('email')}
                     placeholder="name@riskradar.local" />
            </label>
            <label>Full name
              <input required value={form.display_name} onChange={set('display_name')} />
            </label>
            <label>Role
              <select value={form.role} onChange={set('role')}>
                {ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
              </select>
            </label>
            <label>Initial password
              <input type="password" required minLength={12} value={form.password}
                     onChange={set('password')} placeholder="at least 12 characters" />
            </label>
          </div>
          <label>Reason
            <input required minLength={20} value={form.reason} onChange={set('reason')}
                   placeholder="why this account is needed — at least 20 characters" />
          </label>
          <p className="dim" style={{ fontSize: 12 }}>
            The password is hashed before it is stored, and the authenticator secret is
            minted only when the second administrator approves — so neither ever sits in
            a pending change request waiting to be read (D99). New accounts start with the
            code prompt on.
          </p>
          <div className="row" style={{ gap: 8 }}>
            <button className="primary" type="submit" disabled={busy}>Propose account</button>
            <button type="button" className="ghost"
                    onClick={() => { setAdding(false); setError(null) }}>Cancel</button>
          </div>
        </form>
      )}

      <div className="card" style={{ padding: 0 }}>
        <div className="toolbar">
          <strong style={{ fontSize: 14 }}>Users &amp; access</strong>
          <span className="dim" style={{ fontSize: 12 }}>Roles enforce separation of duties on every request (D12b).</span>
          {!adding && (
            <button className="primary" style={{ marginLeft: 'auto' }}
                    onClick={() => { setAdding(true); setNotice(null); setError(null) }}>
              ＋ Add user
            </button>
          )}
        </div>
        <div className="table-scroll">
          <table className="rowtable">
            <thead>
              <tr><th>User</th><th>Email</th><th>Role</th><th>Status</th><th>Code asked</th>
                  <th>Created</th><th>Access</th></tr>
            </thead>
            <tbody>
              {users.items.map((u) => {
                const self = u.id === me.id
                const open = panel && panel.id === u.id
                return [
                  <tr key={u.id}>
                    <td>
                      <div className="userpair">
                        <span className="avatar sm">{roleInitials(u.display_name)}</span>
                        <div>
                          <div style={{ fontWeight: 560 }}>{u.display_name}</div>
                          <div className="mono dim" style={{ fontSize: 10.5 }}>USR-{u.id}</div>
                        </div>
                      </div>
                    </td>
                    <td className="dim">{u.email}</td>
                    <td>
                      {self ? <span className="pill">{roleLabel(u.role)}</span> : (
                        <select value={u.role} disabled={busy} aria-label={`Role for ${u.email}`}
                                style={{ width: 152 }}
                                onChange={(e) => changeRole(u, e.target.value)}>
                          {ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
                        </select>
                      )}
                    </td>
                    <td>
                      <span className="authdot">
                        <span className={`live-dot ${u.active ? 'ok' : ''}`}
                              style={!u.active ? { background: 'var(--text-3)', boxShadow: 'none' } : {}} />
                        {u.active ? 'Active' : 'Inactive'}
                      </span>
                    </td>
                    <td className={u.totp_enabled ? '' : 'dim'}>
                      {u.totp_enabled ? 'Yes' : 'No — password only'}
                    </td>
                    <td className="dim" style={{ fontSize: 11.5 }}>{when(u.created_at)}</td>
                    <td>
                      {self ? <span className="dim" style={{ fontSize: 11.5 }}>your own account</span> : (
                        <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                          <button className="ghost" disabled={busy}
                                  title="Correct the email and name on this account"
                                  onClick={() => openPanel(u, 'profile')}>Edit</button>
                          <button className="ghost" disabled={busy}
                                  title="Set a new password for this account"
                                  onClick={() => openPanel(u, 'password')}>Password</button>
                          <button className="ghost" disabled={busy}
                                  title={u.totp_enabled
                                    ? 'Stop asking this user for a code at sign-in'
                                    : 'Require a code from this user at sign-in'}
                                  onClick={() => toggleMfa(u)}>
                            {u.totp_enabled ? 'No code' : 'Require code'}
                          </button>
                          <button className="ghost" disabled={busy}
                                  title="Issue a new authenticator secret, keeping the prompt on"
                                  onClick={() => resetMfa(u)}>Reset MFA</button>
                          <button className={u.active ? 'danger' : ''} disabled={busy}
                                  onClick={() => toggleActive(u)}>
                            {u.active ? 'Disable' : 'Restore'}
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>,
                  open && (
                    <tr key={`${u.id}-panel`}>
                      <td colSpan={7} style={{ background: 'var(--accent-dim)' }}>
                        {panel.mode === 'profile' ? (
                          <form className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}
                                onSubmit={(e) => saveProfile(u, e)}>
                            <label style={{ margin: 0 }}>Email
                              <input type="email" required value={edit.email}
                                     onChange={setEditField('email')} style={{ width: 230 }} />
                            </label>
                            <label style={{ margin: 0 }}>Full name
                              <input required value={edit.display_name}
                                     onChange={setEditField('display_name')} style={{ width: 190 }} />
                            </label>
                            <label style={{ margin: 0, flex: 1, minWidth: 240 }}>Reason
                              <input required minLength={20} value={edit.reason}
                                     onChange={setEditField('reason')}
                                     placeholder="at least 20 characters" />
                            </label>
                            <button className="primary" type="submit" disabled={busy}>Propose</button>
                            <button type="button" className="ghost" onClick={() => setPanel(null)}>Cancel</button>
                            <p className="dim" style={{ fontSize: 11.5, width: '100%', margin: 0 }}>
                              The email is the login identifier, so changing it changes who can sign
                              in to this account.
                            </p>
                          </form>
                        ) : (
                          <form className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}
                                onSubmit={(e) => savePassword(u, e)}>
                            <label style={{ margin: 0 }}>New password
                              <input type="password" required minLength={12} value={edit.password}
                                     onChange={setEditField('password')}
                                     placeholder="at least 12 characters" style={{ width: 230 }} />
                            </label>
                            <label style={{ margin: 0, flex: 1, minWidth: 240 }}>Reason
                              <input required minLength={20} value={edit.reason}
                                     onChange={setEditField('reason')}
                                     placeholder="at least 20 characters" />
                            </label>
                            <button className="primary" type="submit" disabled={busy}>Propose</button>
                            <button type="button" className="ghost" onClick={() => setPanel(null)}>Cancel</button>
                            <p className="dim" style={{ fontSize: 11.5, width: '100%', margin: 0 }}>
                              Hashed before it is stored, so the plaintext never reaches the database
                              or the pending request (D99).
                            </p>
                          </form>
                        )}
                      </td>
                    </tr>
                  ),
                ]
              })}
            </tbody>
          </table>
        </div>
        <div className="table-foot dim">
          Showing {users.items.length} user{users.items.length === 1 ? '' : 's'}. Every change here
          is proposed, and takes a second administrator&apos;s approval (D99, D104, D105).
        </div>
      </div>
    </>
  )
}

function AuditLog() {
  const { data, error } = useAsync(() => api.audit({ limit: 200 }))
  const [verification, setVerification] = useState(null)

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>

  return (
    <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="between">
          <div>
            <h2 style={{ marginBottom: 4 }}>Tamper evidence</h2>
            <p className="dim" style={{ fontSize: 12, margin: 0 }}>
              Each row stores the hash of the one before it. The application
              database role holds INSERT and SELECT on this table and nothing
              else — it cannot rewrite what it writes.
            </p>
          </div>
          <button className="primary" onClick={async () => setVerification(await api.verifyAudit())}>
            Verify chain
          </button>
        </div>
        {verification && (
          <Banner kind={verification.ok ? 'ok' : 'error'}>
            {verification.ok
              ? `Chain intact across ${verification.checked} rows.`
              : `Chain broken at row ${verification.broken_at_id}: ${verification.reason}`}
          </Banner>
        )}
      </div>

      <div className="card" style={{ padding: 0 }}>
        <div className="table-scroll">
          <table>
            <thead>
              <tr><th>When</th><th>Actor</th><th>Action</th><th>Object</th>
                  <th>From → to</th><th>Hash</th></tr>
            </thead>
            <tbody>
              {data.items.map((a) => (
                <tr key={a.id}>
                  <td className="mono">{when(a.occurred_at)}</td>
                  <td>{a.actor} <span className="dim">({a.actor_role})</span></td>
                  <td className="mono">{a.action}</td>
                  <td className="muted">{a.object_type} {a.object_id}</td>
                  <td className="dim">
                    {a.from_state || a.to_state
                      ? `${String(a.from_state ?? '—').slice(0, 30)} → ${String(a.to_state ?? '—').slice(0, 30)}`
                      : '—'}
                  </td>
                  <td className="mono dim">{a.hash.slice(0, 12)}…</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
