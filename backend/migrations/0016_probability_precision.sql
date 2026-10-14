-- D85: probabilities keep the precision the model's tie-break needs.
--
-- Isotonic calibration gives many payments exactly the same probability, so the
-- model adds a billionth of its raw score to order them within a band (D60d).
-- numeric(8,6) rounded that away the moment a decision or a threshold was
-- stored. Thresholds derived from stored decisions then sat on a tied value,
-- and every tied payment alerted: on IEEE-CIS a 100-a-day budget implied 486 a
-- day, because 10.8% of traffic shared the model's top probability to six
-- decimals. Twelve decimals keep the nudge (1e-9 of a score between 0 and 1).

ALTER TABLE decisions
    ALTER COLUMN p_fraud TYPE numeric(14,12);

ALTER TABLE threshold_sets
    ALTER COLUMN p_monitor TYPE numeric(14,12),
    ALTER COLUMN p_review  TYPE numeric(14,12),
    ALTER COLUMN p_hold    TYPE numeric(14,12);

ALTER TABLE disposition_policies
    ALTER COLUMN auto_close_max_probability TYPE numeric(14,12);
