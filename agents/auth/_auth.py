"""
Auth helper -- private module for user management.

Provides register, login, token validation, and user lookup.
Uses EdgeOne KV store for persistence.

KV keys:
  user_{user_id}          → { user_id, email, username, password_hash, created_at }
  token_{token}           → { user_id, email, username, created_at }
  user_by_email_{email}   → user_id (for email lookup)
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

from .._logger import create_logger

logger = create_logger("auth")


def _hash_password(password: str) -> str:
    """SHA-256 hash with salt for password storage."""
    salt = "warung_lakku_salt_2024"
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


def _generate_token() -> str:
    """Generate a random auth token."""
    return secrets.token_hex(32)


def _get_kv(context: Any):
    """Resolve the EdgeOne KV binding."""
    env = getattr(context, "env", None)
    if not env:
        return None
    kv = getattr(env, "KV_STORE", None)
    if kv and (hasattr(kv, "get") or hasattr(kv, "put")):
        return kv
    return None


async def _kv_get(kv: Any, key: str) -> Any:
    """Get a value from KV store."""
    try:
        res = kv.get(key)
        if hasattr(res, "__await__"):
            res = await res
        if res is None:
            return None
        if isinstance(res, str):
            return json.loads(res)
        return res
    except Exception:
        return None


async def _kv_put(kv: Any, key: str, value: Any) -> bool:
    """Put a value into KV store."""
    try:
        data = json.dumps(value) if isinstance(value, dict) else value
        res = kv.put(key, data)
        if hasattr(res, "__await__"):
            await res
        return True
    except Exception as e:
        logger.error(f"[auth] KV put failed for {key}: {e}")
        return False


async def register_user(context: Any, email: str, username: str, password: str) -> dict:
    """Register a new user. Returns { success, user_id, token } or { error }."""
    kv = _get_kv(context)
    if not kv:
        return {"error": "Storage unavailable"}

    email = email.strip().lower()
    username = username.strip()

    if not email or not username or not password:
        return {"error": "Email, username, and password are required"}

    if len(password) < 6:
        return {"error": "Password must be at least 6 characters"}

    # Check if email already exists
    existing_id = await _kv_get(kv, f"user_by_email_{email}")
    if existing_id:
        return {"error": "Email already registered"}

    # Create user
    user_id = secrets.token_hex(16)
    now = int(time.time() * 1000)
    user_data = {
        "user_id": user_id,
        "email": email,
        "username": username,
        "password_hash": _hash_password(password),
        "created_at": now,
    }

    # Save user data
    if not await _kv_put(kv, f"user_{user_id}", user_data):
        return {"error": "Failed to create user"}

    # Save email → user_id mapping
    await _kv_put(kv, f"user_by_email_{email}", user_id)

    # Generate token
    token = _generate_token()
    token_data = {
        "user_id": user_id,
        "email": email,
        "username": username,
        "created_at": now,
    }
    await _kv_put(kv, f"token_{token}", token_data)

    logger.log(f"[auth] User registered: {email} (id={user_id})")
    return {
        "success": True,
        "user_id": user_id,
        "email": email,
        "username": username,
        "token": token,
    }


async def login_user(context: Any, email: str, password: str) -> dict:
    """Login an existing user. Returns { success, user_id, token } or { error }."""
    kv = _get_kv(context)
    if not kv:
        return {"error": "Storage unavailable"}

    email = email.strip().lower()

    if not email or not password:
        return {"error": "Email and password are required"}

    # Find user by email
    user_id = await _kv_get(kv, f"user_by_email_{email}")
    if not user_id:
        return {"error": "Invalid email or password"}

    user_data = await _kv_get(kv, f"user_{user_id}")
    if not user_data:
        return {"error": "Invalid email or password"}

    # Check password
    if user_data.get("password_hash") != _hash_password(password):
        return {"error": "Invalid email or password"}

    # Generate token
    token = _generate_token()
    token_data = {
        "user_id": user_id,
        "email": email,
        "username": user_data.get("username", ""),
        "created_at": user_data.get("created_at", 0),
    }
    await _kv_put(kv, f"token_{token}", token_data)

    logger.log(f"[auth] User logged in: {email}")
    return {
        "success": True,
        "user_id": user_id,
        "email": email,
        "username": user_data.get("username", ""),
        "token": token,
    }


async def validate_token(context: Any, token: str) -> dict | None:
    """Validate an auth token. Returns user data or None if invalid."""
    if not token:
        return None

    kv = _get_kv(context)
    if not kv:
        return None

    token_data = await _kv_get(kv, f"token_{token}")
    if not token_data or not isinstance(token_data, dict):
        return None

    return {
        "user_id": token_data.get("user_id"),
        "email": token_data.get("email"),
        "username": token_data.get("username"),
    }


def extract_token(context: Any) -> str | None:
    """Extract auth token from request headers or body."""
    # Try Authorization header first
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

    # Try request body
    body = getattr(context.request, "body", None)
    if isinstance(body, dict):
        return body.get("token")

    return None
