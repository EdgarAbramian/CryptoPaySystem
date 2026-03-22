import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update

from core.database import DbSession
from core.models import AdminNotification
from services.api.admin_auth import require_admin

router = APIRouter(
    prefix="/notifications",
    tags=["admin-notifications"],
    dependencies=[Depends(require_admin)],
)

# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    message: str
    is_read: bool
    created_at: str

    model_config = {"from_attributes": True}

class UnreadCountOut(BaseModel):
    count: int

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/unread-count", response_model=UnreadCountOut, summary="Количество непрочитанных уведомлений")
async def get_unread_count(db: DbSession) -> UnreadCountOut:
    """Для отображения цифры на бейдже (колокольчик)."""
    from sqlalchemy import func
    
    query = select(func.count(AdminNotification.id)).where(AdminNotification.is_read == False)
    result = await db.execute(query)
    count = result.scalar() or 0
    return UnreadCountOut(count=count)


@router.get("", response_model=list[NotificationOut], summary="Список уведомлений")
async def list_notifications(
    db: DbSession,
    limit: int = Query(10, ge=1, le=50),
    offset: int = 0
) -> list[NotificationOut]:
    """Для показа в выпадающем списке."""
    query = (
        select(AdminNotification)
        .order_by(AdminNotification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(query)
    notifications = result.scalars().all()
    
    return [
        NotificationOut(
            id=n.id,
            type=n.type,
            message=n.message,
            is_read=n.is_read,
            created_at=n.created_at.isoformat()
        )
        for n in notifications
    ]


@router.patch("/{notification_id}/read", summary="Отметить как прочитанное")
async def mark_as_read(notification_id: uuid.UUID, db: DbSession):
    """Отметить конкретное уведомление как прочитанное."""
    query = (
        update(AdminNotification)
        .where(AdminNotification.id == notification_id)
        .values(is_read=True)
    )
    result = await db.execute(query)
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    await db.commit()
    return {"status": "ok"}


@router.post("/read-all", summary="Прочитать всё")
async def mark_all_as_read(db: DbSession):
    """Отметить все уведомления как прочитанные (сбросить счетчик)."""
    query = (
        update(AdminNotification)
        .where(AdminNotification.is_read == False)
        .values(is_read=True)
    )
    await db.execute(query)
    await db.commit()
    return {"status": "ok"}
