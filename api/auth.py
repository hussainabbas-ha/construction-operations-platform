"""
Auth utilities — password hashing, JWT creation/validation, current-user dependency.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from api.database import User, get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. Add a long random string to your .env file. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def _prepare_password(plain: str) -> str:
    """
    Bcrypt has a hard 72-byte limit. Passwords longer than that are silently
    truncated, which is a security risk. We SHA-256 hash the password first
    so any length password is safely reduced to 64 hex chars before bcrypt.
    This is a well-known pattern (used by Django, etc.).
    """
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def hash_password(plain: str) -> str:
    prepared = _prepare_password(plain)
    logger.debug("Hashing password (sha256 pre-hash applied)")
    return pwd_context.hash(prepared)


def verify_password(plain: str, hashed: str) -> bool:
    prepared = _prepare_password(plain)
    try:
        result = pwd_context.verify(prepared, hashed)
        logger.debug("Password verification result: %s", result)
        return result
    except Exception as exc:
        logger.error("Password verification failed with exception: %s", exc)
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def create_access_token(user_id: str, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": user_id,
        "email": email,
        "exp": expire,
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    logger.info("Access token created for user_id=%s expires=%s", user_id, expire.isoformat())
    return token


# ---------------------------------------------------------------------------
# Current-user dependency
# ---------------------------------------------------------------------------
def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str | None = payload.get("sub")
        if not user_id:
            logger.warning("JWT decoded but missing 'sub' field")
            raise credentials_exception
    except JWTError as exc:
        logger.warning("JWT decode failed: %s", exc)
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        logger.warning("Token valid but user_id=%s not found in DB", user_id)
        raise credentials_exception
    if not user.is_active:
        logger.warning("Token valid but user_id=%s is inactive", user_id)
        raise credentials_exception

    logger.debug("Authenticated user_id=%s email=%s", user.id, user.email)
    return user


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def get_user_by_email(db: Session, email: str) -> User | None:
    normalized = email.lower().strip()
    user = db.query(User).filter(User.email == normalized).first()
    logger.debug("get_user_by_email(%s) → %s", normalized, "found" if user else "not found")
    return user


def create_user(db: Session, email: str, full_name: str, plain_password: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=email.lower().strip(),
        full_name=full_name.strip(),
        hashed_password=hash_password(plain_password),
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    try:
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("New user created: id=%s email=%s", user.id, user.email)
    except Exception as exc:
        db.rollback()
        logger.error("Failed to create user email=%s error=%s", email, exc)
        raise
    return user
