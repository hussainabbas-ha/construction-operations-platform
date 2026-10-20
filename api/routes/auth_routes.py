"""
Auth routes — /api/auth/signup, /api/auth/login, GET /api/auth/me
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from api.auth import create_access_token, create_user, get_current_user, get_user_by_email, verify_password
from api.database import User, get_db
from api.schemas import LoginRequest, SignupRequest, TokenResponse, UserResponse

router = APIRouter(prefix="/auth", tags=["Auth"])
logger = logging.getLogger(__name__)


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
)
def signup(payload: SignupRequest, db: Session = Depends(get_db)) -> TokenResponse:
    logger.info("Signup attempt for email=%s", payload.email)

    if get_user_by_email(db, payload.email):
        logger.warning("Signup rejected — email already exists: %s", payload.email)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    try:
        user = create_user(
            db,
            email=payload.email,
            full_name=payload.full_name,
            plain_password=payload.password,
        )
    except Exception as exc:
        logger.error("Signup failed for email=%s — %s: %s", payload.email, type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Account creation failed. Please try again or contact support.",
        )

    token = create_access_token(user.id, user.email)
    logger.info("Signup successful for email=%s user_id=%s", user.email, user.id)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_active=user.is_active,
        ),
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login and receive a JWT token",
)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    logger.info("Login attempt for email=%s", payload.email)

    user = get_user_by_email(db, payload.email)

    if not user:
        logger.warning("Login failed — email not found: %s", payload.email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    if not verify_password(payload.password, user.hashed_password):
        logger.warning("Login failed — wrong password for email=%s", payload.email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    if not user.is_active:
        logger.warning("Login failed — account disabled for email=%s", payload.email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled. Contact support.",
        )

    token = create_access_token(user.id, user.email)
    logger.info("Login successful for email=%s user_id=%s", user.email, user.id)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_active=user.is_active,
        ),
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get current logged-in user info",
)
def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    logger.debug("GET /api/auth/me for user_id=%s", current_user.id)
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        is_active=current_user.is_active,
    )
