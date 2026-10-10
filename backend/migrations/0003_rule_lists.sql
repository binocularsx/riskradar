-- Lists the rules layer reads.
--
-- D11a: rules override (a deterministic veto — sanctioned beneficiary, known
-- mule) and suppress (an allowlist — pre-registered beneficiary). Both need
-- membership data, and membership has to be administrable without a code change
-- (FR-040) and audited when it changes (FR-041).
--
-- Note what these hold: HMAC tokens, never account numbers. A sanctions list
-- Risk Radar can read is still a list Risk Radar cannot resolve to a person.

CREATE TYPE beneficiary_list_kind AS ENUM ('SANCTIONED', 'KNOWN_MULE', 'ALLOWLIST');

CREATE TABLE beneficiary_lists (
    id            bigserial PRIMARY KEY,
    kind          beneficiary_list_kind NOT NULL,
    token         text        NOT NULL,          -- beneficiary account_token
    -- ALLOWLIST entries are scoped to one account: "this customer has paid this
    -- destination before, on purpose". SANCTIONED and KNOWN_MULE are global.
    account_token text,
    note          text,
    added_at      timestamptz NOT NULL DEFAULT now(),
    added_by      bigint REFERENCES users(id),
    CONSTRAINT allowlist_is_scoped
        CHECK ((kind = 'ALLOWLIST') = (account_token IS NOT NULL))
);

CREATE UNIQUE INDEX beneficiary_lists_global_uniq
    ON beneficiary_lists(kind, token) WHERE account_token IS NULL;
CREATE UNIQUE INDEX beneficiary_lists_scoped_uniq
    ON beneficiary_lists(kind, token, account_token) WHERE account_token IS NOT NULL;
CREATE INDEX beneficiary_lists_lookup_idx ON beneficiary_lists(token);

GRANT SELECT, INSERT, UPDATE, DELETE ON beneficiary_lists TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE beneficiary_lists_id_seq TO riskradar_app;
