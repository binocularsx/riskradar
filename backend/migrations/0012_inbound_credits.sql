-- WP-03 / D78: watch the money coming in (base PRD FR-209, REG-NG-08).
--
-- Until now every transaction was money leaving a customer's account. A Nigerian
-- bank can now lose money on fraud it merely received: the APP framework lets
-- the CBN direct NIBSS to withhold settlement from beneficiary institutions,
-- and a mule account is, first of all, an account that receives.
--
-- A credit into one of our customers' accounts is a transaction with
-- direction INBOUND. It reuses everything a payment already has: the
-- idempotent door, the queue, the immutable decision, alerts, cases and
-- directives. What differs is named here:
--
--   * account_token / subject_token are the RECEIVING account and customer;
--   * remitter_token is the sender's account, tokenised in the account
--     namespace (D9d), and beneficiary_token is empty;
--   * every feature that reads outgoing history filters direction = 'OUTBOUND',
--     so no existing feature changes meaning (feature spec 1.3.0).

ALTER TABLE transactions
    ADD COLUMN direction text NOT NULL DEFAULT 'OUTBOUND'
        CHECK (direction IN ('OUTBOUND', 'INBOUND')),
    ADD COLUMN remitter_token text,
    ADD COLUMN remitter_bank_code text,
    ADD CONSTRAINT inbound_has_a_remitter_not_a_beneficiary
        CHECK (direction = 'OUTBOUND' OR (remitter_token IS NOT NULL AND beneficiary_token IS NULL)),
    ADD CONSTRAINT outbound_has_no_remitter
        CHECK (direction = 'INBOUND' OR remitter_token IS NULL);

-- The receiving-side profile reads an account's recent credits.
CREATE INDEX transactions_inbound_account_idx ON transactions(account_token, occurred_at)
    WHERE direction = 'INBOUND';

-- An inbound credit's envelope row is a CREDIT, linked like a PAYMENT.
ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'CREDIT';

-- Compared as text: a new enum value cannot be used as a literal in the same
-- transaction that adds it.
ALTER TABLE events DROP CONSTRAINT payment_links_its_transaction;
ALTER TABLE events ADD CONSTRAINT money_events_link_their_transaction
    CHECK ((event_type::text IN ('PAYMENT', 'CREDIT')) = (transaction_id IS NOT NULL));
