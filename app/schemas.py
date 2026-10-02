import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SEAT_LABEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,15}$"

SeatStatus = Literal["available", "held", "confirmed"]


def _no_duplicates(seats: list[str]) -> list[str]:
    if len(set(seats)) != len(seats):
        raise ValueError("seat labels must be unique")
    return seats


class CreateShowRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    seats: list[str] = Field(min_length=1)
    price_paise: int = Field(ge=0)  # integer paise; strict mode rejects 250.0 and "250"
    per_user_limit: int | None = Field(default=None, gt=0)
    hold_ttl_seconds: int | None = Field(default=None, gt=0, le=3600)

    model_config = {"strict": True}

    @field_validator("seats")
    @classmethod
    def _seats(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if not re.match(SEAT_LABEL_PATTERN, s)]
        if bad:
            raise ValueError(f"invalid seat labels: {bad[:5]}")
        return _no_duplicates(v)


class SeatView(BaseModel):
    seat: str
    status: SeatStatus


class SeatCounts(BaseModel):
    total: int
    available: int
    held: int
    confirmed: int


class ShowView(BaseModel):
    id: str
    name: str
    price_paise: int
    per_user_limit: int
    hold_ttl_seconds: int
    total_seats: int
    created_at: str
    counts: SeatCounts
    seats: list[SeatView]


IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"


class ReserveRequest(BaseModel):
    # Unknown fields (e.g. a spoofed "user_id") are ignored: identity is the token's.
    seats: list[str] = Field(min_length=1, max_length=10)
    idempotency_key: str | None = Field(default=None, pattern=IDEMPOTENCY_KEY_PATTERN)
    # false: confirm immediately. true: time-boxed hold that must be confirmed
    # before hold_ttl_seconds or it expires back to available.
    hold: bool = False

    model_config = {"strict": True, "extra": "ignore"}

    @field_validator("seats")
    @classmethod
    def _seats(cls, v: list[str]) -> list[str]:
        return _no_duplicates(v)


ReservationStatus = Literal["held", "confirmed", "cancelled", "expired"]


class ReservationView(BaseModel):
    reservation_id: str
    show_id: str
    user_id: str
    seats: list[str]
    amount_paise: int
    status: ReservationStatus
    hold_expires_at: str | None = None
    created_at: str
