"""
Register endpoint -- EdgeOne Makers
====================================

File path agents/auth/register.py maps to **POST /auth/register**

Client-side auth: user data is stored in localStorage.
This endpoint is a pass-through that echoes back the registration data.
"""

from typing import Any


async def handler(context: Any) -> dict[str, Any]:
    """Register pass-through — client handles actual auth via localStorage."""
    body = context.request.body or {}
    user_id = body.get("user_id")
    email = body.get("email")
    username = body.get("username")
    token = body.get("token")

    if not all([user_id, email, token]):
        return {"error": "Missing registration data"}

    return {
        "success": True,
        "user_id": user_id,
        "email": email,
        "username": username or email.split("@")[0],
        "token": token,
    }
