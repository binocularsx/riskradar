-- D103: a restriction can be lifted, and a wrong one can be undone (plan §5.2, §5.3).
--
-- D97 delivers an approved restriction to the bank and records what the bank
-- did. It has no way back. That is the half of a customer-impacting control
-- that matters most when it is wrong: an account restricted on a finding later
-- shown to be mistaken stays restricted, and the only "fix" available was to
-- edit or delete the original record, which the plan forbids outright.
--
-- So a lift is its **own** order, of kind RELEASE, naming the order it lifts.
-- It is proposed, approved by a second person, delivered and acknowledged
-- exactly like a restriction, because asking a bank to unblock a customer is
-- no less consequential than asking it to block one. The original order is
-- never rewritten: the pair is the history.

ALTER TABLE restriction_orders
    ADD COLUMN kind text NOT NULL DEFAULT 'RESTRICT' CHECK (kind IN ('RESTRICT', 'RELEASE'));

-- Which order this one lifts. Set on a RELEASE, null on a RESTRICT.
ALTER TABLE restriction_orders
    ADD COLUMN releases_order_id bigint REFERENCES restriction_orders(id) ON DELETE CASCADE;

-- When a temporary restriction should end. The bank applies its own expiry if
-- it has one; when it does not, the reconciliation sweep proposes the lift, so
-- a restriction cannot outlive its reason by neglect.
ALTER TABLE restriction_orders ADD COLUMN expires_at timestamptz;

ALTER TABLE restriction_orders
    ADD CONSTRAINT release_names_its_original
        CHECK ((kind = 'RELEASE') = (releases_order_id IS NOT NULL));

-- One live lift per restriction: two people cannot both be releasing the same
-- account. A rejected or failed lift leaves the original standing, and a fresh
-- one may be proposed.
CREATE UNIQUE INDEX restriction_one_release_per_order
    ON restriction_orders (releases_order_id)
    WHERE kind = 'RELEASE';

CREATE INDEX restriction_orders_expiring_idx ON restriction_orders (expires_at)
    WHERE kind = 'RESTRICT' AND expires_at IS NOT NULL;

-- The audited emergency switch the plan asks for: outbound delivery can be
-- paused while detection, investigation and approval carry on. Paused means
-- messages queue in the outbox, not that they are lost.
INSERT INTO app_config (key, value) VALUES
    ('restriction_delivery_paused', 'false'),
    ('restriction_pause_reason', '""'),
    -- How long a delivered restriction may go unacknowledged before it is an
    -- owned exception rather than a message in flight.
    ('restriction_ack_overdue_hours', '24')
ON CONFLICT (key) DO NOTHING;
