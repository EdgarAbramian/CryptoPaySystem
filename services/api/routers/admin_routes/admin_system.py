from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Annotated

import psutil
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from core.config import settings
from core.database import DbSession
from core.models import User, AuditLog
from services.api.admin_auth import require_admin

router = APIRouter(
    prefix="",
    tags=["admin-system"],
    dependencies=[Depends(require_admin)],
)

# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: str

    model_config = {"from_attributes": True}


class AuditLogOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: str | None
    created_at: str

    model_config = {"from_attributes": True}


class SystemHealthOut(BaseModel):
    status: str
    cpu_load: float
    ram_usage: float
    disk_usage: float
    db_connected: bool
    redis_connected: bool
    uptime_seconds: int
    celery_workers: int


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users", response_model=list[UserOut], summary="Управление списком пользователей")
async def list_users(db: DbSession) -> list[UserOut]:
    result = await db.execute(select(User))
    users = result.scalars().all()
    return [
        UserOut(
            id=u.id,
            email=u.email,
            role=u.role,
            is_active=u.is_active,
            created_at=u.created_at.isoformat(),
        )
        for u in users
    ]


@router.get("/audit-logs", response_model=list[AuditLogOut], summary="Лог действий пользователей")
async def list_audit_logs(
    db: DbSession,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditLogOut]:
    result = await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    )
    logs = result.scalars().all()
    return [
        AuditLogOut(
            id=log.id,
            user_id=log.user_id,
            action=log.action,
            target_type=log.target_type,
            target_id=log.target_id,
            created_at=log.created_at.isoformat(),
        )
        for log in logs
    ]


@router.get("/system/health", response_model=SystemHealthOut, summary="Мониторинг состояния серверов")
async def get_system_health(db: DbSession) -> SystemHealthOut:
    """
    Комплексный мониторинг состояния серверов.
    """
    cpu_load = psutil.cpu_percent()
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    db_ok = True
    try:
        await db.execute(select(func.now()))
    except Exception:
        db_ok = False

    redis_ok = True
    try:
        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=1)
        await r.ping()
        await r.aclose()
    except Exception:
        redis_ok = False

    # Процесс аптайм (время работы текущего API процесса)
    process = psutil.Process()
    uptime = int(datetime.utcnow().timestamp() - process.create_time())

    return SystemHealthOut(
        status="healthy" if db_ok and redis_ok and cpu_load < 90 else "degraded",
        cpu_load=cpu_load,
        ram_usage=ram.percent,
        disk_usage=disk.percent,
        db_connected=db_ok,
        redis_connected=redis_ok,
        uptime_seconds=uptime,
        celery_workers=0, # В данном проекте используются кастомные воркеры вместо Celery
    )
