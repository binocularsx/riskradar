/**
 * API client.
 *
 * `credentials: 'include'` on every call because authentication is a server-side
 * session in an httpOnly cookie (D12), not a token this code could read. There is
 * deliberately no place in this file where a credential is stored — an XSS on
 * this page cannot lift a session, and logging out actually ends it.
 */

const JSON_HEADERS = { 'Content-Type': 'application/json' }

async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'include',
    ...options,
  })

  if (response.status === 401) {
    const error = new Error('not authenticated')
    error.unauthenticated = true
    throw error
  }

  const text = await response.text()
  const body = text ? JSON.parse(text) : null

  if (!response.ok) {
    const error = new Error(
      (body && (body.detail?.[0]?.msg || body.detail)) || `HTTP ${response.status}`
    )
    error.status = response.status
    error.body = body
    throw error
  }
  return body
}

const get = (path) => request(path)
const post = (path, data) =>
  request(path, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(data ?? {}) })
const patch = (path, data) =>
  request(path, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(data ?? {}) })
const del = (path) => request(path, { method: 'DELETE' })

export const api = {
  // auth
  login: (email, password, totp_code) =>
    post('/v1/auth/login', { email, password, totp_code: totp_code || null }),
  logout: () => post('/v1/auth/logout'),
  me: () => get('/v1/auth/me'),

  // the worklist — what to work next, with exposure, clock and recommendation
  worklist: (params) => get(`/v1/worklist?${new URLSearchParams(params)}`),
  nextCase: () => post('/v1/worklist/next'),
  disposition: (id, data) => post(`/v1/cases/${id}/disposition`, data),
  operations: () => get('/v1/metrics/operations'),

  // queue and cases
  cases: (params) => get(`/v1/cases?${new URLSearchParams(params)}`),
  caseDetail: (id) => get(`/v1/cases/${id}`),
  startReview: (id) => post(`/v1/cases/${id}/review`),
  addNote: (id, body) => post(`/v1/cases/${id}/notes`, { body }),
  setOutcome: (id, outcome, note) => post(`/v1/cases/${id}/outcome`, { outcome, note: note || null }),
  escalate: (id, target, note) => post(`/v1/cases/${id}/escalate`, { target, note: note || null }),
  closeCase: (id) => post(`/v1/cases/${id}/close`),
  // regulatory clocks (WP-05)
  recordReport: (id, data) => post(`/v1/cases/${id}/report`, data),
  recordMilestone: (id, data) => post(`/v1/cases/${id}/milestones`, data),
  clockPolicy: () => get('/v1/clocks/policy'),
  // the 24-hour watch-list flag (WP-06)
  placeFlag: (caseId, data) => post(`/v1/cases/${caseId}/watchlist`, data),
  flagContact: (flagId, data) => post(`/v1/watchlist/${flagId}/contact`, data),
  liftFlag: (flagId, data) => post(`/v1/watchlist/${flagId}/lift`, data ?? {}),
  watchlist: (active = true) => get(`/v1/watchlist?active=${active}`),
  integrations: () => get('/v1/admin/integrations'),
  industryWatchlist: (active = true) => get(`/v1/industry-watchlist?active=${active}`),
  alerts: (params) => get(`/v1/alerts?${new URLSearchParams(params)}`),

  // aggregates
  overview: (hours) => get(`/v1/metrics/overview?hours=${hours}`),
  detection: (days = 30) => get(`/v1/metrics/detection?days=${days}`),
  budgetMenu: () => get('/v1/metrics/budget-menu'),
  searchTransactions: (params) => get(`/v1/transactions/search?${new URLSearchParams(params)}`),

  // administration
  rules: () => get('/v1/admin/rules'),
  updateRule: (code, data) => patch(`/v1/admin/rules/${code}`, data),
  thresholds: () => get('/v1/admin/thresholds'),
  createThresholds: (data) => post('/v1/admin/thresholds', data),
  models: () => get('/v1/admin/models'),
  promoteModel: (id) => post('/v1/admin/models/promote', { model_version_id: id }),
  lists: (kind) => get(`/v1/admin/lists${kind ? `?kind=${kind}` : ''}`),
  addListEntry: (data) => post('/v1/admin/lists', data),
  removeListEntry: (id) => del(`/v1/admin/lists/${id}`),
  enforcementPolicy: () => get('/v1/admin/enforcement-policy'),
  directiveMetrics: (days = 7) => get(`/v1/metrics/directives?days=${days}`),
  audit: (params) => get(`/v1/admin/audit?${new URLSearchParams(params || {})}`),
  verifyAudit: () => get('/v1/admin/audit/verify'),
}

/** Kobo to a readable naira string. Integers in, formatting out (D9a). */
export function naira(minor) {
  if (minor === null || minor === undefined) return '—'
  return new Intl.NumberFormat('en-NG', {
    style: 'currency',
    currency: 'NGN',
    maximumFractionDigits: 2,
  }).format(Number(minor) / 100)
}

export function when(value) {
  if (!value) return '—'
  return new Date(value).toLocaleString('en-GB', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

/** Compact naira for dense tables: NGN 4.2m rather than NGN 4,231,900.00 */
export function nairaShort(minor) {
  if (minor === null || minor === undefined) return '—'
  const n = Number(minor) / 100
  if (n >= 1e9) return `₦${(n / 1e9).toFixed(1)}b`
  if (n >= 1e6) return `₦${(n / 1e6).toFixed(1)}m`
  if (n >= 1e3) return `₦${(n / 1e3).toFixed(0)}k`
  return `₦${n.toFixed(0)}`
}

/** "18m left" / "42m over" — an SLA clock reads better as a duration. */
export function clock(minutes) {
  const m = Math.abs(Math.round(minutes))
  const text = m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`
  return minutes < 0 ? `${text} over` : `${text} left`
}

export function ago(value) {
  if (!value) return '—'
  const seconds = (Date.now() - new Date(value).getTime()) / 1000
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}
