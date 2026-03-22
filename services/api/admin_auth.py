import logging
import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select

from core.database import DbSession
from core.models import User, UserRole, Merchant
from services.api.auth_utils import decode_token

logger = logging.getLogger(__name__)

security = HTTPBearer()

async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: DbSession,
) -> User:
    """
    FastAPI dependency to get the current authenticated user from JWT.
    """
    token = credentials.credentials
    payload = decode_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid user ID")

    result = await db.execute(select(User).where(User.id == user_uuid))
    user = result.scalar_one_or_none()
    
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is inactive")
    
    return user


async def require_admin(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """
    Dependency to ensure the current user is an ADMIN.
    """
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return current_user


async def require_merchant(
    current_user: Annotated[User, Depends(get_current_user)],
    db: DbSession,
) -> Merchant:
    """
    Dependency to ensure the current user is a MERCHANT and return the Merchant object.
    """
    if current_user.role != UserRole.MERCHANT or not current_user.merchant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Merchant privileges required",
        )
    
    # We fetch the merchant object to ensure it's in the current DB session
    result = await db.execute(select(Merchant).where(Merchant.id == current_user.merchant_id))
    merchant = result.scalar_one_or_none()
    
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant profile not found")
        
    return merchant