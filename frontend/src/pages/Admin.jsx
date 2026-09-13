import { useCallback, useEffect, useState } from 'react'

import { api, when } from '../lib/api'
import { Banner, Empty } from '../components/ui'

const TABS = ['Rules', 'Thresholds', 'Models', 'Lists', 'Audit']

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
  const [tab, setTab] = useState('Rules')
  return (
    <>
      <div className="tabs">
        {TABS.map((t) => (
          <button key={t} className={`tab ${tab === t ? 'active' : ''}`} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </div>
      {tab === 'Rules' && <Rules />}
      {tab === 'Thresholds' && <Thresholds />}
      {tab === 'Models' && <Models />}
      {tab === 'Lists' && <Lists />}
      {tab === 'Audit' && <Audit />}
    </>
  )
}

function useAsync(fn, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const load = useCallback(() => {
    fn().then(setData).catch((e) => setError(e.message))
  }, deps) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { load() }, [load])
  return { data, error, reload: load, setError }
}

/* ------------------------------------------------------------------ rules */

function Rules() {
  const { data, error, reload, setError } = useAsync(() => api.rules())
  const [busy, setBusy] = useState(false)
  // D69e (base PRD FR-506): a change can carry its reason. Given, it becomes the
  // rule's rationale and restarts its 90-day review clock.
  const [reasons, setReasons] = useState({})
  const reasonFor = (code) => (reasons[code] || '').trim() || undefined

  async function change(rule, patch) {
    setBusy(true)
    try {
      await api.updateRule(rule.code, { ...patch, rationale: reasonFor(rule.code) })
      setReasons((r) => ({ ...r, [rule.code]: '' }))
      reload()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  const toggle = (rule) => change(rule, { enabled: !rule.enabled })
  const retune = (rule, params) => change(rule, { params })

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>

  return (
    <>
      <Banner kind="info">
        Active ruleset <strong>v{data.ruleset.version}</strong>. Changing anything
        here publishes a new version; decisions keep pointing at the version that
        actually produced them.
      </Banner>

      {['ESCALATE', 'OVERRIDE', 'SUPPRESS'].map((power) => (
        <div className="card" key={power} style={{ marginBottom: 14 }}>
          <h2>
            {power === 'ESCALATE' && 'Escalate — raise the band'}
            {power === 'OVERRIDE' && 'Override — deterministic veto, the model gets no vote'}
            {power === 'SUPPRESS' && 'Suppress — the primary false-positive control'}
          </h2>
          {data.rules.filter((r) => r.power === power).map((rule) => (
            <div key={rule.code} style={{ padding: '10px 0', borderBottom: '1px solid var(--bg-inset)' }}>
              <div className="between">
                <div>
                  <span className="mono">{rule.code}</span>{' '}
                  <span className={`pill ${power.toLowerCase()}`}>{rule.severity}</span>
                </div>
                <button disabled={busy} onClick={() => toggle(rule)}>
                  {rule.enabled ? 'Disable' : 'Enable'}
                </button>
              </div>
              {Object.keys(rule.params || {}).length > 0 && (
                <div className="row wrap" style={{ marginTop: 8 }}>
                  {Object.entries(rule.params).map(([key, value]) => (
                    <div key={key} style={{ width: 210 }}>
                      <label>{key}</label>
                      <input
                        type="number"
                        step="any"
                        defaultValue={value}
                        onBlur={(e) => {
                          const next = { ...rule.params, [key]: Number(e.target.value) }
                          if (Number(e.target.value) !== Number(value)) retune(rule, next)
                        }}
                      />
                    </div>
                  ))}
                </div>
              )}
              {!rule.enabled && <p className="dim" style={{ fontSize: 12 }}>Disabled — this rule emits no signal.</p>}
              <RuleGovernance rule={rule} />
              <input
                style={{ marginTop: 8 }}
                placeholder="Reason for your next change (recorded, and restarts the review clock)"
                value={reasons[rule.code] || ''}
                onChange={(e) => setReasons((r) => ({ ...r, [rule.code]: e.target.value }))}
              />
            </div>
          ))}
        </div>
      ))}
    </>
  )
}

/**
 * Who owns the rule, why it exists, and whether it needs attention (D69e,
 * base PRD FR-504 and FR-507). Every warning is written out in words.
 */
function RuleGovernance({ rule }) {
  const warnings = [
    rule.orphaned && 'No owner',
    rule.review_overdue && 'Review overdue',
    rule.dormant && 'Has not fired in 30 days',
  ].filter(Boolean)
  return (
    <div style={{ marginTop: 8, fontSize: 12.5 }}>
      <div className="row wrap" style={{ gap: 8 }}>
        <span className="muted">Owner <strong style={{ color: 'var(--text)' }}>{rule.owner || '—'}</strong></span>
        <span className="dim">·</span>
        <span className="muted">Approved {rule.approved_at ? when(rule.approved_at) : '—'} by {rule.approved_by || '—'}</span>
        <span className="dim">·</span>
        <span className="muted">Next review {rule.next_review_at ? when(rule.next_review_at) : '—'}</span>
        <span className="dim">·</span>
        <span className="muted">Fired {rule.fired_30d} times in 30 days</span>
        {warnings.map((w) => <span key={w} className="risk risk-HIGH">{w}</span>)}
      </div>
      {rule.rationale && <div className="dim" style={{ marginTop: 4 }}>{rule.rationale}</div>}
    </div>
  )
}

/* ------------------------------------------------------------- thresholds */

function Thresholds() {
  const { data, error, reload, setError } = useAsync(() => api.thresholds())
  const [form, setForm] = useState(null)
  const [busy, setBusy] = useState(false)

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
    setBusy(true)
    try {
      await api.createThresholds(form)
      reload()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  return (
    <>
      <Banner kind="info">
        Thresholds are derived <strong>backwards from the alert budget</strong> —
        analysts multiplied by reviewable alerts per day — then checked against
        recall. Never from "80 sounds high". <code>scripts/derive_thresholds.py</code>{' '}
        computes them from a scored sample.
      </Banner>

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

/* ----------------------------------------------------------------- models */

function Models() {
  const { data, error, reload, setError } = useAsync(() => api.models())
  const [busy, setBusy] = useState(false)

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>

  return (
    <>
      <Banner kind="info">
        Promotion repoints a pointer and is audited with the comparison attached.
        Rollback is repointing it back — not a redeploy.
      </Banner>
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
                <button disabled={busy} onClick={async () => {
                  setBusy(true)
                  try { await api.promoteModel(m.id); reload() }
                  catch (e) { setError(e.message) } finally { setBusy(false) }
                }}>Promote</button>
              )}
            </div>
            {held && (
              <p style={{ fontSize: 13, marginBottom: 0, marginTop: 10 }}>
                Held-out typology <strong>{m.metrics.held_out_typology}</strong>:{' '}
                incident recall <strong>{held.incident_recall}</strong>{' '}
                ({held.incidents_caught}/{held.incidents} incidents) at the alert budget.{' '}
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

  if (error) return <Banner kind="error">{error}</Banner>
  if (!data) return <p className="muted">Loading…</p>

  return (
    <>
      <Banner kind="info">
        Entries are supplied as account numbers and stored as one-way HMAC tokens.
        Risk Radar can check membership; it can never resolve an entry back to an
        account.
      </Banner>

      <div className="card" style={{ marginBottom: 14 }}>
        <h2>Add an entry</h2>
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
          <input style={{ flex: 1, minWidth: 160 }} placeholder="Note"
                 value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
          <button className="primary" disabled={busy || !form.beneficiary_account_id}
                  onClick={async () => {
                    setBusy(true)
                    try {
                      await api.addListEntry({
                        kind: form.kind,
                        beneficiary_account_id: form.beneficiary_account_id,
                        account_id: form.kind === 'ALLOWLIST' ? form.account_id : null,
                        note: form.note || null,
                      })
                      setForm({ ...form, beneficiary_account_id: '', account_id: '', note: '' })
                      reload()
                    } catch (e) { setError(e.message) } finally { setBusy(false) }
                  }}>Add</button>
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
                    <button className="danger" onClick={async () => {
                      await api.removeListEntry(e.id); reload()
                    }}>Remove</button>
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

function Audit() {
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
