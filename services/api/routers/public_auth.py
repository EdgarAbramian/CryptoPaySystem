from __future__ import annotations

import secrets
import uuid
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from core.database import DbSession
from core.models import Merchant, MerchantStatus, User, UserRole, Coin, Balance, AdminNotification
from services.api.auth_utils import create_access_token, verify_password, hash_password

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

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
    role: str
    merchant_id: uuid.UUID | None = None

@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_merchant(payload: RegisterRequest, db: DbSession):
    """
    Public merchant registration.
    Creates a Merchant (PENDING) and a User (MERCHANT).
    """
    # 1. Check if user already exists
    existing_user_result = await db.execute(select(User).where(User.email == payload.email))
    if existing_user_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    # 2. Create Merchant
    api_key = secrets.token_urlsafe(32)
    new_merchant = Merchant(
        id=uuid.uuid4(),
        api_key=api_key,
        name=payload.name,
        email=payload.email,
        status=MerchantStatus.PENDING,
        tier="BRONZE",
    )
    db.add(new_merchant)

    # 3. Create User
    new_user = User(
        id=uuid.uuid4(),
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=UserRole.MERCHANT,
        merchant_id=new_merchant.id,
        is_active=True,
    )
    db.add(new_user)

    # 4. Initialize Balances for active coins
    coins_result = await db.execute(select(Coin).where(Coin.is_active == True))
    for coin in coins_result.scalars().all():
        db.add(Balance(
            merchant_id=new_merchant.id,
            coin_id=coin.id,
        ))

    # 5. Create Admin Notification
    db.add(AdminNotification(
        type="MERCHANT_REGISTERED",
        message=f"New merchant registered: {payload.name} ({payload.email})",
    ))

    await db.flush()
    logger.info("Merchant registered: name=%s email=%s", payload.name, payload.email)
    
    return {"status": "ok", "message": "Check your email for confirmation (mock)"}

@router.post("/login", response_model=TokenOut)
async def login(payload: LoginRequest, db: DbSession) -> TokenOut:
    """
    Unified login for admins and merchants.
    """
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")

    access_token = create_access_token(
        data={"sub": str(user.id), "role": user.role.value}
    )
    
    return TokenOut(
        access_token=access_token, 
        role=user.role.value,
        merchant_id=user.merchant_id
    )
