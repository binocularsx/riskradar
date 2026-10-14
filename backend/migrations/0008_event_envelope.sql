-- WP-01 / D72: one event, not one transaction (base PRD FR-102 foundation).
--
-- Until now the only thing Risk Radar could be told was that money moved. A
-- login, a new device, a changed PIN, a newly enrolled payee, a SIM swap and a
-- raised transfer limit cannot be expressed as a transaction at all, and they
-- are the signals published account-takeover playbooks are built on.
--
-- ``events`` is the spine: one row per thing that happened, whatever its type,
-- keyed by the caller's idempotency reference and carrying the identity every
-- detector needs (customer, account, device, region, channel), tokenised at the
-- boundary exactly as payments are.
--
-- A payment is one event type among several. Its typed columns stay in
-- ``transactions`` — the feature package, the model and every stored decision
-- read them, and nothing about payment scoring changes — and its envelope row
-- links to it. Every other type keeps its type-specific facts in ``detail``,
-- validated per type at the boundary before it is written.

CREATE TYPE event_type AS ENUM (
    'PAYMENT',
    'LOGIN',
    'DEVICE_BOUND',
    'CREDENTIAL_CHANGED',
    'PAYEE_ADDED',
    'SIM_CHANGED',
    'LIMIT_CHANGED'
);

CREATE TABLE events (
    id                  bigserial   PRIMARY KEY,
    -- Caller-supplied idempotency key, unique across every type.
    event_ref           text        NOT NULL UNIQUE,
    event_type          event_type  NOT NULL,
    -- D9b holds for every type: detectors use occurred_at, never ingested_at.
    occurred_at         timestamptz NOT NULL,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    subject_token       text        NOT NULL,
    account_token       text,
    device_token        text,
    ip_region           text,
    channel             channel,
    -- Cascades so a retention delete removes a payment and its envelope together (0005).
    transaction_id      bigint      UNIQUE REFERENCES transactions(id) ON DELETE CASCADE,
    -- Type-specific facts, already tokenised. Empty for a payment.
    detail              jsonb       NOT NULL DEFAULT '{}'::jsonb,
    is_replay           boolean     NOT NULL DEFAULT false,
    CONSTRAINT payment_links_its_transaction
        CHECK ((event_type = 'PAYMENT') = (transaction_id IS NOT NULL))
);

-- The two questions WP-02 asks of this table: what did this customer do lately,
-- and what happened on this account lately.
CREATE INDEX events_subject_time_idx ON events(subject_token, occurred_at DESC);
CREATE INDEX events_account_time_idx ON events(account_token, occurred_at DESC)
    WHERE account_token IS NOT NULL;
CREATE INDEX events_type_time_idx    ON events(event_type, occurred_at DESC);

-- Events are observations: never altered in place. As 0005 settled for
-- transactions, immutable means "never silently altered", not "never removable";
-- a retention job may still delete. Row level, so it fires only on a real change.
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

GRANT SELECT, INSERT, DELETE ON events TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE events_id_seq TO riskradar_app;

-- Every payment already ingested gets its envelope, so "every event has an
-- envelope" is true from the first day rather than only for new traffic.
INSERT INTO events (event_ref, event_type, occurred_at, ingested_at, subject_token,
                    account_token, device_token, ip_region, channel, transaction_id, is_replay)
SELECT transaction_ref, 'PAYMENT', occurred_at, ingested_at, subject_token,
       account_token, device_token, ip_region, channel, id, is_replay
  FROM transactions
 ORDER BY id;
