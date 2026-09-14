-- WP-05 / D71: the regulator's clocks (base PRD REG-NG-05, REG-NG-06, FR-407).
--
-- Three things a Nigerian bank needs that the per-severity response clocks in
-- cases/triage.py cannot give it:
--
--   1. Deadlines as versioned configuration. The values below are the CBN
--      exposure draft of 26 November 2025; the final circular may change them,
--      and a change must be a new version, not an edit to code or to history.
--   2. A working-day calendar. Four of the clocks count working days, and the
--      Federal Government declares the holidays (Islamic ones only days ahead).
--   3. The moments that start and stop the clocks, recorded on the case:
--      above all first_reported_at, which starts the refund clock.
--
-- A breach is recorded once in clock_breaches, written to the hash-chained
-- record and escalated to Fraud Ops by clocks/sweep.py.

CREATE TABLE clock_policies (
    id                  bigserial PRIMARY KEY,
    version             int         NOT NULL UNIQUE,
    definitions         jsonb       NOT NULL,
    source              text        NOT NULL,
    notes               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          bigint REFERENCES users(id),
    is_active           boolean     NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX clock_policies_single_active ON clock_policies((is_active)) WHERE is_active;

CREATE TABLE public_holidays (
    holiday_date        date        PRIMARY KEY,
    name                text        NOT NULL,
    -- false: an estimate (Islamic holidays before the Federal Government
    -- declares them). Clocks that span one say so.
    confirmed           boolean     NOT NULL DEFAULT true,
    source              text        NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE cases
    ADD COLUMN first_reported_at          timestamptz,
    ADD COLUMN report_channel             text,
    ADD COLUMN counterparty_institution   text,
    ADD COLUMN acknowledged_at            timestamptz,
    ADD COLUMN counterparty_notified_at   timestamptz,
    ADD COLUMN investigation_concluded_at timestamptz,
    ADD COLUMN reimbursed_at              timestamptz,
    -- The policy in force when the customer reported. A later policy does not
    -- move the deadlines of a complaint already running.
    ADD COLUMN clock_policy_version       int REFERENCES clock_policies(version);

CREATE INDEX cases_reported_idx ON cases(first_reported_at) WHERE first_reported_at IS NOT NULL;

-- One row per clock per case, ever. The primary key is what makes the sweep
-- safe to run from more than one process.
CREATE TABLE clock_breaches (
    case_id             bigint      NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    clock_code          text        NOT NULL,
    policy_version      int         NOT NULL REFERENCES clock_policies(version),
    due_at              timestamptz NOT NULL,
    detected_at         timestamptz NOT NULL DEFAULT now(),
    escalated           boolean     NOT NULL,
    PRIMARY KEY (case_id, clock_code)
);

GRANT SELECT, INSERT, UPDATE ON clock_policies, public_holidays TO riskradar_app;
GRANT SELECT, INSERT ON clock_breaches TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE clock_policies_id_seq TO riskradar_app;

INSERT INTO clock_policies (version, is_active, source, notes, definitions) VALUES (
    1, true,
    'CBN Exposure Draft, Guidelines for Handling Authorised Push Payment Fraud, 26 Nov 2025',
    'Draft values. Re-check against the final circular before treating as settled (D71).',
    '[
      {"code": "CUSTOMER_REPORT_WINDOW", "owner": "CUSTOMER", "escalate": false,
       "obligation": "Customer reports the fraud", "starts": "fraud_first_at", "ends": "first_reported_at",
       "amount": 72, "unit": "HOURS"},
      {"code": "ACKNOWLEDGE_REPORT", "owner": "BANK",
       "obligation": "Acknowledge the customer''s report", "starts": "first_reported_at", "ends": "acknowledged_at",
       "amount": 24, "unit": "HOURS"},
      {"code": "NOTIFY_COUNTERPARTY", "owner": "BANK",
       "obligation": "Notify the receiving institution", "starts": "first_reported_at", "ends": "counterparty_notified_at",
       "amount": 30, "unit": "MINUTES"},
      {"code": "CONCLUDE_INVESTIGATION", "owner": "BANK",
       "obligation": "Conclude the investigation", "starts": "first_reported_at", "ends": "investigation_concluded_at",
       "amount": 14, "unit": "WORKING_DAYS"},
      {"code": "REIMBURSE_AFTER_INVESTIGATION", "owner": "BANK", "requires_outcome": "CONFIRMED_FRAUD",
       "obligation": "Reimburse after the investigation", "starts": "investigation_concluded_at", "ends": "reimbursed_at",
       "amount": 48, "unit": "HOURS"},
      {"code": "TOTAL_REFUND", "owner": "BANK", "requires_outcome": "CONFIRMED_FRAUD",
       "obligation": "Complete the refund, from first report", "starts": "first_reported_at", "ends": "reimbursed_at",
       "amount": 16, "unit": "WORKING_DAYS"}
    ]'::jsonb
);

-- Generated by riskradar.clocks.calendar (easter_sunday, observed): weekend
-- holidays moved to the next free working day. Islamic holidays are
-- astronomical estimates until declared; mark them confirmed when they are.
INSERT INTO public_holidays (holiday_date, name, confirmed, source) VALUES
    ('2026-01-01', 'New Year''s Day', true, 'Public Holidays Act'),
    ('2026-03-20', 'Eid el-Fitr', false, 'estimate'),
    ('2026-03-23', 'Eid el-Fitr holiday (observed)', false, 'estimate'),
    ('2026-04-03', 'Good Friday', true, 'Public Holidays Act'),
    ('2026-04-06', 'Easter Monday', true, 'Public Holidays Act'),
    ('2026-05-01', 'Workers'' Day', true, 'Public Holidays Act'),
    ('2026-05-27', 'Eid el-Kabir', false, 'estimate'),
    ('2026-05-28', 'Eid el-Kabir holiday', false, 'estimate'),
    ('2026-06-12', 'Democracy Day', true, 'Public Holidays Act'),
    ('2026-08-26', 'Id el-Maulud', false, 'estimate'),
    ('2026-10-01', 'Independence Day', true, 'Public Holidays Act'),
    ('2026-12-25', 'Christmas Day', true, 'Public Holidays Act'),
    ('2026-12-28', 'Boxing Day (observed)', true, 'Public Holidays Act'),
    ('2027-01-01', 'New Year''s Day', true, 'Public Holidays Act'),
    ('2027-03-10', 'Eid el-Fitr', false, 'estimate'),
    ('2027-03-11', 'Eid el-Fitr holiday', false, 'estimate'),
    ('2027-03-26', 'Good Friday', true, 'Public Holidays Act'),
    ('2027-03-29', 'Easter Monday', true, 'Public Holidays Act'),
    ('2027-05-03', 'Workers'' Day (observed)', true, 'Public Holidays Act'),
    ('2027-05-17', 'Eid el-Kabir', false, 'estimate'),
    ('2027-05-18', 'Eid el-Kabir holiday', false, 'estimate'),
    ('2027-06-14', 'Democracy Day (observed)', true, 'Public Holidays Act'),
    ('2027-08-16', 'Id el-Maulud (observed)', false, 'estimate'),
    ('2027-10-01', 'Independence Day', true, 'Public Holidays Act'),
    ('2027-12-27', 'Christmas Day (observed)', true, 'Public Holidays Act'),
    ('2027-12-28', 'Boxing Day (observed)', true, 'Public Holidays Act');
