from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from core.database import DbSession
from core.models import User, UserRole
from services.api.auth_utils import create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str

@router.post("/login", response_model=TokenOut)
async def login(payload: LoginRequest, db: DbSession) -> TokenOut:
    """
    Authenticate user and return JWT token.
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
    
    return TokenOut(access_token=access_token, role=user.role.value)
