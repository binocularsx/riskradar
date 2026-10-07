/**
 * API client.
 *
 * `credentials: 'include'` on every call because authentication is a server-side
 * session in an httpOnly cookie (D12), not a token this code could read. There is
 * deliberately no place in this file where a credential is stored — an XSS on
 * this page cannot lift a session, and logging out actually ends it.
 */

const JSON_HEADERS = { 'Content-Type': 'application/json' }

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/**
 * The CSRF token (D95). The server mirrors it into a readable `rr_csrf` cookie
 * at login; the double-submit defence is echoing it back in a header on every
 * unsafe request. A cross-origin page can set neither this cookie nor this
 * header, which is exactly what makes the pair unforgeable.
 */
function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)rr_csrf=([^;]+)/)
  return match ? decodeURIComponent(match[1]) : null
}

async function request(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const headers = { ...(options.headers || {}) }
  if (UNSAFE_METHODS.has(method)) {
    const token = csrfToken()
    if (token) headers['X-CSRF-Token'] = token
  }

  const response = await fetch(path, {
    credentials: 'include',
    signal: AbortSignal.timeout(20000),
    ...options,
    headers,
  })

  if (response.status === 401) {
    if (path !== '/v1/auth/login') window.dispatchEvent(new Event('riskradar:auth-required'))
    const error = new Error('not authenticated')
    error.unauthenticated = true
    throw error
  }

  const text = await response.text()
  let body = null
  try { body = text ? JSON.parse(text) : null } catch {
    throw new Error(`The service returned an unreadable response (HTTP ${response.status}). Try again.`)
  }

  if (!response.ok) {
    const detail = body?.detail
    const message = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map((d) => d.msg).filter(Boolean).join('; ')
        : detail?.message || `Request failed (HTTP ${response.status}).`
    const error = new Error(message)
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
  // D93: an analyst proposes; a different lead decides.
  submitFraud: (id, data) => post(`/v1/cases/${id}/submissions`, data),
  // D106: what to block, derived from the case's own alerted transactions, so
  // confirming fraud proposes the restrictions instead of leaving them empty.
  restrictionSuggestions: (id) => get(`/v1/cases/${id}/restriction-suggestions`),
  caseReports: (id) => get(`/v1/cases/${id}/account-manager-reports`),
  caseSubmissions: (id) => get(`/v1/cases/${id}/submissions`),
  approvals: () => get('/v1/approvals'),
  decideSubmission: (id, data) => post(`/v1/submissions/${id}/decision`, data),
  submissionCatalog: () => get('/v1/submissions/catalog'),
  operations: () => get('/v1/metrics/operations'),
  systemStatus: () => get('/v1/system/status'),
  restrictionReconciliation: () => get('/v1/metrics/restrictions/reconciliation'),
  // D83: the live desk, the case tracker and the workflow of one case
  intake: (minutes = 60) => get(`/v1/metrics/intake?minutes=${minutes}`),
  pipeline: () => get('/v1/workflow/pipeline'),
  workflowCatalog: () => get('/v1/workflow/catalog'),
  caseWorkflow: (id) => get(`/v1/cases/${id}/workflow`),
  recordAction: (id, data) => post(`/v1/cases/${id}/actions`, data),
  returnCase: (id, findings) => post(`/v1/cases/${id}/return`, { findings }),
  caseLinks: (id, days = 30) => get(`/v1/cases/${id}/links?days=${days}`),
  // D97: restrictions a lead approved on a case, and how their delivery is going.
  caseRestrictions: (id) => get(`/v1/cases/${id}/restrictions`),
  restrictionStatus: () => get('/v1/metrics/restrictions'),

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
  // D109: activity by session region, for the map on Analytics.
  geography: (hours = 168) => get(`/v1/metrics/geography?hours=${hours}`),
  budgetMenu: () => get('/v1/metrics/budget-menu'),
  searchTransactions: (params) => get(`/v1/transactions/search?${new URLSearchParams(params)}`),

  // administration
  rules: () => get('/v1/admin/rules'),
  // D96: a rule or threshold change is now a *proposal*; a different admin approves it.
  updateRule: (code, data) => patch(`/v1/admin/rules/${code}`, data),
  thresholds: () => get('/v1/admin/thresholds'),
  createThresholds: (data) => post('/v1/admin/thresholds', data),
  configChanges: (state) => get(`/v1/admin/change-requests${state ? `?state=${state}` : ''}`),
  decideConfigChange: (id, data) => post(`/v1/admin/change-requests/${id}/decision`, data),
  models: () => get('/v1/admin/models'),
  // D98: model promotion and list changes are now maker-checker (propose → approve).
  promoteModel: (id, reason) => post('/v1/admin/models/promote', { model_version_id: id, reason }),
  lists: (kind) => get(`/v1/admin/lists${kind ? `?kind=${kind}` : ''}`),
  addListEntry: (data) => post('/v1/admin/lists', data),
  removeListEntry: (id, reason) => del(`/v1/admin/lists/${id}?reason=${encodeURIComponent(reason)}`),
  enforcementPolicy: () => get('/v1/admin/enforcement-policy'),
  directiveMetrics: (days = 7) => get(`/v1/metrics/directives?days=${days}`),
  audit: (params) => get(`/v1/admin/audit?${new URLSearchParams(params || {})}`),
  verifyAudit: () => get('/v1/admin/audit/verify'),
  users: () => get('/v1/admin/users'),
  // D99/D104: every one of these *proposes*. A second administrator approves it
  // in Pending before anything about the account actually changes.
  createUser: (data) => post('/v1/admin/users', data),
  changeUserRole: (id, data) => post(`/v1/admin/users/${id}/role`, data),
  setUserActive: (id, data) => post(`/v1/admin/users/${id}/active`, data),
  resetUserMfa: (id, data) => post(`/v1/admin/users/${id}/mfa-reset`, data),
  // D105: whether a code is asked for at all, a new password, and the email and
  // name on the account. All three are access changes, so all three propose.
  setUserMfa: (id, data) => post(`/v1/admin/users/${id}/mfa`, data),
  resetUserPassword: (id, data) => post(`/v1/admin/users/${id}/password`, data),
  updateUserProfile: (id, data) => post(`/v1/admin/users/${id}/profile`, data),
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
