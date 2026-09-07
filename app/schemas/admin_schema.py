from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, ValidationError

__all__ = ["AdminCreateUserSchema", "AdminUpdateUserSchema", "ValidationError"]


class AdminCreateUserSchema(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: Literal["teacher", "student", "admin"]


class AdminUpdateUserSchema(BaseModel):
    role: Optional[Literal["teacher", "student", "admin"]] = None
    is_active: Optional[bool] = None
