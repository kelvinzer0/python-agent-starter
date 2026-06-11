"""
Login endpoint -- EdgeOne Makers
=================================

File path agents/auth/login.py maps to **POST /auth/login**

Authenticates a user and returns an auth token.
"""

from typing import Any
from ._auth import login_user


async def handler(context: Any) -> dict[str, Any]:
    body = context.request.body or {}
    email = body.get("email", "")
    password = body.get("password", "")

    return await login_user(context, email, password)
