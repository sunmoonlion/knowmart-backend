from fastapi import APIRouter, Depends, HTTPException

from app.infrastructure.messaging.celery_producer import (
    CeleryNotConfiguredError,
    get_celery_producer,
)
from app.interfaces.http.middleware.auth import require_knowmart_admin
from core.config import get_settings

router = APIRouter(
    prefix="/admin/v1/diagnostics/tasks",
    tags=["Admin diagnostics"],
    dependencies=[Depends(require_knowmart_admin)],
)


@router.post("/ping", summary="Enqueue a diagnostic Celery ping")
async def enqueue_ping() -> dict[str, str]:
    producer = get_celery_producer()
    if not producer.enabled:
        raise HTTPException(status_code=503, detail="Celery producer not configured")
    try:
        task_id = producer.dispatch_ping()
    except CeleryNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"task_id": task_id, "queue": get_settings().celery_queue}
