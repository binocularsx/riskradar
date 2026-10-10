-- D75: BVN identity and the industry watch-list (base PRD REG-NG-03, REG-NG-04,
-- INT-05, FR-702). Closes the gap left open in D73c.
--
-- The CBN watch-list is a BVN watch-list, shared across the industry through
-- NIBSS. Risk Radar has no core banking connection and no NIBSS link yet, so
-- this migration builds everything on our side of those two connections and
-- leaves each connection as an adapter:
--
--   * The BVN (and NIN) arrive at the boundary, or are resolved there from the
--     core by customer number, and are tokenised like every other identifier.
--     A raw BVN is never stored (D9c).
--   * A temporary flag is placed on the BVN when it is known, so it covers
--     every customer record the same person holds.
--   * Every place, lift and expiry is written to an outbox in the same
--     transaction, and a dispatcher hands it to the industry connector. With
--     no connector, messages wait, visibly, with the reason.
--   * Flags other institutions place arrive through an inbound door, are
--     tokenised on arrival, and are shown on the case.

ALTER TABLE transactions ADD COLUMN bvn_token text;
ALTER TABLE events       ADD COLUMN bvn_token text;

-- Which customer records belong to which BVN. One BVN may hold several
-- customer records; one customer record has one BVN.
CREATE TABLE customer_identities (
    subject_token       text        PRIMARY KEY,
    bvn_token           text        NOT NULL,
    nin_token           text,
    source              text        NOT NULL CHECK (source IN ('BOUNDARY', 'CORE_LOOKUP')),
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    -- REG-NG-03: the registry's answer the last time it was asked.
    verification_status text        NOT NULL DEFAULT 'UNVERIFIED'
        CHECK (verification_status IN ('UNVERIFIED', 'VALID', 'INVALID', 'UNAVAILABLE')),
    verified_at         timestamptz,
    verification_source text
);
CREATE INDEX customer_identities_bvn_idx ON customer_identities(bvn_token);

-- A flag carries the BVN it was placed on, when known. Set at placement and
-- never rewritten (the 0009 trigger is extended below).
ALTER TABLE subject_watchlist ADD COLUMN bvn_token text;
CREATE UNIQUE INDEX subject_watchlist_one_open_per_bvn ON subject_watchlist(bvn_token)
    WHERE lifted_at IS NULL AND bvn_token IS NOT NULL;

CREATE OR REPLACE FUNCTION watchlist_moves_forward_only() RETURNS trigger AS $$
BEGIN
    IF NEW.subject_token IS DISTINCT FROM OLD.subject_token
       OR NEW.bvn_token IS DISTINCT FROM OLD.bvn_token
       OR NEW.case_id IS DISTINCT FROM OLD.case_id AND NEW.case_id IS NOT NULL
       OR NEW.reason IS DISTINCT FROM OLD.reason
       OR NEW.placed_at IS DISTINCT FROM OLD.placed_at
       OR NEW.placed_by IS DISTINCT FROM OLD.placed_by
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'subject_watchlist: a placed flag cannot be rewritten';
    END IF;
    IF OLD.customer_contacted_at IS NOT NULL
       AND (NEW.customer_contacted_at IS DISTINCT FROM OLD.customer_contacted_at
            OR NEW.contact_outcome IS DISTINCT FROM OLD.contact_outcome) THEN
        RAISE EXCEPTION 'subject_watchlist: customer contact is recorded once';
    END IF;
    IF OLD.lifted_at IS NOT NULL AND ROW(NEW.lifted_at, NEW.lift_reason, NEW.lifted_by)
                                     IS DISTINCT FROM ROW(OLD.lifted_at, OLD.lift_reason, OLD.lifted_by) THEN
        RAISE EXCEPTION 'subject_watchlist: a lifted flag stays lifted';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The transactional outbox to the industry watch-list.
CREATE TABLE watchlist_outbox (
    id                  bigserial   PRIMARY KEY,
    flag_id             bigint      NOT NULL REFERENCES subject_watchlist(id),
    operation           text        NOT NULL CHECK (operation IN ('PLACE', 'LIFT', 'EXPIRE')),
    -- Tokens only. The bank-side connector maps a token to the BVN it sends.
    payload             jsonb       NOT NULL,
    status              text        NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SENT', 'FAILED', 'NOT_SHAREABLE')),
    attempts            int         NOT NULL DEFAULT 0,
    last_error          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    next_attempt_at     timestamptz NOT NULL DEFAULT now(),
    sent_at             timestamptz,
    external_ref        text,
    UNIQUE (flag_id, operation)
);
CREATE INDEX watchlist_outbox_due_idx ON watchlist_outbox(next_attempt_at) WHERE status = 'PENDING';

-- Flags placed by other institutions, as received. Tokenised on arrival.
CREATE TABLE industry_watchlist (
    id                  bigserial   PRIMARY KEY,
    external_ref        text        NOT NULL UNIQUE,
    bvn_token           text        NOT NULL,
    institution_code    text        NOT NULL,
    reason_code         text        NOT NULL,
    flagged_at          timestamptz NOT NULL,
    expires_at          timestamptz NOT NULL,
    lifted_at           timestamptz,
    received_at         timestamptz NOT NULL DEFAULT now(),
    source              text        NOT NULL CHECK (source IN ('INBOUND_API', 'CONNECTOR_PULL')),
    CONSTRAINT industry_at_most_24_hours
        CHECK (expires_at > flagged_at AND expires_at <= flagged_at + interval '24 hours')
);
CREATE INDEX industry_watchlist_bvn_idx ON industry_watchlist(bvn_token, expires_at DESC);

GRANT SELECT, INSERT, UPDATE ON customer_identities, watchlist_outbox, industry_watchlist TO riskradar_app;
-- Retention (0005): identities and received flags may be removed; the outbox is history.
GRANT DELETE ON customer_identities, industry_watchlist TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE watchlist_outbox_id_seq, industry_watchlist_id_seq TO riskradar_app;
