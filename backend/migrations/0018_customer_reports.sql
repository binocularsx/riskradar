-- D90: a customer's report is the strongest signal the desk ever gets.
--
-- Until now a report could only be recorded on a case the system had already
-- opened. Fraud the system never alerted on, which is exactly the fraud a model
-- most needs to learn from, had no way in. A report now names the payments;
-- any that were not alerted get an alert raised by the report, so the case,
-- its timeline, the clocks and the outcome label all work as for any other.
-- ``source`` says which alerts the detector raised and which a customer did,
-- so "fraud we missed" is a number that can be counted.

ALTER TABLE alerts ADD COLUMN source text NOT NULL DEFAULT 'DETECTOR'
    CHECK (source IN ('DETECTOR', 'CUSTOMER_REPORT'));
CREATE INDEX alerts_customer_report_idx ON alerts (raised_at DESC) WHERE source = 'CUSTOMER_REPORT';
