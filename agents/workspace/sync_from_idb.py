"""
POST /workspace/sync-from-idb
Frontend pushes its IDB files to backend before each chat message,
so the sandbox initializes with the correct (latest) workspace state.

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

    # Save to KV so sandbox picks them up on next init
    from .files import save_workspace_files, load_workspace_files

    try:
        current = await load_workspace_files(context, skip_templates=True)
        # Merge: IDB files take precedence
        merged = dict(current)
        merged.update(files)
        await save_workspace_files(context, merged)
        logger.log(f"[sync-from-idb] Merged {len(files)} IDB files into KV for {cid[:8]}")
        return {"success": True, "count": len(files)}
    except Exception as e:
        logger.error(f"[sync-from-idb] Failed: {e}")
        return {"error": str(e)}
