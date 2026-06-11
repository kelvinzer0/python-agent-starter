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

logger = create_logger("workspace_files")

# ── Workspace Persistence & Synchronization ──

async def _load_workspace_raw(context: Any) -> dict[str, Any] | None:
    """Load raw workspace data from store. Returns the stored object (may be
    old-format plain dict or new-format {"version": N, "files": {...}})."""
    store = getattr(context, "store", None)
    if not store:
        return None

    # Resolve conversation_id dynamically from context or request body
    cid = getattr(context, "conversation_id", None)
    if not cid and hasattr(context, "request") and getattr(context.request, "body", None):
        body = context.request.body or {}
        if isinstance(body, dict):
            cid = body.get("conversationId") or body.get("conversation_id")

    if not cid:
        logger.log(f"[DEBUG _load_workspace_raw] Failed to resolve conversation ID! context={context}")
        return None

    kv_key = f"workspace_files_{cid}"
    logger.log(f"[DEBUG _load_workspace_raw] cid={cid}, store_type={type(store)}")
    raw = None

    # Try standard KV first
    try:
        if hasattr(store, "get"):
            res = store.get(kv_key)
            if inspect.isawaitable(res):
                res = await res
            if res and isinstance(res, dict):
                raw = res
                logger.log(f"[DEBUG _load_workspace_raw] Successfully loaded raw dict from KV store")
    except Exception as e:
        logger.log(f"[DEBUG _load_workspace_raw] Failed to get files from KV store: {e}")

    # Fallback: Load from conversation history
    if raw is None:
        try:
            res = store.get_messages(cid, limit=500, order="asc")
            if inspect.isawaitable(res):
                messages = await res
            else:
                messages = res

            if hasattr(store, "to_openai_input"):
                messages = store.to_openai_input(messages)

            for msg in reversed(messages or []):
                if isinstance(msg, dict):
                    role = msg.get("role")
                    content = msg.get("content")
                else:
                    role = getattr(msg, "role", None)
                    content = getattr(msg, "content", None)
                if role == "system" and isinstance(content, str) and content.startswith("__WORKSPACE_FILES_STATE__:"):
                    json_str = content[len("__WORKSPACE_FILES_STATE__:"):]
                    raw = json.loads(json_str)
                    logger.log(f"[workspace] Loaded workspace from conversation history for {cid}")
                    break
        except Exception as e:
            logger.log(f"[workspace] Failed to load workspace from conversation history: {e}")

    return raw


def _unwrap_files(raw: dict[str, Any] | None) -> dict[str, str]:
    """Extract files dict from either old-format (plain dict) or new-format
    {"version": N, "files": {...}} storage."""
    if raw is None:
        return {}
    # New format with version key
    if isinstance(raw, dict) and "files" in raw and isinstance(raw.get("files"), dict):
        return raw["files"]
    # Old format — raw dict is the files dict itself
    if isinstance(raw, dict) and "version" not in raw:
        return {k: v for k, v in raw.items() if isinstance(v, str)}
    # Fallback
    return {}


async def load_workspace_version(context: Any) -> int:
    """Return the current workspace version number (0 if not yet versioned)."""
    raw = await _load_workspace_raw(context)
    if isinstance(raw, dict) and "version" in raw and isinstance(raw["version"], int):
        return raw["version"]
    # Old format or empty — version is 0
    return 0


async def load_workspace_files(context: Any, skip_templates: bool = False) -> dict[str, str]:
    """Load workspace files from store (falls back to message history if KV is empty/unsupported).
    Falls back to project templates if store is empty, unless skip_templates is True.
    """
    raw = await _load_workspace_raw(context)
    files_dict = _unwrap_files(raw)

    if files_dict:
        return files_dict

    # Resolve conversation_id dynamically for logging
    cid = getattr(context, "conversation_id", None)
    if not cid and hasattr(context, "request") and getattr(context.request, "body", None):
        body = context.request.body or {}
        if isinstance(body, dict):
            cid = body.get("conversationId") or body.get("conversation_id")

    # If both failed/empty, load templates (unless skipped)
    if skip_templates:
        logger.log(f"[workspace] No files in store for {cid}. Skipping templates (skip_templates=True).")
        return {}
    logger.log(f"[workspace] No files in store for {cid}. Loading default templates.")
    files_dict = {}

    project_root = Path(__file__).resolve().parent.parent.parent
    workspace_dir = project_root / "workspace"
    if not workspace_dir.exists():
        workspace_dir = Path(__file__).resolve().parent.parent / "workspace"
    if not workspace_dir.exists():
        workspace_dir = Path.cwd() / "workspace"

    if workspace_dir.exists() and workspace_dir.is_dir():
        for filepath in workspace_dir.glob("**/*"):
            if filepath.is_file():
                rel_path = str(filepath.relative_to(workspace_dir))
                # Skip python files, pycache, and system hidden files/folders
                if rel_path.endswith(".py") or rel_path.startswith("__") or "pycache" in rel_path or "/__" in rel_path or rel_path.startswith("."):
                    continue
                try:
                    content = filepath.read_text(encoding="utf-8").strip()
                    files_dict[rel_path] = content
                except Exception:
                    pass
    return files_dict


async def save_workspace_files(context: Any, files_dict: dict[str, str]) -> None:
    """Save workspace files to store (falls back to message history if KV is not supported).
    Stores {"version": N, "files": {...}} with an incrementing version number.
    """
    store = getattr(context, "store", None)
    if not store:
        return

    # Resolve conversation_id dynamically from context or request body
    cid = getattr(context, "conversation_id", None)
    if not cid and hasattr(context, "request") and getattr(context.request, "body", None):
        body = context.request.body or {}
        if isinstance(body, dict):
            cid = body.get("conversationId") or body.get("conversation_id")

    if not cid:
        logger.log(f"[DEBUG save_workspace_files] Failed to resolve conversation ID! context={context}")
        return

    kv_key = f"workspace_files_{cid}"
    logger.log(f"[DEBUG save_workspace_files] cid={cid}, store_type={type(store)}")

    # Determine current version before saving
    current_version = await load_workspace_version(context)
    new_version = current_version + 1
    wrapped = {"version": new_version, "files": files_dict}

    # Try standard KV first (in case it gets supported or in other environments)
    try:
        if hasattr(store, "put"):
            res = store.put(kv_key, wrapped)
            if inspect.isawaitable(res):
                await res
            logger.log(f"[workspace] Saved workspace v{new_version} to KV store for {cid}")
            return
        elif hasattr(store, "set"):
            res = store.set(kv_key, wrapped)
            if inspect.isawaitable(res):
                await res
            logger.log(f"[workspace] Saved workspace v{new_version} to KV store for {cid}")
            return
    except Exception as e:
        logger.log(f"[DEBUG save_workspace_files] KV store save failed, falling back to messages: {e}")

    # Fallback: Save as a system message in conversation history
    try:
        content_str = "__WORKSPACE_FILES_STATE__:" + json.dumps(wrapped)
        res = store.append_message(cid, "system", content_str)
        if inspect.isawaitable(res):
            await res
        logger.log(f"[workspace] Saved workspace v{new_version} to conversation history for {cid}")
    except Exception as e:
        logger.log(f"[workspace] Failed to save workspace to conversation history: {e}")


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

    # Determine if this is a mutation action that should skip template fallback
    is_mutation = action in ("write", "delete", "sync")

    # Load the current workspace dict (falls back to templates if not in store yet)
    files_dict = await load_workspace_files(context, skip_templates=is_mutation)

    # Filter out python files, system files, and __pycache__ from the workspace view
    files_dict = {
        k: v for k, v in files_dict.items()
        if not (k.endswith(".py") or k.startswith("__") or "pycache" in k or k == "files.py" or k.startswith("."))
    }

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

        from ..chat.index import sandbox_write_file, run_sandbox_command_system

        action_type = body.get("action_type")
        if action_type == "delete":
            try:
                await run_sandbox_command_system(tool_registry, f"rm -f /workspace/{shlex.quote(filename)}")
                # Also update KV: load files, remove entry, save
                current_files = await load_workspace_files(context, skip_templates=True)
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
                current_files = await load_workspace_files(context, skip_templates=True)
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
            
            # Sync to sandbox if session is active
            tool_registry = _active_tool_registry
            if tool_registry:
                try:
                    from ..chat.index import sandbox_write_file, run_sandbox_command_system
                    await sandbox_write_file(tool_registry, f"/workspace/{filename}", content)
                    new_version = await load_workspace_version(context)
                    await sandbox_write_file(tool_registry, "/tmp/.workspace_version", str(new_version))
                    logger.log(f"[workspace_files] Automatically synced write {filename} to sandbox")
                except Exception as ex:
                    logger.error(f"[workspace_files] Failed to sync write to sandbox: {ex}")
                    
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
            
            # Sync delete to sandbox if session is active
            tool_registry = _active_tool_registry
            if tool_registry:
                try:
                    from ..chat.index import run_sandbox_command_system, sandbox_write_file
                    await run_sandbox_command_system(tool_registry, f"rm -f /workspace/{shlex.quote(filename)}")
                    new_version = await load_workspace_version(context)
                    await sandbox_write_file(tool_registry, "/tmp/.workspace_version", str(new_version))
                    logger.log(f"[workspace_files] Automatically synced delete {filename} to sandbox")
                except Exception as ex:
                    logger.error(f"[workspace_files] Failed to sync delete to sandbox: {ex}")
                    
            return {"success": True}
        except Exception as e:
            logger.error(f"[workspace_files] Failed to delete file: {e}")
            return {"error": f"Failed to delete file: {str(e)}"}

    else:
        return {"error": f"Invalid action: {action}"}
