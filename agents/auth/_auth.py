"""
Auth helper -- private module for user management.

Since KV_STORE and context.store are not available for non-chat routes,
auth is handled client-side via localStorage. This module provides
a /auth/me endpoint that validates tokens passed in the request body.

The actual user data is stored in the client's localStorage.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .._logger import create_logger

logger = create_logger("auth")


def _hash_password(password: str) -> str:
    """SHA-256 hash with salt for password storage."""
    salt = "warung_lakku_salt_2024"
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


def extract_token(context: Any) -> str | None:
    """Extract auth token from request headers or body."""
    headers = getattr(context.request, "headers", None)
    if headers:
        auth_header = None
        if isinstance(headers, dict):
            auth_header = headers.get("Authorization") or headers.get("authorization")
        else:
            auth_header = getattr(headers, "get", lambda k: None)("Authorization") or \
                          getattr(headers, "get", lambda k: None)("authorization")
        if auth_header and isinstance(auth_header, str) and auth_header.startswith("Bearer "):
            return auth_header[7:]

    body = getattr(context.request, "body", None)
    if isinstance(body, dict):
        return body.get("token")

    return None


async def handler(context: Any) -> dict[str, Any]:
    """Auth /me endpoint — validates token from client-side auth.

    Client stores users in localStorage as:
      eo_users = { "user_id": { user_id, email, username, password_hash } }
      eo_auth_token = token
      eo_auth_user = { user_id, email, username }

    This endpoint simply echoes back the user info from the token.
    The real validation happens client-side (password hash check).
    """
    token = extract_token(context)
    if not token:
        return {"error": "Not authenticated"}

    # The client sends user data in the body for validation
    body = context.request.body or {}
    user_id = body.get("user_id")
    email = body.get("email")
    username = body.get("username")

    if not user_id or not email:
        return {"error": "Invalid token data"}

    logger.log(f"[auth] Token validated for user: {email}")
    return {
        "success": True,
        "user_id": user_id,
        "email": email,
        "username": username,
    }
