-- WP-06 / D73: the twenty-four hour flag (base PRD REG-NG-04).
--
-- CBN's addendum to the BVN framework (12 March 2026, in force 1 May 2026)
-- lets a bank place a customer on a temporary watch-list for at most 24 hours,
-- during which it must contact the customer for clarification. Our existing
-- lists (0003) hold destination accounts and never expire; this is the other
-- shape: a customer, a hard ceiling, and an obligation attached.
--
-- Keyed on subject_token, the customer. The BVN is the bank-wide identity
-- behind it; Risk Radar never receives a BVN (REG-NG-03 is not built), so the
-- bank maps the customer to their BVN when it applies the restriction (D9d).
--
-- Placing, contacting, lifting and expiry are all written to the hash-chained
-- record by the application. The row itself only ever moves forward: once
-- contacted or lifted, those columns do not change again (trigger below).

CREATE TYPE watchlist_lift_reason AS ENUM ('EXPIRED', 'CLEARED');
CREATE TYPE watchlist_contact_outcome AS ENUM ('CUSTOMER_CONFIRMED_GENUINE', 'CUSTOMER_REPORTED_FRAUD');

CREATE TABLE subject_watchlist (
    id                  bigserial   PRIMARY KEY,
    subject_token       text        NOT NULL,
    case_id             bigint      REFERENCES cases(id) ON DELETE SET NULL,
    reason              text        NOT NULL,
    placed_at           timestamptz NOT NULL DEFAULT now(),
    placed_by           bigint      NOT NULL REFERENCES users(id),
    expires_at          timestamptz NOT NULL,
    customer_contacted_at timestamptz,
    contacted_by        bigint      REFERENCES users(id),
    contact_outcome     watchlist_contact_outcome,
    lifted_at           timestamptz,
    lifted_by           bigint      REFERENCES users(id),
    lift_reason         watchlist_lift_reason,
    -- The law's ceiling, in the schema: no code path can place a longer flag.
    CONSTRAINT at_most_24_hours
        CHECK (expires_at > placed_at AND expires_at <= placed_at + interval '24 hours'),
    CONSTRAINT contact_is_complete
        CHECK ((customer_contacted_at IS NULL) = (contact_outcome IS NULL)
               AND (customer_contacted_at IS NULL) = (contacted_by IS NULL)),
    CONSTRAINT lift_is_complete
        CHECK ((lifted_at IS NULL) = (lift_reason IS NULL)
               AND (lifted_by IS NULL OR lifted_at IS NOT NULL)),
    CONSTRAINT no_lift_after_expiry
        CHECK (lifted_at IS NULL OR lifted_at <= expires_at)
);

-- One open flag per customer. "Open" is "not lifted"; a flag past its expiry
-- is inactive in every read even before the sweep lifts it (see clocks/watchlist.py).
CREATE UNIQUE INDEX subject_watchlist_one_open ON subject_watchlist(subject_token)
    WHERE lifted_at IS NULL;
CREATE INDEX subject_watchlist_expiry_idx ON subject_watchlist(expires_at) WHERE lifted_at IS NULL;
CREATE INDEX subject_watchlist_case_idx ON subject_watchlist(case_id);

CREATE OR REPLACE FUNCTION watchlist_moves_forward_only() RETURNS trigger AS $$
BEGIN
    IF NEW.subject_token IS DISTINCT FROM OLD.subject_token
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

CREATE TRIGGER subject_watchlist_forward_only BEFORE UPDATE ON subject_watchlist
    FOR EACH ROW EXECUTE FUNCTION watchlist_moves_forward_only();

GRANT SELECT, INSERT, UPDATE ON subject_watchlist TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE subject_watchlist_id_seq TO riskradar_app;
