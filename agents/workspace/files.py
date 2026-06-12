"""
Workspace files API endpoint -- EdgeOne Makers
=======================================

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

# Default shared workspace key (used when no user is authenticated)
SHARED_WORKSPACE_KEY = "workspace_files"


def _workspace_key(user_id: str | None) -> str:
    """Build the KV key for workspace files."""
    if user_id:
        return f"workspace_files_{user_id}"
    return SHARED_WORKSPACE_KEY


async def _resolve_user_id(context: Any) -> str | None:
    """Resolve user_id from auth token in request headers."""
    headers = getattr(context.request, "headers", None)
    if headers:
        auth_header = None
        if isinstance(headers, dict):
            auth_header = headers.get("Authorization") or headers.get("authorization")
        else:
            auth_header = getattr(headers, "get", lambda k: None)("Authorization") or \
                          getattr(headers, "get", lambda k: None)("authorization")
        if auth_header and isinstance(auth_header, str) and auth_header.startswith("Bearer "):
            # For client-side auth, we just check if token exists
            # Real user_id is in the request body or managed client-side
            return None
    return None


def _get_kv_store(context: Any):
    """Resolve the EdgeOne KV binding from context.env."""
    env = getattr(context, "env", None)
    if not env:
        return None
    kv = getattr(env, "KV_STORE", None)
    if kv and (hasattr(kv, "get") or hasattr(kv, "put")):
        return kv
    return None


def _workspace_key(user_id: str | None) -> str:
    """Build the KV key for workspace files."""
    if user_id:
        return f"workspace_files_{user_id}"
    return SHARED_WORKSPACE_KEY


async def _load_workspace_raw(context: Any) -> dict[str, Any] | None:
    """Load raw workspace data from store. Returns the stored object (may be
    old-format plain dict or new-format {"version": N, "files": {...}}).

    Resolution order:
      1. User-scoped key (workspace_files_{user_id}) — per-user persistence
      2. Shared key (workspace_files) — backward compat
      3. Legacy per-conversation key (workspace_files_{cid}) — migration
      4. Conversation history fallback
    """

    # Resolve user_id from auth token
    user_id = await _resolve_user_id(context)

    # Resolve conversation_id for legacy fallback
    cid = getattr(context, "conversation_id", None)
    if not cid and hasattr(context, "request") and getattr(context.request, "body", None):
        body = context.request.body or {}
        if isinstance(body, dict):
            cid = body.get("conversationId") or body.get("conversation_id")

    kv_store = _get_kv_store(context)
    if not kv_store:
        # No KV store — fall back to conversation history
        return await _load_from_history(context, cid)

    # --- 1. Try user-scoped key ---
    raw = None
    if user_id:
        user_key = _workspace_key(user_id)
        raw = await _kv_get(kv_store, user_key)
        if raw:
            logger.log(f"[workspace] Loaded from user key '{user_key}'")
            return raw

    # --- 2. Fallback: shared key ---
    raw = await _kv_get(kv_store, SHARED_WORKSPACE_KEY)
    if raw:
        logger.log(f"[workspace] Loaded from shared key '{SHARED_WORKSPACE_KEY}'")
        # If user is authenticated, migrate to user-scoped key
        if user_id:
            await _save_raw_to_kv(kv_store, _workspace_key(user_id), raw)
        return raw

    # --- 3. Fallback: legacy per-conversation key ---
    if cid:
        legacy_key = f"workspace_files_{cid}"
        raw = await _kv_get(kv_store, legacy_key)
        if raw:
            logger.log(f"[workspace] Migrating from legacy key '{legacy_key}'")
            target_key = _workspace_key(user_id) if user_id else SHARED_WORKSPACE_KEY
            await _save_raw_to_kv(kv_store, target_key, raw)
            return raw

    # --- 4. Fallback: conversation history ---
    return await _load_from_history(context, cid)


async def _load_from_history(context: Any, cid: str | None) -> dict[str, Any] | None:
    """Load workspace data from conversation history fallback."""
    if not cid:
        return None

    store = getattr(context, "store", None)
    if not store:
        return None

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
                return json.loads(json_str)
    except Exception as e:
        logger.log(f"[workspace] Failed to load from history: {e}")

    return None


async def _kv_get(kv_store: Any, key: str) -> dict[str, Any] | None:
    """Get a value from KV store."""
    try:
        if hasattr(kv_store, "get"):
            res = kv_store.get(key)
            if inspect.isawaitable(res):
                res = await res
            if res is not None:
                if isinstance(res, str):
                    return json.loads(res)
                elif isinstance(res, dict):
                    return res
    except Exception:
        pass
    return None


async def _save_raw_to_kv(kv_store: Any, key: str, data: dict[str, Any]) -> None:
    """Save raw dict to a KV store (EdgeOne or context.store)."""
    try:
        if hasattr(kv_store, "put"):
            res = kv_store.put(key, json.dumps(data) if not isinstance(data, str) else data)
            if inspect.isawaitable(res):
                await res
    except Exception as e:
        logger.log(f"[workspace] Failed to save to KV key '{key}': {e}")


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
    When templates are loaded, they are auto-saved to KV for persistence.

    Priority: IDB cache (from sync-from-idb) → KV → templates.
    """
    # 1. Check IDB cache first (source of truth when KV is broken)
    cid = getattr(context, "conversation_id", None)
    if not cid and hasattr(context, "request") and getattr(context.request, "body", None):
        body = context.request.body or {}
        if isinstance(body, dict):
            cid = body.get("conversationId") or body.get("conversation_id")

    if cid:
        cached = get_workspace_cache(cid)
        if cached is not None:
            logger.log(f"[workspace] Loaded {len(cached)} files from IDB cache for {cid[:8]}")
            return cached

    # 2. Fall back to KV / history
    raw = await _load_workspace_raw(context)
    files_dict = _unwrap_files(raw)

    if files_dict:
        return files_dict

    # If both failed/empty, load templates (unless skipped)
    if skip_templates:
        logger.log(f"[workspace] No files in store for {cid}. Skipping templates (skip_templates=True).")
        return {}
    logger.log(f"[workspace] No files in store for {cid}. Loading default templates from filesystem.")
    files_dict = _load_templates_from_filesystem()

    # Auto-save templates to KV so they persist across conversations/reloads
    if files_dict:
        try:
            await save_workspace_files(context, files_dict)
            logger.log(f"[workspace] Auto-saved {len(files_dict)} template files to KV")
        except Exception as e:
            logger.log(f"[workspace] Failed to auto-save templates to KV: {e}")

    return files_dict


def _load_templates_from_filesystem() -> dict[str, str]:
    """Load default workspace template files from the project's workspace/ directory."""
    files_dict: dict[str, str] = {}

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
    Saves to user-scoped key if authenticated, otherwise shared key.
    """

    # Resolve user_id for scoped key
    user_id = await _resolve_user_id(context)
    kv_key = _workspace_key(user_id)
    logger.log(f"[workspace] Saving to key '{kv_key}' (user_id={user_id})")

    # Determine current version before saving
    current_version = await load_workspace_version(context)
    new_version = current_version + 1
    wrapped = {"version": new_version, "files": files_dict}

    # Always update request-scoped cache (source of truth when KV is broken)
    cid = getattr(context, "conversation_id", None)
    if cid:
        set_workspace_cache(cid, files_dict)

    # Try EdgeOne KV store first (context.env.KV_STORE)
    kv_store = _get_kv_store(context)
    if kv_store:
        try:
            if hasattr(kv_store, "put"):
                res = kv_store.put(kv_key, json.dumps(wrapped))
                if inspect.isawaitable(res):
                    await res
                logger.log(f"[workspace] Saved workspace v{new_version} to '{kv_key}'")
                return
        except Exception as e:
            logger.log(f"[workspace] EdgeOne KV store save failed: {e}")

    # Fallback: Try context.store (ConversationMemory) for backward compatibility
    store = getattr(context, "store", None)
    if not store:
        return

    # Try standard KV on context.store first
    try:
        if hasattr(store, "put"):
            res = store.put(kv_key, wrapped)
            if inspect.isawaitable(res):
                await res
            logger.log(f"[workspace] Saved workspace v{new_version} to context.store key '{kv_key}'")
            return
        elif hasattr(store, "set"):
            res = store.set(kv_key, wrapped)
            if inspect.isawaitable(res):
                await res
            logger.log(f"[workspace] Saved workspace v{new_version} to context.store key '{kv_key}'")
            return
    except Exception as e:
        logger.log(f"[workspace] KV store save failed: {e}")

    # Fallback: Save as a system message in conversation history
    cid = getattr(context, "conversation_id", None)
    if not cid:
        return
    try:
        content_str = "__WORKSPACE_FILES_STATE__:" + json.dumps(wrapped)
        res = store.append_message(cid, "system", content_str)
        if inspect.isawaitable(res):
            await res
        logger.log(f"[workspace] Saved workspace v{new_version} to conversation history for {cid}")
    except Exception as e:
        logger.log(f"[workspace] Failed to save workspace to conversation history: {e}")


# ── Request-scoped workspace cache (IDB is source of truth) ──
# When sync-from-idb pushes frontend files, they land here.
# load_workspace_files reads from this cache before falling back to templates.
_workspace_cache: dict[str, dict[str, str]] = {}


def set_workspace_cache(conversation_id: str, files: dict[str, str]) -> None:
    """Store workspace files in request-scoped cache (from frontend IDB)."""
    _workspace_cache[conversation_id] = files


def get_workspace_cache(conversation_id: str) -> dict[str, str] | None:
    """Get cached workspace files for a conversation (from frontend IDB)."""
    return _workspace_cache.get(conversation_id)


# ── Active tool registry reference for sandbox sync ──

_active_tool_registry = None


def set_active_tool_registry(registry):
    global _active_tool_registry
    _active_tool_registry = registry


def clear_active_tool_registry():
    global _active_tool_registry
    _active_tool_registry = None


def get_active_tool_registry():
    """Get the active tool registry (for external callers like sync-from-idb)."""
    return _active_tool_registry


async def snapshot_workspace(context: Any) -> dict[str, str]:
    """Read-only snapshot of the workspace file map.

    Unlike ``load_workspace_files``, this skips template fallback and
    version-bookkeeping side-effects — it simply returns whatever is
    currently stored (or an empty dict if nothing is stored yet).

    Priority: IDB cache → KV → empty.
    """
    # Check IDB cache first
    cid = getattr(context, "conversation_id", None)
    if not cid and hasattr(context, "request") and getattr(context.request, "body", None):
        body = context.request.body or {}
        if isinstance(body, dict):
            cid = body.get("conversationId") or body.get("conversation_id")
    if cid:
        cached = get_workspace_cache(cid)
        if cached is not None:
            return cached

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
                    from ..chat.index import sandbox_write_file
                    await sandbox_write_file(tool_registry, f"/workspace/{filename}", content)
                    # Also update cache so next request has latest state
                    cid = getattr(context, "conversation_id", None)
                    if cid:
                        set_workspace_cache(cid, files_dict)
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
                    from ..chat.index import run_sandbox_command_system
                    await run_sandbox_command_system(tool_registry, f"rm -f /workspace/{shlex.quote(filename)}")
                    # Also update cache so next request has latest state
                    cid = getattr(context, "conversation_id", None)
                    if cid:
                        set_workspace_cache(cid, files_dict)
                    logger.log(f"[workspace_files] Automatically synced delete {filename} to sandbox")
                except Exception as ex:
                    logger.error(f"[workspace_files] Failed to sync delete to sandbox: {ex}")
                    
            return {"success": True}
        except Exception as e:
            logger.error(f"[workspace_files] Failed to delete file: {e}")
            return {"error": f"Failed to delete file: {str(e)}"}

    else:
        return {"error": f"Invalid action: {action}"}
