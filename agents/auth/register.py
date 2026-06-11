"""
Register endpoint -- EdgeOne Makers
====================================

File path agents/auth/register.py maps to **POST /auth/register**

Client-side auth pass-through: echoes back the user data.
Actual registration is handled client-side via localStorage.
"""

from typing import Any


async def handler(context: Any) -> dict[str, Any]:
    body = context.request.body or {}
    user_id = body.get("user_id")
    email = body.get("email")
    username = body.get("username")
    token = body.get("token")

    if not user_id or not email or not token:
        return {"error": "Missing required fields (user_id, email, token)"}

    return {
        "success": True,
        "user_id": user_id,
        "email": email,
        "username": username or email.split("@")[0],
        "token": token,
    }
