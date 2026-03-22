from __future__ import annotations

import secrets
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from core.database import DbSession
from core.models import User, UserRole, Merchant, MerchantStatus, AdminNotification
from services.api.auth_utils import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    name: str = Field(..., max_length=255)

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    merchant_id: str | None = None
    role: str
    api_key: str | None = None

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/register", status_code=status.HTTP_201_CREATED, summary="Регистрация нового мерчанта")
async def register(payload: RegisterRequest, db: DbSession):
    """
    Самостоятельная регистрация мерчанта.
    Создает мерчанта в статусе PENDING и пользователя с ролью MERCHANT.
    """
    # 1. Проверка уникальности email
    result = await db.execute(select(User).where(User.email == payload.email))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="User with this email already exists"
        )
    
    # 2. Создание мерчанта
    merchant = Merchant(
        name=payload.name,
        email=payload.email,
        api_key=f"sk_{secrets.token_urlsafe(32)}",
        status=MerchantStatus.PENDING,
        tier="BRONZE"
    )
    db.add(merchant)
    await db.flush()  # Получаем ID мерчанта
    
    # 3. Создание пользователя
    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=UserRole.MERCHANT,
        merchant_id=merchant.id,
        is_active=True
    )
    db.add(user)
    
    # 4. Уведомление администратора
    notification = AdminNotification(
        type="MERCHANT_REGISTERED",
        message=f"Новый мерчант зарегистрирован: {payload.name} ({payload.email}). Ожидает одобрения."
    )
    db.add(notification)
    
    await db.commit()
    return {"message": "Success. Your account is pending approval.", "merchant_id": str(merchant.id)}


@router.post("/login", response_model=TokenOut, summary="Вход в систему")
async def login(payload: LoginRequest, db: DbSession) -> TokenOut:
    """
    Аутентификация пользователя и выдача JWT токена.
    Работает как для админов, так и для мерчантов.
    """
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled"
        )
        
    access_token = create_access_token(
        data={"sub": str(user.id), "role": user.role.value}
    )
    
    api_key = None
    if user.merchant_id:
        res = await db.execute(select(Merchant.api_key).where(Merchant.id == user.merchant_id))
        api_key = res.scalar_one_or_none()

    return TokenOut(
        access_token=access_token,
        merchant_id=str(user.merchant_id) if user.merchant_id else None,
        role=user.role.value,
        api_key=api_key
    )
