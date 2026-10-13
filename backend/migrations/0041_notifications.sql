-- D114: notifications between the people on the desk.
--
-- The stream (D14) tells every screen that may see a case that something
-- happened to it. That is right for a queue and wrong for a person: a lead may
-- see every case, so "everything I can see" is 130 alerts a day. A notification
-- is addressed to one person because something needs them, or because their own
-- work changed, and it is kept until they have seen it.
--
-- One row per recipient, written in the same transaction as the action that
-- caused it, so a notification never exists without its action and an action
-- never commits without its notification.
--
-- `urgent` notifications ask for something to be done (a case assigned, a
-- finding sent back, an approval waiting). They are `resolved_at` when the work
-- is done — by the recipient or by someone else, which `resolved_note` says —
-- so a second lead is not left chasing an approval the first already gave.
--
-- `times` lets repeated news about the same case merge into one line rather
-- than a list of identical rows.
--
-- Not an audit record: rows are read and marked read. audit_log stays the
-- authority on who did what (D12c).

CREATE TABLE notifications (
    id              bigserial   PRIMARY KEY,
    recipient_id    bigint      NOT NULL REFERENCES users(id),
    kind            text        NOT NULL,
    urgent          boolean     NOT NULL DEFAULT false,
    case_id         bigint      REFERENCES cases(id) ON DELETE CASCADE,
    ref             text,
    title           text        NOT NULL,
    body            text,
    link            text,
    actor_id        bigint      REFERENCES users(id),
    times           int         NOT NULL DEFAULT 1,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    read_at         timestamptz,
    resolved_at     timestamptz,
    resolved_note   text
);

-- The bell: a person's unread items, newest first.
CREATE INDEX notifications_recipient_idx ON notifications (recipient_id, updated_at DESC);
-- Resolving the open copies of one piece of work across everyone it was sent to.
CREATE INDEX notifications_open_ref_idx ON notifications (kind, ref) WHERE resolved_at IS NULL;

GRANT SELECT, INSERT, UPDATE ON notifications TO riskradar_app;
GRANT USAGE, SELECT ON SEQUENCE notifications_id_seq TO riskradar_app;
