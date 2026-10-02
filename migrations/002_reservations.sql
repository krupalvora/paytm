CREATE TABLE reservations (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    show_id         uuid        NOT NULL REFERENCES shows (id) ON DELETE CASCADE,
    user_id         text        NOT NULL,
    seats           text[]      NOT NULL,
    amount_paise    bigint      NOT NULL CHECK (amount_paise >= 0),
    status          text        NOT NULL CHECK (status IN ('held', 'confirmed', 'cancelled', 'expired')),
    idempotency_key text        NOT NULL,
    -- sha256 of the canonical request (show, sorted seats, hold flag); a reused
    -- key with a different hash is rejected instead of replayed.
    request_hash    text        NOT NULL,
    hold_expires_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- Exactly-once: a key can create at most one reservation per user, enforced
    -- by the index, not by application checks. Scoped per user so one user's
    -- key can never collide with (or replay) another user's.
    CONSTRAINT reservations_idempotency UNIQUE (user_id, idempotency_key),
    CONSTRAINT reservation_hold_consistent CHECK ((status = 'held') = (hold_expires_at IS NOT NULL))
);

CREATE INDEX reservations_expiring ON reservations (hold_expires_at) WHERE status = 'held';

ALTER TABLE seats
    ADD CONSTRAINT seats_reservation_fk FOREIGN KEY (reservation_id) REFERENCES reservations (id);

-- Per-user, per-show count of seats currently held or confirmed. The limit is
-- enforced by a guarded upsert on this row (seats_held + n <= limit), which
-- also serialises concurrent reserves from the same user on the same show.
CREATE TABLE user_show_seats (
    show_id    uuid    NOT NULL REFERENCES shows (id) ON DELETE CASCADE,
    user_id    text    NOT NULL,
    seats_held integer NOT NULL CHECK (seats_held >= 0),
    PRIMARY KEY (show_id, user_id)
);
