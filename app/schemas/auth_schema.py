from typing import Literal

from pydantic import BaseModel, EmailStr, Field, ValidationError

__all__ = ["RegisterSchema", "LoginSchema", "ValidationError"]


class RegisterSchema(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: Literal["teacher", "student"]


class LoginSchema(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)
