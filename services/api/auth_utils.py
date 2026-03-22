from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import jwt
import bcrypt

from core.config import settings

# JWT setup
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours for now

def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt.
    """
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a brypt-hashed password.
    """
    return bcrypt.checkpw(
        plain_password.encode("utf-8"), 
        hashed_password.encode("utf-8")
    )

def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.admin_secret, algorithm=ALGORITHM)
    return encoded_jwt

def decode_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.admin_secret, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return {}
