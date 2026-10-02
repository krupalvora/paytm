from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.auth import USER_ID_PATTERN, issue_user_token
from app.errors import AppError

router = APIRouter(tags=["auth"])


class TokenRequest(BaseModel):
    user_id: str = Field(pattern=USER_ID_PATTERN.pattern)


@router.post("/auth/token")
async def token(body: TokenRequest, request: Request) -> dict:
    """Demo identity provider: mints a signed token for any user id.

    Stands in for a real IdP so load tests can create many users. Disable with
    ALLOW_TOKEN_ISSUE=false; the rest of the service only trusts the signature.
    """
    settings = request.app.state.settings
    if not settings.allow_token_issue:
        raise AppError(404, "not_found", "token issuance disabled")
    tok = issue_user_token(settings.jwt_secret, body.user_id, settings.token_ttl_s)
    return {"access_token": tok, "token_type": "bearer", "user_id": body.user_id, "expires_in": settings.token_ttl_s}
