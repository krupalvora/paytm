import hmac

from fastapi import Request

from app.errors import AppError


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
