"""
Workspace files API endpoint -- EdgeOne Makers
========================================

File path agents/workspace/files.py maps to **POST /workspace/files**
"""

import json
import shlex
from typing import Any
from pathlib import Path
import inspect

from .._logger import create_logger
from ..chat.index import load_workspace_files, save_workspace_files, load_workspace_version, _load_workspace_raw, _unwrap_files

logger = create_logger("workspace_files")

# ── Active tool registry reference for sandbox sync ──

_active_tool_registry = None


def set_active_tool_registry(registry):
    global _active_tool_registry
    _active_tool_registry = registry


def clear_active_tool_registry():
    global _active_tool_registry
    _active_tool_registry = None


async def snapshot_workspace(context: Any) -> dict[str, str]:
    """Read-only snapshot of the workspace file map from KV.

    Unlike ``load_workspace_files``, this skips template fallback and
    version-bookkeeping side-effects — it simply returns whatever is
    currently stored (or an empty dict if nothing is stored yet).
    """
    raw = await _load_workspace_raw(context)
    return _unwrap_files(raw)


async def handler(context: Any) -> dict[str, Any]:
    body = context.request.body or {}
    cid = body.get("conversationId") or body.get("conversation_id") or context.conversation_id
    logger.log(f"[workspace_files] conversation_id: {cid}")

    if not cid:
        return {"error": "conversation_id is required"}
    action = body.get("action")
    filename = body.get("filename")
    content = body.get("content")

    # We reuse load_workspace_files from agents.chat.index to get the current workspace dict
    # (which falls back to templates if not in store yet)
    files_dict = await load_workspace_files(context)

    if action == "list":
        # Return list of files
        files_list = [{"name": k, "size": len(v)} for k, v in files_dict.items()]
        return {"files": files_list}

    elif action == "read":
        if not filename:
            return {"error": "filename is required for read action"}
        file_content = files_dict.get(filename, "")
        return {"content": file_content}

    elif action == "status":
        files_list = [{"name": k, "size": len(v)} for k, v in files_dict.items()]
        version = await load_workspace_version(context)
        return {"files": files_list, "version": version}

    elif action == "sync":
        if not filename:
            return {"error": "filename is required for sync action"}
        tool_registry = _active_tool_registry
        if not tool_registry:
            return {"error": "No active sandbox session for sync"}

        from ..chat.index import sandbox_write_file, run_sandbox_command

        action_type = body.get("action_type")
        if action_type == "delete":
            try:
                await run_sandbox_command(tool_registry, f"rm -f /workspace/{shlex.quote(filename)}")
                # Also update KV: load files, remove entry, save
                current_files = await load_workspace_files(context)
                if filename in current_files:
                    del current_files[filename]
                    await save_workspace_files(context, current_files)
                logger.log(f"[workspace_files] Synced delete {filename} to sandbox")
                return {"success": True}
            except Exception as e:
                logger.error(f"[workspace_files] Failed to sync delete: {e}")
                return {"error": f"Failed to sync delete: {str(e)}"}
        else:
            # Write/sync content to sandbox
            if content is None:
                return {"error": "content is required for sync write action"}
            try:
                await sandbox_write_file(tool_registry, f"/workspace/{filename}", content)
                # Also update KV: load files, update entry, save
                current_files = await load_workspace_files(context)
                current_files[filename] = content
                await save_workspace_files(context, current_files)
                logger.log(f"[workspace_files] Synced write {filename} to sandbox")
                return {"success": True}
            except Exception as e:
                logger.error(f"[workspace_files] Failed to sync write: {e}")
                return {"error": f"Failed to sync write: {str(e)}"}

    elif action == "write":
        if not filename:
            return {"error": "filename is required for write action"}
        if content is None:
            return {"error": "content is required for write action"}

        # Version conflict check
        expected_version = body.get("expectedVersion")
        if expected_version is not None:
            current_version = await load_workspace_version(context)
            if current_version != expected_version:
                return {"error": "version_conflict", "currentVersion": current_version}

        files_dict[filename] = content

        # Save back to store
        try:
            await save_workspace_files(context, files_dict)
            logger.log(f"[workspace_files] Saved updated file {filename} to store")
            return {"success": True}
        except Exception as e:
            logger.error(f"[workspace_files] Failed to write file: {e}")
            return {"error": f"Failed to save file: {str(e)}"}

    elif action == "delete":
        if not filename:
            return {"error": "filename is required for delete action"}

        # Version conflict check
        expected_version = body.get("expectedVersion")
        if expected_version is not None:
            current_version = await load_workspace_version(context)
            if current_version != expected_version:
                return {"error": "version_conflict", "currentVersion": current_version}

        if filename in files_dict:
            del files_dict[filename]

        # Save back to store
        try:
            await save_workspace_files(context, files_dict)
            logger.log(f"[workspace_files] Deleted file {filename} from store")
            return {"success": True}
        except Exception as e:
            logger.error(f"[workspace_files] Failed to delete file: {e}")
            return {"error": f"Failed to delete file: {str(e)}"}

    else:
        return {"error": f"Invalid action: {action}"}
