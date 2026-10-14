-- D108: the analyst can act on account takeover without routing through a
-- specialist the desk does not have.
--
-- D12b gave compromise to INFOSEC_ANALYST, and the owner has directed that the
-- role is out of scope: there is no InfoSec specialist on this desk or in the
-- account, so "escalate to InfoSec" was a dead end that left takeover, login
-- abuse and MFA problems with nobody to action them.
--
-- The answer is not to let an analyst reach into a customer's account - they
-- never take a customer-facing action (D7 and the owner's rule). It is to give
-- them the same instrument they already have for money: a recommendation the
-- lead approves and the bank executes, reported onward to the account manager.
-- These three are the standard containment for a compromised login.

ALTER TYPE restriction_action ADD VALUE IF NOT EXISTS 'SESSION_TERMINATION';
ALTER TYPE restriction_action ADD VALUE IF NOT EXISTS 'CREDENTIAL_RESET';
ALTER TYPE restriction_action ADD VALUE IF NOT EXISTS 'MFA_REENROLMENT';
