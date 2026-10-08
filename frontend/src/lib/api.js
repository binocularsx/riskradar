/**
 * API client.
 *
 * `credentials: 'include'` on every call because authentication is a server-side
 * session in an httpOnly cookie (D12), not a token this code could read. There is
 * deliberately no place in this file where a credential is stored — an XSS on
 * this page cannot lift a session, and logging out actually ends it.
 *
 * D112: the cookie can hold several sessions, one per tab, so each tab has to
 * say which one is its own. That name lives in `sessionStorage`, which is
 * per-tab and is not shared with another tab or window — which is what makes a
 * new tab start signed out, and what lets this tab sign out without disturbing
 * the others. It is a selector and not a credential: on its own it authenticates
 * nothing, because the session identifier is still the httpOnly cookie.
 */

const TAB_KEY = 'rr_tab'
const CSRF_KEY = 'rr_csrf_token'

// sessionStorage throws in some privacy modes; a tab that cannot keep its key
// simply behaves like a signed-out one rather than breaking.
function remembered(key) {
  try { return window.sessionStorage.getItem(key) } catch { return null }
}
function remember(key, value) {
  try {
    if (value === null) window.sessionStorage.removeItem(key)
    else window.sessionStorage.setItem(key, value)
  } catch { /* nothing to do: the tab just will not persist across a reload */ }
}

export function tabKey() {
  return remembered(TAB_KEY)
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/**
 * The CSRF token (D95). The server binds one to each session and checks the
 * header against the session's own copy, so the token is kept per tab beside
 * the tab key (D112) — one readable cookie cannot carry a different token for
 * each of several sessions. The cookie is still read as a fallback for a tab
 * that was signed in before this change. A cross-origin page can neither read
 * this value nor set the header, which is what makes it unforgeable.
 */
function csrfToken() {
  const kept = remembered(CSRF_KEY)
  if (kept) return kept
  const match = document.cookie.match(/(?:^|;\s*)rr_csrf=([^;]+)/)
  return match ? decodeURIComponent(match[1]) : null
}

async function request(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const headers = { ...(options.headers || {}) }
  // D112: every request says which of the browser's sessions this tab is using.
  const tab = remembered(TAB_KEY)
  if (tab) headers['X-Session-Key'] = tab
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
  login: async (email, password, totp_code) => {
    const body = await post('/v1/auth/login', { email, password, totp_code: totp_code || null })
    // D112: keep this tab's session key and CSRF token for this tab only.
    if (body?.tab_key) remember(TAB_KEY, body.tab_key)
    if (body?.csrf_token) remember(CSRF_KEY, body.csrf_token)
    return body
  },
  logout: async () => {
    try {
      return await post('/v1/auth/logout')
    } finally {
      // Forget this tab's session whatever the server said: a failed logout
      // must not leave the tab believing it is still signed in.
      remember(TAB_KEY, null)
      remember(CSRF_KEY, null)
    }
  },
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
  caseReports: (id) => get(`/v1/cases/${id}/support-reports`),
  headsUp: (id, message) => post(`/v1/cases/${id}/heads-up`, { message }),
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
  recordMilestone: (id, data) => post(`/v1/cases/${id}/milestones`, data),
  clockPolicy: () => get('/v1/clocks/policy'),
  // the 24-hour watch-list flag (WP-06)
  placeFlag: (caseId, data) => post(`/v1/cases/${caseId}/watchlist`, data),
  liftFlag: (flagId, data) => post(`/v1/watchlist/${flagId}/lift`, data ?? {}),
  watchlist: (active = true) => get(`/v1/watchlist?active=${active}`),
  integrations: () => get('/v1/admin/integrations'),
  // D87, D109g: machine keys, each for the bank's systems or for the support team.
  apiKeys: () => get('/v1/admin/api-keys'),
  createApiKey: (name, scope) => post('/v1/admin/api-keys', { name, scope }),
  revokeApiKey: (id) => del(`/v1/admin/api-keys/${id}`),
  industryWatchlist: (active = true) => get(`/v1/industry-watchlist?active=${active}`),
  alerts: (params) => get(`/v1/alerts?${new URLSearchParams(params)}`),

  // aggregates
  overview: (hours) => get(`/v1/metrics/overview?hours=${hours}`),
  detection: (days = 30) => get(`/v1/metrics/detection?days=${days}`),
  // D109: activity by session region, for the map on Analytics.
  geography: (hours = 168) => get(`/v1/metrics/geography?hours=${hours}`),
  budgetMenu: () => get('/v1/metrics/budget-menu'),
  searchTransactions: (params) => get(`/v1/transactions/search?${new URLSearchParams(params)}`),
  paymentCheck: (ref) => get(`/v1/transactions/${encodeURIComponent(ref)}/check`),
  completedCases: (params) => get(`/v1/completed-cases?${new URLSearchParams(params)}`),
  decisionRecord: (id) => get(`/v1/completed-cases/${id}/record`),

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
