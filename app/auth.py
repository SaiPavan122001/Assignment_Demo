"""Seeded users, password hashing, JWT, RBAC."""
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.models import User

ROLES = ("student", "evaluator", "admin")
STAFF = ("evaluator", "admin")
DEV_PASSWORD = "password"  # seeded dev users only
SEED_USERS = [
    ("student1@example.com", "student"),
    ("student2@example.com", "student"),
    ("evaluator@example.com", "evaluator"),
    ("admin@example.com", "admin"),
]


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, _ = stored.split("$")
    return hmac.compare_digest(hash_password(password, bytes.fromhex(salt_hex)), stored)


def seed_users(s: Session) -> None:
    for email, role in SEED_USERS:
        if not s.scalar(select(User).where(User.email == email)):
            s.add(User(email=email, role=role, password_hash=hash_password(DEV_PASSWORD)))
    s.flush()


def create_token(user: User, ttl_minutes: int = 480) -> str:
    claims = {
        "sub": user.id, "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(claims, get_settings().jwt_secret, algorithm="HS256")


_bearer = HTTPBearer(auto_error=False)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    s: Session = Depends(get_session),
) -> User:
    if creds is None:
        raise HTTPException(401, "missing bearer token")
    try:
        claims = jwt.decode(creds.credentials, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid or expired token")
    user = s.get(User, claims["sub"])
    if user is None:
        raise HTTPException(401, "unknown user")
    return user


def require_roles(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "insufficient role")
        return user
    return dep
