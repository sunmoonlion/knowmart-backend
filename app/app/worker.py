"""Celery application — 与 celeryworker Deployment 共用同一 backend 镜像。"""

from __future__ import annotations

import os

from celery import Celery, signals
from kombu import Exchange, Queue

from app.infrastructure.logging.logging import configure_library_logging

signals.after_setup_logger.connect(
    configure_library_logging,
    weak=False,
    dispatch_uid="sunmoonai.library-logging.v1",
)

celery_app = Celery("backend")
_configured = False


def is_celery_configured() -> bool:
    return _configured


def configure_celery(*, require_broker: bool = False) -> bool:
    """按 CELERY_BROKER_URL 配置 broker；本地未设置时不抛错。"""
    global _configured
    if _configured:
        return True

    from core.config import get_settings

    settings = get_settings()
    broker = settings.celery_broker_url or os.environ.get("CELERY_BROKER_URL")
    if not broker:
        if require_broker:
            raise RuntimeError("CELERY_BROKER_URL is required for Celery")
        return False

    queue = (
        settings.celery_queue
        or os.environ.get("CELERY_QUEUE")
        or os.environ.get("CELERY_TASK_DEFAULT_QUEUE")
        or "default"
    )
    result_backend = settings.celery_result_backend or os.environ.get(
        "CELERY_RESULT_BACKEND"
    )
    task_exchange = Exchange(queue, type="direct", durable=True)
    task_queue = Queue(
        queue,
        exchange=task_exchange,
        routing_key=queue,
        durable=True,
        no_declare=settings.celery_task_topology_predeclared,
    )

    celery_app.conf.update(
        broker_url=broker,
        result_backend=result_backend or None,
        task_default_queue=queue,
        task_default_exchange=queue,
        task_default_exchange_type="direct",
        task_default_routing_key=queue,
        task_queues=(task_queue,),
        # A typo must not synthesize another queue outside the provisioned topology.
        task_create_missing_queues=not settings.celery_task_topology_predeclared,
        broker_transport_options=(
            {"confirm_publish": True}
            if settings.celery_task_topology_predeclared
            else {}
        ),
        task_routes={
            ("*" if settings.celery_task_topology_predeclared else "app.tasks.*"): {
                "queue": queue,
                "exchange": queue,
                "routing_key": queue,
                **(
                    {"mandatory": True, "confirm_timeout": 5.0}
                    if settings.celery_task_topology_predeclared
                    else {}
                ),
            }
        },
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        worker_prefetch_multiplier=int(
            os.environ.get("CELERY_WORKER_PREFETCH_MULTIPLIER", "1")
        ),
        task_acks_late=os.environ.get("CELERY_TASK_ACKS_LATE", "true").lower()
        in ("1", "true", "yes"),
        worker_max_tasks_per_child=int(
            os.environ.get("CELERY_WORKER_MAX_TASKS_PER_CHILD", "1000")
        ),
    )
    _configured = True
    return True


if os.environ.get("CELERY_BROKER_URL"):
    configure_celery()

import app.tasks.durable_delivery  # noqa: E402, F401 — shared durable transport
import app.tasks.market  # noqa: E402, F401 — 供需市场的定时活
import app.tasks.ping  # noqa: E402, F401 — register tasks

celery_app.conf.beat_schedule = {
    **(celery_app.conf.beat_schedule or {}),
    "durable-delivery": {"task": "app.tasks.durable_delivery.pump", "schedule": 5.0},
    "market-sweep": {"task": "app.tasks.market.sweep", "schedule": 3600.0},
}
