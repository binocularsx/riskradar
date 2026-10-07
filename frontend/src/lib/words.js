/**
 * Every code the system stores, said the way a fraud analyst would say it.
 *
 * The database and the API keep their codes (VELOCITY_BURST_1H, ALERT_RAISED):
 * they are stable identifiers and the tests, the audit trail and the model
 * depend on them. Nobody on the desk should have to read one. Anything shown on
 * screen goes through `say(MAP, code)`, and a code that has no entry yet still
 * comes out as ordinary words rather than SHOUTED_SNAKE_CASE.
 */

/** "ACCOUNT_TAKEOVER_SEQUENCE" -> "Account takeover sequence". The fallback only. */
export function words(code) {
  if (code == null || code === '') return ''
  const s = String(code).replace(/_/g, ' ').toLowerCase().trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** Look a code up; fall back to plain words so a new code is never shown raw. */
export function say(map, code) {
  if (code == null || code === '') return ''
  return map[code] ?? words(code)
}

/** Same, lower-case first letter, for use mid-sentence. */
export function sayLower(map, code) {
  const s = say(map, code)
  return s.charAt(0).toLowerCase() + s.slice(1)
}

/** What each detection rule spotted. Short enough for a tag. */
export const RULE = {
  VELOCITY_BURST_1H: 'Many payments in one hour',
  ACCOUNT_TAKEOVER_SEQUENCE: 'Signs of account takeover',
  MULE_INBOUND_FANIN: 'Money in from many senders',
  SECOND_LEG_ONWARD_PAYMENT: 'Money received, then sent straight on',
  CARD_TESTING_PROBES: 'Card being tested with small payments',
  SCAM_BENEFICIARY_FANIN: 'New account many customers paid today',
  SIM_SWAP_TRANSFER: 'Payment soon after a SIM change',
  DORMANT_ACCOUNT_REACTIVATION: 'Dormant account suddenly active',
  CARD_PRESENT_NEW_REGION_CASHOUT: 'Card used at many terminals somewhere new',
  SANCTIONED_BENEFICIARY: 'Recipient is on the sanctions list',
  KNOWN_MULE_BENEFICIARY: 'Recipient is a known mule account',
  PRE_REGISTERED_BENEFICIARY: 'Customer has paid this recipient before',
  ESTABLISHED_PAYEE_NORMAL: 'Usual amount to a regular recipient',
  MODEL: 'The risk model',
}

/** What a rule does to the risk level. */
export const RULE_EFFECT = {
  ESCALATE: 'Raises risk',
  SUPPRESS: 'Lowers risk',
  OVERRIDE: 'Sets risk to Critical',
}

/** Where a case is on its way to being finished. */
export const STAGE = {
  NEW: 'Not started',
  OPEN: 'Not started',
  IN_REVIEW: 'Being reviewed',
  UNDER_REVIEW: 'Being reviewed',
  ESCALATED: 'With a specialist',
  AWAITING_APPROVAL: 'Waiting for approval',
  AWAITING_CLOSE: 'Ready to close',
  CLOSED: 'Completed',
}

/** The result of an investigation. */
export const OUTCOME = {
  CONFIRMED_FRAUD: 'Fraud confirmed',
  FALSE_POSITIVE: 'No fraud found',
  INCONCLUSIVE: 'More review needed',
}

/** A suggested result waiting for a second person. */
export const PROPOSAL_STATE = {
  PENDING: 'Waiting for a different lead',
  APPROVED: 'Approved',
  REJECTED: 'Rejected',
  RETURNED: 'Sent back for more work',
}

export const ROLE = {
  ANALYST: 'Fraud analyst',
  FRAUD_OPS_LEAD: 'Fraud operations lead',
  INFOSEC_ANALYST: 'Information security analyst',
  ADMIN: 'Administrator',
  SYSTEM: 'Risk Radar (automatic)',
}

export const CHANNEL = {
  MOBILE_APP: 'Mobile app',
  WEB: 'Internet banking',
  USSD: 'USSD',
  POS: 'Card machine (POS)',
  ATM: 'ATM',
  AGENT: 'Bank agent',
  BRANCH: 'Branch',
  API: 'Partner connection',
  CONTACT_CENTRE: 'Contact centre',
  EMAIL: 'Email',
  SOCIAL_MEDIA: 'Social media',
}

/** What happened when the payment was attempted. */
export const PAYMENT_RESULT = {
  APPROVED: 'Approved',
  DECLINED: 'Declined',
  FAILED: 'Failed',
  REVERSED: 'Reversed',
}

export const DECLINE_REASON = {
  DO_NOT_HONOUR: 'Refused by the card issuer',
  INSUFFICIENT_FUNDS: 'Not enough money',
  INVALID_PIN: 'Wrong PIN',
  LIMIT_EXCEEDED: 'Over the limit',
  TIMEOUT: 'Timed out',
}

/** What the system did with a payment once it had a risk level. */
export const DECISION = {
  ALLOW: 'Let it through',
  MONITOR: 'Let it through and keep watching',
  REVIEW: 'Send to the team for review',
  HOLD: 'Hold for urgent review',
}

/** Things the bank can be asked to do to an account, card or payment. */
export const BANK_ACTION = {
  DEBIT_RESTRICTION: 'Stop money leaving the account',
  CHANNEL_RESTRICTION: 'Block one banking channel',
  CARD_FREEZE: 'Freeze the card',
  CARD_BLOCK_REQUESTED: 'Card block requested',
  BENEFICIARY_RESTRICTION: 'Block payments to this recipient',
  TRANSACTION_REVERSAL: 'Reverse the payment',
  SESSION_TERMINATION: 'Log the customer out everywhere',
  CREDENTIAL_RESET: 'Reset the password and PIN',
  MFA_REENROLMENT: 'Set up two-step login again',
  HOLD_REQUESTED: 'Hold requested',
  RECALL_REQUESTED: 'Recall of funds requested',
}

/** Lists of accounts kept by the bank. */
export const LIST_KIND = {
  KNOWN_MULE: 'Known mule account',
  SANCTIONED: 'Sanctions list',
  ALLOWLIST: 'Trusted (never flag)',
  BLOCKLIST: 'Blocked',
}

/** How a case is being handled. */
export const HANDLING = {
  HUMAN: 'Handled by the team',
  MACHINE: 'Handled automatically',
  NONE: 'No action',
  HUMAN_REVIEW: 'Sent to the team',
  MACHINE_ACTION: 'Handled automatically',
  AUTO_CLOSE: 'Closed automatically',
}

/** Delivery of an instruction to the bank's systems. */
export const DELIVERY = {
  PENDING: 'Waiting to send',
  SENT: 'Sent',
  FAILED: 'Could not send',
  NOT_SHAREABLE: 'Not shared (not allowed)',
  RECOMMENDED: 'Approved, waiting to send to the bank',
  DELIVERED: 'Sent, waiting for the bank to reply',
  APPLIED: 'The bank applied it',
  NOT_APPLIED: 'The bank did not apply it',
  REJECTED: 'The bank refused it',
}

/** Why an alert was held back instead of raised straight away. */
export const HOLD_REASON = {
  DAILY_CAP: "the team's daily review limit was reached",
  HOURLY_PACE: 'too many alerts arrived in the same hour',
  RULE_QUOTA: "this rule's daily share was used up",
  MODEL_QUOTA: "the risk model's daily share was used up",
  RULE_CAP: "this rule's daily limit was reached",
}

/** System problems the desk needs to know about. */
export const ALARM = {
  ALERTS_EXPIRED_UNREVIEWED: 'Some alerts expired before anyone reviewed them',
  MODEL_UNAVAILABLE: 'The risk model is down; only the written rules are running',
  ALERT_BUDGET_OVERRUN: 'More alerts today than the team can review',
  RULE_ONLY_MODE: 'The risk model is down; only the written rules are running',
  SCORING_BACKLOG: 'Payments are waiting longer than usual to be checked',
  WORKERS_DOWN: 'The payment-checking service is not running',
  DEAD_LETTER: 'Some payments could not be checked',
}

/** Every entry in the activity log, as a sentence fragment. */
export const EVENT = {
  ALARM: 'System alarm',
  ALERT_BUDGET_CHANGED: 'Daily review limit changed',
  ALERT_DEFERRAL_EXPIRED: 'Held-back alert expired',
  ALERT_DEFERRAL_RELEASED: 'Held-back alert released to the team',
  ALERT_RAISED: 'Alert raised',
  API_KEY_CREATED: 'Connection key created',
  API_KEY_REVOKED: 'Connection key withdrawn',
  BENEFICIARY_RESTRICTION: 'Payments to recipient blocked',
  CARD_BLOCK_REQUESTED: 'Card block requested',
  CARD_FREEZE: 'Card frozen',
  CARD_LOCATION_CONFIRMED: 'Card location confirmed with customer',
  CASE_ACTION_RECORDED: 'Investigation step recorded',
  CASE_ASSIGNED_AUTOMATICALLY: 'Case assigned automatically',
  CASE_ASSIGNED_FROM_ESCALATION: 'Case assigned to a specialist',
  CASE_CLOSED: 'Case completed',
  CASE_ESCALATED: 'Case sent to a specialist',
  CASE_NOTE_ADDED: 'Note added',
  CASE_OUTCOME_SET: 'Result recorded',
  CASE_REASSIGNED: 'Case reassigned',
  CASE_RETURNED: 'Case sent back',
  CASE_REVIEW_STARTED: 'Review started',
  CASE_UNASSIGNED_EXCEPTION: 'Case left unassigned',
  CASE_VIEWED: 'Case opened',
  CLOCK_BREACHED: 'Regulatory deadline missed',
  CLOCK_COUNTERPARTY_NOTIFIED: 'Receiving bank notified',
  CONFIG_CHANGE_APPROVED: 'Setting change approved',
  CONFIG_CHANGE_PROPOSED: 'Setting change proposed',
  CONFIG_CHANGE_REJECTED: 'Setting change rejected',
  CONTACT_DETAILS_CHECKED: 'Contact details checked',
  CUSTOMER_CONTACTED: 'Customer contacted',
  CUSTOMER_REPORT_RECORDED: 'Customer complaint recorded',
  DEBIT_RESTRICTION: 'Account debits stopped',
  DESTINATIONS_CHECKED: 'Recipient accounts checked',
  DISPOSITION_MACHINE_ACTION: 'Handled automatically',
  ENFORCEMENT_POLICY_PUBLISHED: 'Automatic-action policy updated',
  ESCALATE: 'Sent to a specialist',
  FRAUD_APPROVED: 'Fraud result approved',
  FRAUD_RETURNED: 'Fraud result sent back',
  FRAUD_SUBMITTED: 'Result suggested for approval',
  HOLD_REQUESTED: 'Hold requested',
  IDENTITY_VERIFIED: "Customer's identity checked",
  LOGIN: 'Signed in',
  LOGOUT: 'Signed out',
  OUTCOME: 'Result recorded',
  RECALL_REQUESTED: 'Recall of funds requested',
  RECEIVING_BANK_NOTIFIED: 'Receiving bank notified',
  RESTRICTION_ACKNOWLEDGED: 'Bank confirmed the restriction',
  RESTRICTION_DELIVERY_PAUSED: 'Sending restrictions to the bank paused',
  RESTRICTION_DELIVERY_RESUMED: 'Sending restrictions to the bank resumed',
  RESTRICTION_ISSUED: 'Restriction sent to the bank',
  RESTRICTION_RELEASED: 'Restriction lifted',
  THRESHOLDS_CHANGED: 'Risk levels adjusted',
  TIMELINE_REVIEWED: 'Account history reviewed',
  TRANSACTION_REVERSAL: 'Payment reversal requested',
  USER_MFA_DISABLED: 'Two-step login turned off for a user',
  WATCHLIST_CUSTOMER_CONTACTED: 'Watch-listed customer contacted',
  WATCHLIST_FLAG_EXPIRED: '24-hour watch ended',
  WATCHLIST_FLAG_LIFTED: '24-hour watch lifted',
  WATCHLIST_FLAG_PLACED: '24-hour watch placed',
}

/**
 * The measurements the risk model uses. Shown when someone opens
 * "what moved the score".
 */
export const MEASURE = {
  amount_log10: 'Size of the payment',
  amount_ratio_to_account_p95_30d: 'Payment compared with this account\'s usual largest',
  txn_count_1h_account: 'Payments in the last hour',
  approved_value_ratio_24h_vs_daily_mean_30d: 'Money sent today compared with a normal day',
  failed_attempts_1h_account: 'Failed attempts in the last hour',
  decline_rate_24h_account: 'Share of payments declined today',
  distinct_beneficiaries_1h_account: 'Different recipients in the last hour',
  beneficiary_is_new_to_account: 'Recipient is new to this customer',
  beneficiary_first_seen_days: 'How long the bank has known the recipient',
  device_is_new_to_subject: 'Device is new to this customer',
  account_age_days: 'How old the account is',
  days_since_account_activity: 'Days since the account was last used',
  failed_logins_1h_subject: 'Failed logins in the last hour',
  device_bound_hours: 'Hours since a new device was linked',
  credential_changed_hours: 'Hours since the password, PIN or login method changed',
  sim_changed_hours: 'Hours since the SIM was changed',
  payee_added_minutes: 'Minutes since the recipient was added',
  credits_24h_account: 'Money received today',
  distinct_remitters_24h_account: 'Different senders today',
  inbound_count_ratio_24h_vs_daily_mean_30d: 'Payments received today compared with a normal day',
  minutes_since_last_credit: 'Minutes since money last arrived',
  pass_through_ratio_24h: 'Share of money received that was sent straight on',
  region_is_new_to_subject: 'Region is new to this customer',
  card_present_count_1h_account: 'Card-machine uses in the last hour',
  beneficiary_distinct_senders_24h: 'Other customers who paid this recipient today',
  hour_of_day_local: 'Time of day',
}

const nairaFromLog = (v) => `₦${Number((10 ** Number(v)).toPrecision(3)).toLocaleString('en-NG')}`
const pct = (v) => `${Math.round(Number(v) * 100)}%`

/** A rule's settings, each as a short phrase an administrator can check. */
export const PARAM = {
  within_hours: (v) => `within ${v} hours`,
  min_precursors: (v) => `at least ${v} warning signs (new device, SIM change, password or PIN change, failed logins)`,
  min_failed_logins: (v) => `${v} or more failed logins count as a sign`,
  min_count_1h: (v) => `${v} or more card-machine uses in an hour`,
  min_failed_1h: (v) => `${v} or more failed attempts in an hour`,
  min_decline_rate_24h: (v) => `${pct(v)} or more of today's payments declined`,
  min_amount_log10: (v) => `payments of ${nairaFromLog(v)} or more`,
  min_dormant_days: (v) => `account unused for ${v} days or more`,
  min_remitters: (v) => `money from ${v} or more different senders`,
  min_count_ratio: (v) => `${v} times more incoming payments than on a normal day`,
  min_amount_ratio: (v) => `at least ${v} times the customer's usual largest payment`,
  max_amount_ratio: (v) => `no more than ${v} times the customer's usual largest payment`,
  min_other_senders: (v) => `${v} or more other customers paid the same account`,
  max_beneficiary_age_days: (v) => `recipient account ${v} days old or newer`,
  min_beneficiary_age_days: (v) => `recipient known for ${v} days or more`,
  min_pass_through: (v) => `${pct(v)} or more of the money received is sent straight on`,
  max_minutes_since_credit: (v) => `sent on within ${v} minutes of arriving`,
  channels: (v) => `on ${[].concat(v).map((c) => say(CHANNEL, c)).join(' or ')}`,
  min_count: (v) => `${v} or more payments in an hour`,
  new_destination_days: (v) => `to a recipient the bank first saw ${v} day(s) ago or less`,
  no_destination_min_count: (v) => `or ${v} or more payments with no recipient`,
}

/** "min_count: 5" -> "5 or more payments in an hour"; unknown settings still read as words. */
export function sayParam(key, value) {
  const f = PARAM[key]
  if (f) return f(value)
  return `${words(key).toLowerCase()}: ${Array.isArray(value) ? value.map(words).join(', ') : value}`
}
