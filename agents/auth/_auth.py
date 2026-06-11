"""
Auth helper -- private module for user management.

Provides register, login, token validation, and user lookup.
Uses context.store (ConversationMemory) for persistence since KV_STORE
is not available in this EdgeOne deployment.

Storage strategy:
  - Users stored as JSON messages in conversation "auth_users"
  - Tokens stored as JSON messages in conversation "auth_tokens"
  - Message format: role=system, content=__AUTH_USER__:{json} or __AUTH_TOKEN__:{json}
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


def _get_store(context: Any):
    """Resolve the storage backend (context.store ConversationMemory)."""
    store = getattr(context, "store", None)
    if store:
        return store
    return None


async def _store_get(store: Any, key: str) -> Any:
    """Get a value from store by scanning messages for the key.
    Uses a sanitized conversation_id (replaces @ with _at_)."""
    safe_key = key.replace("@", "_at_")
    try:
        messages = await store.get_messages(safe_key, limit=1000, order="asc")
        if not messages:
            return None

        for msg in reversed(messages):
            role = getattr(msg, "role", None) if not isinstance(msg, dict) else msg.get("role")
            content = getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
            if role == "system" and isinstance(content, str):
                if content.startswith("__AUTH_USER__:"):
                    return json.loads(content[len("__AUTH_USER__:"):])
                elif content.startswith("__AUTH_TOKEN__:"):
                    return json.loads(content[len("__AUTH_TOKEN__:"):])
        return None
    except Exception as e:
        logger.error(f"[auth] Store get failed for {key}: {e}")
        return None


async def _store_put(store: Any, key: str, value: Any, prefix: str = "__AUTH_DATA__:") -> bool:
    """Store a value by appending a system message.
    Uses a sanitized conversation_id (replaces @ with _at_)."""
    safe_key = key.replace("@", "_at_")
    try:
        content_str = prefix + json.dumps(value)
        res = store.append_message(safe_key, "system", content_str)
        if hasattr(res, "__await__"):
            await res
        return True
    except Exception as e:
        logger.error(f"[auth] Store put failed for {key}: {e}")
        return False


async def register_user(context: Any, email: str, username: str, password: str) -> dict:
    """Register a new user. Returns { success, user_id, token } or { error }."""
    store = _get_store(context)
    if not store:
        return {"error": "Storage unavailable"}

    email = email.strip().lower()
    username = username.strip()

    if not email or not username or not password:
        return {"error": "Email, username, and password are required"}

    if len(password) < 6:
        return {"error": "Password must be at least 6 characters"}

    # Check if email already exists
    existing = await _store_get(store, f"auth_user_{email}")
    if existing:
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
    if not await _store_put(store, f"auth_user_{email}", user_data, "__AUTH_USER__:"):
        return {"error": "Failed to create user"}

    # Generate token
    token = _generate_token()
    token_data = {
        "user_id": user_id,
        "email": email,
        "username": username,
        "created_at": now,
    }
    await _store_put(store, f"auth_token_{token[:16]}", token_data, "__AUTH_TOKEN__:")

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
    store = _get_store(context)
    if not store:
        return {"error": "Storage unavailable"}

    email = email.strip().lower()

    if not email or not password:
        return {"error": "Email and password are required"}

    # Find user by email
    user_data = await _store_get(store, f"auth_user_{email}")
    if not user_data:
        return {"error": "Invalid email or password"}

    # Check password
    if user_data.get("password_hash") != _hash_password(password):
        return {"error": "Invalid email or password"}

    # Generate token
    token = _generate_token()
    token_data = {
        "user_id": user_data.get("user_id"),
        "email": email,
        "username": user_data.get("username", ""),
        "created_at": user_data.get("created_at", 0),
    }
    await _store_put(store, f"auth_token_{token[:16]}", token_data, "__AUTH_TOKEN__:")

    logger.log(f"[auth] User logged in: {email}")
    return {
        "success": True,
        "user_id": user_data.get("user_id"),
        "email": email,
        "username": user_data.get("username", ""),
        "token": token,
    }


async def validate_token(context: Any, token: str) -> dict | None:
    """Validate an auth token. Returns user data or None if invalid."""
    if not token:
        return None

    store = _get_store(context)
    if not store:
        return None

    token_data = await _store_get(store, f"auth_token_{token[:16]}")
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
