-- Correct the case-correlation constraint.
--
-- 0001 enforced "at most one open case per subject" with a partial unique index.
-- That is stricter than D13a actually says: an alert joins an open case for the
-- same subject *within the correlation window*, and otherwise **opens a new
-- case**. A case an analyst has left open for three days should not silently
-- absorb next week's unrelated incident — that would make queue metrics fiction
-- in the opposite direction from the problem correlation was built to solve.
--
-- The window cannot live in an index predicate (now() is not immutable), so the
-- race is closed with a transaction-scoped advisory lock keyed on the subject
-- token instead. See riskradar/cases/correlation.py.

DROP INDEX IF EXISTS cases_open_subject_uniq;

-- The lookup correlation actually performs.
CREATE INDEX cases_open_subject_window_idx
    ON cases (subject_token, correlation_expires_at DESC)
    WHERE state IN ('OPEN', 'UNDER_REVIEW', 'ESCALATED');
