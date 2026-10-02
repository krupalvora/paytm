from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST

from app import observability as obs

router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    settings = request.app.state.settings
    if request.app.state.migrated.is_set():
        await obs.DB_STATE.refresh(request.app.state.pool, settings.metrics_recent_shows, settings.metrics_db_timeout_s)
    else:
        obs.DB_STATE.mark_unavailable()
    return Response(obs.render_metrics(), media_type=CONTENT_TYPE_LATEST)
