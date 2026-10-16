"""Exercise process-global logging in children, without changing pytest's logs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("role", ["api", "worker", "scheduler", "direct_worker"])
@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_runtime_logging_policy(role, level):
    code = r"""
import asyncio
import importlib
import logging
import os

import httpx
from celery.utils.log import get_task_logger
from sqlalchemy.exc import StatementError

from app.infrastructure.storage.postgres import Postgres

role = os.environ["TEST_LOG_ROLE"]
level = os.environ["LOG_LEVEL"]
# Simulate explicit library configuration before our process bootstrap.
logging.getLogger("httpcore.connection").setLevel(logging.DEBUG)
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.DEBUG)

if role == "api":
    from app.bootstrap.api import create_app
    create_app()
    create_app()
else:
    module = "app.worker" if role == "direct_worker" else f"app.bootstrap.{role}"
    celery_app = importlib.import_module(module).celery_app
    celery_app.log.setup(loglevel=level, redirect_stdouts=False)
    celery_app.log.setup(loglevel=level, redirect_stdouts=False)
    get_task_logger("app.tasks.logging_probe").info("task-log-retained")

app_logger = logging.getLogger("app.audit_probe")
app_logger.info("audit-log-retained")
app_logger.debug("app-debug-retained")
for name in ("sqlalchemy.engine.Engine", "sqlalchemy.pool.impl.AsyncAdaptedQueuePool",
             "httpx", "httpcore.connection", "httpcore.new_child"):
    logger = logging.getLogger(name)
    logger.info("wire-info-must-not-appear")
    logger.debug("wire-debug-must-not-appear")
    logger.warning("library-warning-retained")
    logger.error("library-error-retained")

with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as c:
    response = c.get("https://example.invalid/poll?secret=url-secret-sentinel")
    assert response.status_code == 200

async def inspect_engine():
    pg = Postgres()
    await pg.init()  # constructs real engine; no network or database access
    engine = pg._engine
    assert engine is not None
    assert engine.echo is False
    assert engine.sync_engine.hide_parameters is True
    error = StatementError("safe-error", "SELECT :value",
                           {"value": "sql-secret-sentinel"}, RuntimeError("safe"),
                           hide_parameters=engine.sync_engine.hide_parameters)
    assert "sql-secret-sentinel" not in str(error)
    assert "SELECT :value" in str(error)  # statements are NOT redacted
    await pg.shutdown()

asyncio.run(inspect_engine())
"""
    # Only synthetic settings; never inherit credentials or a local .env file.
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd="/tmp",
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "ENV": "development",
            "LOG_LEVEL": level,
            "TEST_LOG_ROLE": role,
            "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1:1/logging_tests",
            "CELERY_BROKER_URL": "memory://",
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "wire-info-must-not-appear" not in output
    assert "wire-debug-must-not-appear" not in output
    assert "url-secret-sentinel" not in output
    assert "sql-secret-sentinel" not in output
    assert output.count("audit-log-retained") == 1
    assert output.count("library-warning-retained") == 5
    assert output.count("library-error-retained") == 5
    assert output.count("app-debug-retained") == (1 if level == "DEBUG" else 0)
    assert output.count("task-log-retained") == (0 if role == "api" else 1)
