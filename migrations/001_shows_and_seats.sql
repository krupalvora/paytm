CREATE TABLE shows (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text        NOT NULL,
    price_paise      bigint      NOT NULL CHECK (price_paise >= 0),
    per_user_limit   integer     NOT NULL CHECK (per_user_limit > 0),
    hold_ttl_seconds integer     NOT NULL CHECK (hold_ttl_seconds > 0),
    total_seats      integer     NOT NULL CHECK (total_seats > 0),
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- One row per physical seat. The seat row IS the unit of contention: every
-- state change is a conditional UPDATE on this row, so a seat can only ever be
-- in exactly one state and owned by at most one reservation.
CREATE TABLE seats (
    show_id         uuid        NOT NULL REFERENCES shows (id) ON DELETE CASCADE,
    label           text        NOT NULL,
    position        integer     NOT NULL,  -- order as given at creation (seat map order)
    status          text        NOT NULL DEFAULT 'available'
                                CHECK (status IN ('available', 'held', 'confirmed')),
    reservation_id  uuid,
    user_id         text,
    hold_expires_at timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (show_id, label),
    -- Ownership fields must agree with status; a half-written seat is impossible.
    CONSTRAINT seat_state_consistent CHECK (
        (status = 'available' AND reservation_id IS NULL AND user_id IS NULL AND hold_expires_at IS NULL)
     OR (status = 'held'      AND reservation_id IS NOT NULL AND user_id IS NOT NULL AND hold_expires_at IS NOT NULL)
     OR (status = 'confirmed' AND reservation_id IS NOT NULL AND user_id IS NOT NULL AND hold_expires_at IS NULL)
    )
);
