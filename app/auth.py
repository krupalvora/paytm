import hmac
import re
import time

import jwt
from fastapi import Request

from app.errors import AppError
from app.observability import user_id_var

USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_ALGO = "HS256"


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AppError(401, "unauthorized", "missing bearer token")
    return token.strip()


async def require_admin(request: Request) -> None:
    token = _bearer(request)
    expected = request.app.state.settings.admin_token
    if not hmac.compare_digest(token.encode(), expected.encode()):
        raise AppError(403, "forbidden", "admin token required")


def issue_user_token(secret: str, user_id: str, ttl_s: int) -> str:
    now = int(time.time())
    return jwt.encode({"sub": user_id, "iat": now, "exp": now + ttl_s}, secret, algorithm=_ALGO)


async def current_user(request: Request) -> str:
    """The ONLY source of user identity. Request bodies never carry it."""
    token = _bearer(request)
    try:
        claims = jwt.decode(
            token,
            request.app.state.settings.jwt_secret,
            algorithms=[_ALGO],  # pinned: no alg=none / alg confusion
            options={"require": ["sub", "exp"]},
        )
    except jwt.PyJWTError:
        raise AppError(401, "unauthorized", "invalid or expired token") from None
    sub = claims["sub"]
    if not isinstance(sub, str) or not USER_ID_PATTERN.match(sub):
        raise AppError(401, "unauthorized", "invalid subject")
    user_id_var.set(sub)  # tags every log line of this request
    return sub
