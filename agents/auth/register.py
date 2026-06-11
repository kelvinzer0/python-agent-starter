"""
Register endpoint -- EdgeOne Makers
====================================

File path agents/auth/register.py maps to **POST /auth/register**

Creates a new user account and returns an auth token.
"""

from typing import Any
from ._auth import register_user


async def handler(context: Any) -> dict[str, Any]:
    body = context.request.body or {}
    email = body.get("email", "")
    username = body.get("username", "")
    password = body.get("password", "")

    return await register_user(context, email, username, password)
