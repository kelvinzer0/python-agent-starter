"""
Me endpoint -- EdgeOne Makers
===============================

File path agents/auth/me.py maps to **POST /auth/me**

Returns current user info from auth token.
"""

from typing import Any
from ._auth import validate_token, extract_token


async def handler(context: Any) -> dict[str, Any]:
    token = extract_token(context)
    if not token:
        return {"error": "Not authenticated"}

    user = await validate_token(context, token)
    if not user:
        return {"error": "Invalid or expired token"}

    return {"success": True, **user}
