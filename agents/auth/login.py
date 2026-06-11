"""
Login endpoint -- EdgeOne Makers
=================================

File path agents/auth/login.py maps to **POST /auth/login**

Client-side auth: validates credentials against client-stored data.
This endpoint is a no-op pass-through — actual auth is done client-side.
"""

from typing import Any


async def handler(context: Any) -> dict[str, Any]:
    """Login pass-through — client handles actual auth via localStorage."""
    body = context.request.body or {}
    user_id = body.get("user_id")
    email = body.get("email")
    username = body.get("username")
    token = body.get("token")

    if not all([user_id, email, token]):
        return {"error": "Missing auth data"}

    return {
        "success": True,
        "user_id": user_id,
        "email": email,
        "username": username or email.split("@")[0],
        "token": token,
    }
