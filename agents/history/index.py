"""
History API endpoint -- EdgeOne Makers
========================================

File path agents/history/index.py maps to **POST /history**

Returns conversation history for the given conversation ID so the chat
window can be restored after a page refresh.

Uses ``context.conversation_id`` (automatically set by the agents runtime
from the ``makers-conversation-id`` header) and ``context.store`` to
query messages.  Unlike the previous cloud-function implementation, the
agents runtime preserves the original conversation ID — no header
overwrite — so ``store.get_messages()`` always targets the correct
conversation.
"""

import time
import traceback
from typing import Any

from .._logger import create_logger

logger = create_logger("history")

MESSAGE_LIMIT = 100


# --- Message normalization helpers (inline, same logic as old cloud-function) ---


def _attr(item: Any, *keys: str) -> Any:
    """Read an attribute or dict key, trying each name in order until a value is found."""
    if isinstance(item, dict):
        for k in keys:
            if item.get(k) is not None:
                return item[k]
        return None
    for k in keys:
        v = getattr(item, k, None)
        if v is not None:
            return v
    return None


def _content_to_text(content: Any) -> str:
    """Flatten a stored message content (string / dict / list) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "content" in content:
            return _content_to_text(content["content"])
        if "output" in content:
            return _content_to_text(content["output"])
        if "text" in content:
            return str(content["text"] or "")
        return ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("output_text") or "")
            if text:
                parts.append(text)
        return "\n".join(parts)
    return str(content)


def _normalize_message(item: Any) -> dict | None:
    """Normalize a stored message into the frontend response shape; drop unsupported roles."""
    role = _attr(item, "role")
    if role not in ("user", "assistant"):
        return None

    content = _content_to_text(_attr(item, "content"))
    if not content:
        return None

    message_id = _attr(item, "message_id", "messageId")
    created_at = _attr(item, "created_at", "createdAt") or 0
    timestamp = int(created_at) if isinstance(created_at, (int, float)) else 0

    return {
        "id": message_id or f"{role}-{timestamp}",
        "role": role,
        "content": content,
        "timestamp": timestamp,
    }


async def handler(context: Any) -> dict[str, Any]:
    """Return conversation history messages."""
    conversation_id = context.conversation_id
    logger.log(f"[history] conversation_id={conversation_id!r}")

    if not conversation_id:
        return {"conversation_id": "", "messages": []}

    try:
        start = time.time()
        raw_messages = await context.store.get_messages(
            conversation_id=conversation_id,
            limit=MESSAGE_LIMIT,
            order="asc",
        ) or []

        messages = [m for item in raw_messages if (m := _normalize_message(item))]

        elapsed_ms = int((time.time() - start) * 1000)
        logger.log(f"[history] returned {len(messages)} messages in {elapsed_ms}ms")

        return {"conversation_id": conversation_id, "messages": messages}

    except Exception as e:
        logger.error(f"[history] failed: {type(e).__name__}: {e!r}")
        logger.error(f"traceback:\n{traceback.format_exc()}")
        return {"conversation_id": conversation_id, "messages": []}
