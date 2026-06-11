"""
POST /workspace/sync-from-idb
Frontend pushes its IDB files to backend before each chat message,
so the sandbox initializes with the correct (latest) workspace state.

Files are stored in a request-scoped cache (IDB is source of truth).
The cache is read by load_workspace_files() / snapshot_workspace().

Request body:
  {"conversation_id": "...", "files": {"path": "content", ...}}

Response:
  {"success": true, "count": N}
"""

import json
from typing import Any
from .._logger import create_logger

logger = create_logger("sync_from_idb")


async def handler(context: Any) -> dict[str, Any]:
    body = context.request.body or {}
    cid = body.get("conversation_id") or context.conversation_id
    files = body.get("files", {})

    if not cid:
        return {"error": "conversation_id is required"}

    if not isinstance(files, dict):
        return {"error": "files must be a dict"}

    # Store in request-scoped cache (IDB is source of truth)
    from .files import set_workspace_cache, get_active_tool_registry

    set_workspace_cache(cid, files)
    logger.log(f"[sync-from-idb] Cached {len(files)} files for {cid[:8]}")

    # Also sync to sandbox if a session is active (so agent can read them)
    tool_registry = get_active_tool_registry()
    if tool_registry and len(files) > 0:
        try:
            from ..chat.index import sync_workspace_to_sandbox
            await sync_workspace_to_sandbox(tool_registry, files)
            logger.log(f"[sync-from-idb] Synced {len(files)} files to sandbox")
        except Exception as e:
            logger.log(f"[sync-from-idb] Sandbox sync skipped (no active session): {e}")

    return {"success": True, "count": len(files)}
