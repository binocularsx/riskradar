-- D107: a reversal has to name what it reverses.
--
-- The other four actions target an account, a beneficiary or a channel, which
-- the order already carries. A reversal targets one payment, and "reverse the
-- fraud on this case" is not an instruction a bank can execute. The reference
-- is the caller's own transaction_ref (FR-001), not our internal id, because
-- the bank has to find it in their system, not ours.

ALTER TABLE restriction_orders ADD COLUMN IF NOT EXISTS transaction_ref text;

-- A reversal without its transaction is unexecutable, and every other action
-- ignores the column. Enforced here rather than trusted to the callers: an
-- order that cannot be acted on is worse than one that was never raised,
-- because the desk believes the bank was asked.
ALTER TABLE restriction_orders
    ADD CONSTRAINT restriction_orders_reversal_names_a_transaction
    CHECK (action <> 'TRANSACTION_REVERSAL' OR transaction_ref IS NOT NULL);

COMMENT ON COLUMN restriction_orders.transaction_ref IS
    'D107: the payment a TRANSACTION_REVERSAL reverses, as the caller referenced it.';
