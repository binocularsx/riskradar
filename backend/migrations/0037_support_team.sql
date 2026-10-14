-- D109: the fraud desk recommends; the customer-facing support team acts.
--
-- The desk never contacts a customer or acts on a customer's profile. What it
-- used to do itself becomes a recommendation the support team carries out and
-- acknowledges, and its findings reach support as one lead-approved report.
--
-- 1. D106's account-manager report becomes the support report. Same durable
--    outbox, same one-per-approved-finding guarantee; a report can now also be
--    an urgent heads-up (D109d) or a request to contact a watch-flagged
--    customer (D109f), neither of which belongs to a finding.
-- 2. The recommended-action list gains the three actions an analyst used to
--    record as their own steps (D109b).
-- 3. A case remembers support's own ticket reference, so the report back can
--    quote it (D109e).

ALTER TABLE account_manager_reports RENAME TO support_reports;
ALTER SEQUENCE account_manager_reports_id_seq RENAME TO support_reports_id_seq;
ALTER INDEX account_manager_reports_due_idx RENAME TO support_reports_due_idx;
ALTER INDEX account_manager_reports_case_idx RENAME TO support_reports_case_idx;

ALTER TABLE support_reports
    ADD COLUMN kind text NOT NULL DEFAULT 'REPORT'
        CHECK (kind IN ('REPORT', 'HEADS_UP', 'CONTACT_REQUEST')),
    ALTER COLUMN submission_id DROP NOT NULL,
    -- A report answers a finding; the other two kinds never do.
    ADD CONSTRAINT support_reports_report_has_submission
        CHECK ((kind = 'REPORT') = (submission_id IS NOT NULL));

-- One heads-up per case: it is a single "hold, we are investigating", not a
-- running commentary. UNIQUE (submission_id) still holds one report per finding.
CREATE UNIQUE INDEX support_reports_one_heads_up
    ON support_reports (case_id) WHERE kind = 'HEADS_UP';

ALTER TABLE cases ADD COLUMN support_ticket_ref text;

ALTER TYPE restriction_action ADD VALUE IF NOT EXISTS 'CONTACT_CUSTOMER';
ALTER TYPE restriction_action ADD VALUE IF NOT EXISTS 'VERIFY_IDENTITY';
ALTER TYPE restriction_action ADD VALUE IF NOT EXISTS 'NOTIFY_RECEIVING_BANK';
