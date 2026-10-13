from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import create_token, verify_password
from app.db import get_session
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/login")
def login(body: LoginIn, s: Session = Depends(get_session)):
    user = s.scalar(select(User).where(User.email == body.email))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "bad credentials")
    return {"access_token": create_token(user), "token_type": "bearer", "role": user.role}
