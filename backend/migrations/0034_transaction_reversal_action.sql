-- D107: reversing the payment joins the actions a finding can ask the bank for.
--
-- D97 gave the desk four: stop debits, restrict a channel, freeze the card, and
-- block the destination. All four stop the *next* payment. None of them address
-- the money that already moved, which is the first thing anyone asks about a
-- confirmed fraud and the only one with a clock on it.
--
-- Alone in its own migration because PostgreSQL refuses to use a new enum value
-- in the transaction that added it; the column and its constraint follow in
-- 0035.

ALTER TYPE restriction_action ADD VALUE IF NOT EXISTS 'TRANSACTION_REVERSAL';
