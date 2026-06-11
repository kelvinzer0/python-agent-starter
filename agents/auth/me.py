"""
Me endpoint -- EdgeOne Makers
===============================

File path agents/auth/me.py maps to **POST /auth/me**

Client-side auth: validates token from Authorization header and
returns user data from request body.
"""

from typing import Any
from ._auth import extract_token


async def handler(context: Any) -> dict[str, Any]:
    token = extract_token(context)
    if not token:
        return {"error": "Not authenticated"}

    body = context.request.body or {}
    user_id = body.get("user_id")
    email = body.get("email")
    username = body.get("username")

    if not user_id or not email:
        return {"error": "Invalid token data"}

    return {
        "success": True,
        "user_id": user_id,
        "email": email,
        "username": username or email.split("@")[0],
    }
