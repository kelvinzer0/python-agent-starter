"""
Chat handler -- EdgeOne Makers
========================================

File path agents/chat/index.py maps to **POST /chat**

Uses raw httpx streaming to call the LLM API (OpenAI-compatible chat/completions).
Supports tool calling with EdgeOne platform tools (commands, files, code_interpreter, browser).

Tool calling flow:
  1. Send messages + tools to LLM
  2. LLM returns tool_calls -> execute via EdgeOne sandbox
  3. Send tool results back to LLM
  4. Repeat until LLM gives final text response

context convention:
    context.request.body    -- dict, request body
    context.request.signal  -- asyncio.Event, set when /chat/stop is called
    context.conversation_id -- conversation ID
    context.run_id          -- current run ID
    context.tracer          -- manual instrumentation API (no-op fallback if unavailable)
"""

from typing import Any, AsyncGenerator
import asyncio
import time
from pathlib import Path
import inspect
import json
import zipfile
import io
import re
import os

import httpx

from .._model import MODEL_CONFIG, ssl_verify
from .._logger import create_logger
from .._session import ChatSession
from .._tools import build_tools, ToolRegistry, _stringify_result
from ._stream import LlmRoundResult, sse_event, stream_llm_round, safe_json_preview
from ._images import extract_images_from_tool_result
from ..workspace.files import snapshot_workspace


logger = create_logger("chat")


# ── Sandbox File Utilities for Workspace Persistence ──

def find_fs_tool_name(tool_registry: ToolRegistry, operation: str) -> str | None:
    """Dynamically look for the correct files/fs tool name in the registered tools."""
    for name in tool_registry._handlers.keys():
        name_lower = name.lower()
        if "file" in name_lower or "fs" in name_lower:
            if operation == "write" and "write" in name_lower:
                return name
            if operation == "read" and "read" in name_lower:
                return name
            if operation == "list" and "list" in name_lower:
                return name
            if operation == "remove" and ("remove" in name_lower or "delete" in name_lower or "rm" in name_lower):
                return name
    # Fallback to standard platform names
    fallbacks = {
        "write": ["files_write", "files.write", "write_file"],
        "read": ["files_read", "files.read", "read_file"],
        "list": ["files_list", "files.list", "list_dir"],
        "remove": ["files_remove", "files.remove", "remove_file", "delete_file"]
    }
    for fb in fallbacks.get(operation, []):
        if fb in tool_registry._handlers:
            return fb
    return None


def get_tool_param_keys(tool_registry: ToolRegistry, tool_name: str) -> list[str]:
    """Retrieve parameter keys for the specified tool to map argument keys correctly."""
    for t in tool_registry.tools:
        if t["function"]["name"] == tool_name:
            properties = t["function"]["parameters"].get("properties", {})
            return list(properties.keys())
    return []


async def sandbox_write_file(tool_registry: ToolRegistry, path: str, content: str) -> bool:
    """Directly execute the sandbox write tool to write a file in the sandbox."""
    tool_name = find_fs_tool_name(tool_registry, "write")
    if not tool_name:
        return False
    
    params = get_tool_param_keys(tool_registry, tool_name)
    args = {}
    
    path_key = "path"
    if "filepath" in params:
        path_key = "filepath"
        
    content_key = "content"
    if "data" in params:
        content_key = "data"
    elif "text" in params:
        content_key = "text"
        
    args[path_key] = path
    args[content_key] = content
    
    res = await tool_registry.execute_raw(tool_name, json.dumps(args))
    if isinstance(res, dict) and "error" in res:
        return False
    return True


async def sandbox_read_file(tool_registry: ToolRegistry, path: str) -> str | None:
    """Directly execute the sandbox read tool to read a file from the sandbox."""
    tool_name = find_fs_tool_name(tool_registry, "read")
    if not tool_name:
        return None
        
    params = get_tool_param_keys(tool_registry, tool_name)
    path_key = "path"
    if "filepath" in params:
        path_key = "filepath"
        
    args = {path_key: path}
    res = await tool_registry.execute_raw(tool_name, json.dumps(args))
    if isinstance(res, dict) and "error" in res:
        return None
    if isinstance(res, str):
        return res
    if isinstance(res, dict) and "content" in res:
        return str(res["content"])
    return str(res)


def find_command_tool_name(tool_registry: ToolRegistry) -> str | None:
    """Dynamically look for the correct command/exec tool in the registered tools."""
    for name in tool_registry._handlers.keys():
        name_lower = name.lower()
        if "command" in name_lower or "exec" in name_lower or "run" in name_lower:
            if "file" not in name_lower and "fs" not in name_lower:
                return name
    # Fallback standard name
    if "commands_run" in tool_registry._handlers:
        return "commands_run"
    return None


async def run_sandbox_command(tool_registry: ToolRegistry, command: str) -> str | None:
    """Directly execute a shell command in the sandbox container."""
    tool_name = find_command_tool_name(tool_registry)
    if not tool_name:
        return None
    params = get_tool_param_keys(tool_registry, tool_name)
    cmd_key = "command"
    if "cmd" in params:
        cmd_key = "cmd"
    args = {cmd_key: command}
    res = await tool_registry.execute_raw(tool_name, json.dumps(args))
    if isinstance(res, dict) and "error" in res:
        return None
    if isinstance(res, str):
        return res
    if isinstance(res, dict) and "output" in res:
        return str(res["output"])
    return str(res)


async def sync_skills_to_sandbox(tool_registry: ToolRegistry) -> None:
    """Sync the project's local skills folder to the sandbox's /skills directory.
    Uses manifest checking and base64-encoded zip file sync to make it fast, 
    up-to-date, secure against path traversal, and robust against additions/deletions.
    """
    import hashlib

    # 1. Resolve local skills directory
    project_root = Path(__file__).resolve().parent.parent.parent
    skills_dir = project_root / "skills"
    if not skills_dir.exists():
        skills_dir = Path(__file__).resolve().parent.parent / "skills"
    if not skills_dir.exists():
        skills_dir = Path.cwd() / "skills"
    if not skills_dir.exists():
        logger.log("[skills] Local skills folder not found!")
        return

    # 2. Compute local manifest (file path -> MD5 hash)
    local_manifest = {}
    for file_path in skills_dir.glob("**/*"):
        if file_path.is_file():
            if "node_modules" in file_path.parts:
                continue
            relative_path = str(file_path.relative_to(skills_dir))
            try:
                content = file_path.read_bytes()
                local_manifest[relative_path] = hashlib.md5(content).hexdigest()
            except Exception:
                pass

    # 3. Read sandbox manifest and check if it matches
    sandbox_manifest_str = await sandbox_read_file(tool_registry, "/skills/.manifest.json")
    sandbox_manifest = None
    if sandbox_manifest_str is not None:
        try:
            sandbox_manifest = json.loads(sandbox_manifest_str)
        except Exception:
            pass

    if sandbox_manifest == local_manifest:
        logger.log("[skills] Skills in sandbox are up-to-date. Skipping sync.")
        return

    logger.log("[skills] Manifest mismatch or missing. Syncing skills to sandbox...")

    # 4. Package local skills directory into a zip in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for rel_path in local_manifest.keys():
            file_path = skills_dir / rel_path
            if file_path.exists() and file_path.is_file():
                zip_file.write(file_path, rel_path)
    
    zip_bytes = zip_buffer.getvalue()
    import base64
    zip_b64 = base64.b64encode(zip_bytes).decode("utf-8")
    
    # 5. Write base64 zip and local manifest to sandbox /tmp
    logger.log("[skills] Writing files to sandbox...")
    await sandbox_write_file(tool_registry, "/tmp/skills.zip.b64", zip_b64)
    await sandbox_write_file(tool_registry, "/tmp/local_manifest.json", json.dumps(local_manifest))
    
    # 6. Write secure extract script to /tmp/extract.py in sandbox
    extract_script = """import base64
import zipfile
import os
import json

try:
    # Read and decode zip file
    with open("/tmp/skills.zip.b64", "r") as f:
        b64_data = f.read()
    zip_data = base64.b64decode(b64_data)
    with open("/tmp/skills.zip", "wb") as f:
        f.write(zip_data)
        
    # Read manifest
    with open("/tmp/local_manifest.json", "r") as f:
        local_manifest = json.load(f)
        
    target_dir = "/skills"
    os.makedirs(target_dir, exist_ok=True)
    
    # Safe extraction with path traversal validation
    with zipfile.ZipFile("/tmp/skills.zip", "r") as z:
        for member in z.infolist():
            # Resolve the absolute destination path
            target_path = os.path.abspath(os.path.join(target_dir, member.filename))
            # Check if target_path starts with target_dir to prevent path traversal
            if not target_path.startswith(os.path.abspath(target_dir)):
                raise Exception(f"Directory traversal attempt detected in zip: {member.filename}")
            z.extract(member, target_dir)
            
    # Remove files that are not in the local manifest (handling deletions)
    for root, dirs, files in os.walk(target_dir):
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, target_dir)
            if rel_path == ".manifest.json":
                continue
            if rel_path not in local_manifest:
                os.remove(full_path)
                
    # Remove empty directories
    for root, dirs, files in os.walk(target_dir, topdown=False):
        for d in dirs:
            dir_path = os.path.join(root, d)
            if not os.listdir(dir_path):
                os.rmdir(dir_path)
                
    # Write the manifest file to /skills/.manifest.json
    with open(os.path.join(target_dir, ".manifest.json"), "w") as f:
        json.dump(local_manifest, f)
        
    print("SUCCESS")
except Exception as e:
    print("ERROR:", str(e))
"""
    await sandbox_write_file(tool_registry, "/tmp/extract.py", extract_script)
    
    # 7. Execute extract script in sandbox
    logger.log("[skills] Extracting skills inside sandbox...")
    res = await run_sandbox_command(tool_registry, "python3 /tmp/extract.py")
    logger.log(f"[skills] Extract result: {res}")
    
    # 8. Clean up temporary files in sandbox
    await run_sandbox_command(tool_registry, "rm -f /tmp/skills.zip.b64 /tmp/skills.zip /tmp/local_manifest.json /tmp/extract.py")


# ── Workspace Persistence & Synchronization ──

async def save_workspace_files(context: Any, files_dict: dict[str, str]) -> None:
    """Save workspace files to store (falls back to message history if KV is not supported).
    Stores {"version": N, "files": {...}} with an incrementing version number.
    """
    cid = context.conversation_id
    store = context.store
    if not cid or not store:
        return

    # Determine current version before saving
    current_version = await load_workspace_version(context)
    new_version = current_version + 1
    wrapped = {"version": new_version, "files": files_dict}

    # Try standard KV first (in case it gets supported or in other environments)
    try:
        if hasattr(store, "put"):
            res = store.put("workspace_files_global", wrapped)
            if inspect.isawaitable(res):
                await res
            logger.log(f"[workspace] Saved workspace v{new_version} to KV store for {cid}")
            return
        elif hasattr(store, "set"):
            res = store.set("workspace_files_global", wrapped)
            if inspect.isawaitable(res):
                await res
            logger.log(f"[workspace] Saved workspace v{new_version} to KV store for {cid}")
            return
    except Exception as e:
        logger.log(f"[workspace] KV store save failed, falling back to messages: {e}")

    # Fallback: Save as a system message in conversation history
    try:
        content_str = "__WORKSPACE_FILES_STATE__:" + json.dumps(wrapped)
        res = store.append_message(cid, "system", content_str)
        if inspect.isawaitable(res):
            await res
        logger.log(f"[workspace] Saved workspace v{new_version} to conversation history for {cid}")
    except Exception as e:
        logger.log(f"[workspace] Failed to save workspace to conversation history: {e}")


async def _load_workspace_raw(context: Any) -> dict[str, Any] | None:
    """Load raw workspace data from store. Returns the stored object (may be
    old-format plain dict or new-format {"version": N, "files": {...}})."""
    cid = context.conversation_id
    store = context.store
    if not cid or not store:
        return None

    raw = None

    # Try standard KV first
    try:
        if hasattr(store, "get"):
            res = store.get("workspace_files_global")
            if inspect.isawaitable(res):
                res = await res
            if res and isinstance(res, dict):
                raw = res
    except Exception as e:
        logger.log(f"[workspace] Failed to get files from KV store: {e}")

    # Fallback: Load from conversation history
    if raw is None:
        try:
            res = store.get_messages(cid, limit=500, order="desc")
            if inspect.isawaitable(res):
                messages = await res
            else:
                messages = res

            for msg in messages or []:
                role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
                content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
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


async def load_workspace_files(context: Any) -> dict[str, str]:
    """Load workspace files from store (falls back to message history if KV is empty/unsupported).
    Falls back to project templates if store is empty.
    """
    raw = await _load_workspace_raw(context)
    files_dict = _unwrap_files(raw)

    if files_dict:
        return files_dict

    # If both failed/empty, load templates
    logger.log(f"[workspace] No files in store for {context.conversation_id}. Loading default templates.")
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
                try:
                    content = filepath.read_text(encoding="utf-8").strip()
                    files_dict[rel_path] = content
                except Exception:
                    pass
    return files_dict


async def sync_workspace_to_sandbox(tool_registry: ToolRegistry, files_dict: dict[str, str]) -> None:
    """Initialize workspace files inside the stateless sandbox container under /workspace/."""
    # Clean up any existing workspace files in the sandbox to ensure deleted files are removed
    await run_sandbox_command(tool_registry, "rm -rf /workspace && mkdir -p /workspace")
    
    # Track directories we have already created to avoid duplicate mkdir commands
    created_dirs = {"/workspace"}
    
    for filename, content in files_dict.items():
        sandbox_path = f"/workspace/{filename}"
        parent_dir = os.path.dirname(sandbox_path)
        
        # If the file is in a subdirectory, make sure that subdirectory is created first
        if parent_dir not in created_dirs:
            await run_sandbox_command(tool_registry, f"mkdir -p {parent_dir}")
            created_dirs.add(parent_dir)
            
        success = await sandbox_write_file(tool_registry, sandbox_path, content)
        if success:
            logger.log(f"[workspace] Synced {filename} to sandbox path {sandbox_path}")
        else:
            logger.log(f"[workspace] Failed to sync {filename} to sandbox")


async def sync_workspace_from_sandbox(context: Any, tool_registry: ToolRegistry) -> None:
    """Read updated workspace files from the sandbox and save them back to context.store KV."""
    cid = context.conversation_id
    
    list_script = """import os, json
res = {}
target = '/workspace'
if os.path.exists(target):
    for root, dirs, files in os.walk(target):
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, target)
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    res[rel_path] = f.read()
            except Exception:
                pass
print(json.dumps(res))
"""
    await sandbox_write_file(tool_registry, "/tmp/list_workspace.py", list_script)
    output = await run_sandbox_command(tool_registry, "python3 /tmp/list_workspace.py")
    await run_sandbox_command(tool_registry, "rm -f /tmp/list_workspace.py")

    if not output:
        logger.log("[workspace] Failed to list workspace files in sandbox")
        return

    try:
        updated_files = json.loads(output)
    except Exception as e:
        logger.log(f"[workspace] Failed to parse listed workspace JSON: {e}. Output was: {output[:500]}")
        return

    logger.log(f"[workspace] Read {len(updated_files)} files from sandbox workspace")
    
    try:
        await save_workspace_files(context, updated_files)
    except Exception as e:
        logger.log(f"[workspace] Failed to save updated workspace to store: {e}")


# ── Inotify Watcher Lifecycle ──

_WATCHER_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "skills" / "fs_watcher" / "watcher.py"


async def start_inotify_watcher(tool_registry: ToolRegistry) -> None:
    """Deploy and start the inotify watcher inside the sandbox container."""
    # Read the watcher script from local skills directory
    script_path = _WATCHER_SCRIPT_PATH
    if not script_path.exists():
        script_path = Path(__file__).resolve().parent.parent / "skills" / "fs_watcher" / "watcher.py"
    if not script_path.exists():
        logger.log("[inotify] Watcher script not found, skipping watcher start")
        return

    try:
        watcher_code = script_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.log(f"[inotify] Failed to read watcher script: {e}")
        return

    # Write the watcher script to the sandbox
    await sandbox_write_file(tool_registry, "/tmp/fs_watcher.py", watcher_code)
    # Install watchdog with timeout and start the watcher in the background
    await run_sandbox_command(
        tool_registry,
        "timeout 60 pip install watchdog -q 2>/dev/null; nohup python3 /tmp/fs_watcher.py > /tmp/fs_watcher.log 2>&1 &"
    )
    # Clear any stale event log
    await sandbox_write_file(tool_registry, "/tmp/fs_events.jsonl", "")
    logger.log("[inotify] Watcher started in sandbox")


async def read_fs_change_events(tool_registry: ToolRegistry) -> list[dict]:
    """Read and consume filesystem change events from the sandbox watcher."""
    content = await sandbox_read_file(tool_registry, "/tmp/fs_events.jsonl")
    if not content or not content.strip():
        return []

    events = []
    for line in content.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            pass

    # Clear the event log after reading
    await sandbox_write_file(tool_registry, "/tmp/fs_events.jsonl", "")
    return events


async def stop_inotify_watcher(tool_registry: ToolRegistry) -> None:
    """Stop the inotify watcher process in the sandbox."""
    try:
        pid_str = await sandbox_read_file(tool_registry, "/tmp/fs_watcher.pid")
        if pid_str and pid_str.strip():
            pid = pid_str.strip().split("\n")[0].strip()
            await run_sandbox_command(tool_registry, f"kill {pid} 2>/dev/null")
    except Exception as e:
        logger.log(f"[inotify] Failed to stop watcher: {e}")

    # Clean up temp files
    await run_sandbox_command(tool_registry, "rm -f /tmp/fs_watcher.pid /tmp/fs_watcher.log /tmp/fs_events.jsonl /tmp/fs_watcher.py")
    logger.log("[inotify] Watcher stopped and temp files cleaned up")


def get_available_skills() -> list[str]:
    """Retrieve all subdirectory names inside the local project skills directory."""
    project_root = Path(__file__).resolve().parent.parent.parent
    skills_dir = project_root / "skills"
    if not skills_dir.exists():
        skills_dir = Path(__file__).resolve().parent.parent / "skills"
    if not skills_dir.exists():
        skills_dir = Path.cwd() / "skills"
        
    if skills_dir.exists() and skills_dir.is_dir():
        try:
            return sorted([d.name for d in skills_dir.iterdir() if d.is_dir()])
        except Exception:
            pass
    return []


def build_system_prompt(files_dict: dict[str, str]) -> str:
    """Builds a dynamic system prompt containing the base instructions and the
    contents of all active workspace markdown files to shape identity and preferences.
    """
    skills_list = get_available_skills()
    skills_str = ", ".join(skills_list) if skills_list else "None"

    base_prompt = (
        "You are AI Studio Warung Lakku, a smart and helpful agent running inside a sandboxed workspace.\n"
        "You have access to a set of runtime tools to help you answer questions and execute tasks.\n"
        "The runtime exposes platform tools via function calling — their exact\n"
        "names, descriptions, and parameter schemas are provided alongside this message.\n"
        "Read each tool's schema before calling it; do not assume names or parameters.\n\n"
        "Tool families you may see:\n"
        "- commands / shell: execute shell commands in the sandbox (e.g. date, ls, uname, curl).\n"
        "- files / fs: read, write, list, check, remove, or create files and directories.\n"
        "- code_interpreter / interpreter: run code in an isolated interpreter (python, javascript, bash, ...).\n"
        "- browser: fetch web pages, take screenshots, click, type, evaluate scripts.\n\n"
        "Tool-use rules:\n"
        "1. Use a tool only when it is necessary to answer the user concretely.\n"
        "2. Call tools one at a time and wait for each result before deciding the next step.\n"
        "3. Never invent, simulate, or paraphrase tool results. If a tool result is unavailable, say so.\n"
        "4. If a tool call fails, do not repeat it blindly and do not switch to unrelated operations.\n"
        "   Briefly explain the failure, adjust the parameters only if the fix is clear, otherwise ask the user for guidance.\n"
        "5. Do not perform destructive file or shell operations unless the user explicitly asks for them.\n"
        "6. If the task can be answered without tools, answer directly and keep the response concise.\n"
        "Only call tools that appear in the function-calling schema provided to you.\n\n"
        "=== WORKSPACE INSTRUCTIONS ===\n"
        "You have a dedicated workspace directory `/workspace/` containing Markdown files that define your personality, user details, and operational rules.\n"
        "You must read, respect, and update these files as needed to maintain state across sessions.\n"
        "You can read, write, or delete files in the `/workspace/` directory using your file tools (e.g., using paths like `/workspace/IDENTITY.md` or `/workspace/USER.md`).\n"
        "If you make changes to these files, they will be loaded into your system prompt in subsequent turns/sessions.\n\n"
        "=== SKILLS INSTRUCTIONS ===\n"
        "You have a read-only `/skills/` directory containing operational manuals for complex tasks.\n"
        f"Available skills you can load: {skills_str}.\n"
        "You can read any skill manual using files_read (e.g., `/skills/taste-skill/SKILL.md` or `/skills/excel-xlsx/SKILL.md`) to learn how to perform specific tasks. Do not guess how a skill works; read its SKILL.md file if you need to use it.\n\n"
        "=== SUB-AGENT DELEGATION ===\n"
        "You are the coordinator for this session. You have access to a `sessions_spawn` tool to delegate tasks.\n"
        "- Reply directly for trivial conversations, questions, or short answers.\n"
        "- Anything requiring multi-step tool calls, deep workspace file searches, coding, code reviews, debugging, or complex computations should be delegated to a specialized sub-agent using `sessions_spawn`.\n"
        "- Before spawning, define a clear objective and role for the sub-agent.\n"
        "- Treat the output of sub-agents as detailed evidence to synthesize your final response for the user.\n\n"
    )

    files_descriptions = {
        "BOOTSTRAP.md": "BOOTSTRAP / INITIALIZATION SETUP (If this file exists, you are in bootstrap mode. Follow its instructions immediately and delete this file once setup is complete)",
        "IDENTITY.md": "IDENTITY / WHO YOU ARE (Your name, creature, vibe, emoji, avatar)",
        "USER.md": "USER DETAILS / ABOUT THE HUMAN (Name, preferences, timezone, notes)",
        "SOUL.md": "SOUL / PERSONALITY & CORE PRINCIPLES (Your behavioral guidelines and personality)",
        "AGENTS.md": "AGENTS / OPERATIONAL RULES (Your standard operating procedures and workspace instructions)",
        "TOOLS.md": "TOOLS / ENVIRONMENT-SPECIFIC NOTES (Specific credentials, hardware locations, SSH hosts, preferred TTS voice, etc.)",
        "HEARTBEAT.md": "HEARTBEAT / PERIODIC TASKS (Add tasks here to run periodically; if empty, periodic checks are skipped)",
    }

    loaded_files = []
    for filename, content in files_dict.items():
        if content:
            desc = files_descriptions.get(filename, "Workspace file")
            loaded_files.append(f"### {filename} ({desc}):\n```markdown\n{content}\n```")

    workspace_content = ""
    if loaded_files:
        workspace_content = "=== CURRENT WORKSPACE FILES ===\n"
        workspace_content += "\n\n".join(loaded_files)
        workspace_content += "\n\n===============================\n"

    bootstrap_exists = "BOOTSTRAP.md" in files_dict
    if bootstrap_exists:
        base_prompt += (
            "IMPORTANT: BOOTSTRAP.md is present in your workspace. You must read it and start the onboarding conversation with the user.\n"
            "Introduce yourself as AI Studio Warung Lakku, explain that you just came online, and ask the user who they are and what name/details you should set.\n"
            "Guide them through setting up IDENTITY.md, USER.md, and SOUL.md.\n"
            "Once onboarding/bootstrap is fully finished, you MUST use your file tools to DELETE `/workspace/BOOTSTRAP.md` so that the setup is complete.\n\n"
        )

async def run_subagent_loop(context: Any, role: str, objective: str, tool_registry: ToolRegistry) -> str:
    """Spawns a specialized sub-agent to perform an objective in a nested tool-calling loop."""
    logger.log(f"[subagent] Spawning sub-agent '{role}' for objective: '{objective}'")
    
    subagent_system_prompt = (
        f"You are a specialized sub-agent running in a sandboxed workspace.\n"
        f"Your role is: {role}.\n"
        f"Your objective is: {objective}.\n"
        f"You must perform your task, use tools if necessary, and provide a clear, concise summary of your findings as your final answer.\n\n"
        "Tool-use rules:\n"
        "1. Use tools only when necessary.\n"
        "2. Call tools one at a time.\n"
        "3. Complete the task and answer directly once done.\n"
    )
    
    messages = [
        {"role": "system", "content": subagent_system_prompt},
        {"role": "user", "content": f"Please complete your objective: {objective}"}
    ]
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MODEL_CONFIG['api_key']}",
    }
    base_url = MODEL_CONFIG["base_url"].rstrip("/")
    url = f"{base_url}/chat/completions"
    model = MODEL_CONFIG["model"]
    
    MAX_SUBAGENT_ROUNDS = 5
    
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            verify=ssl_verify,
            proxy=None,
        ) as client:
            for round_idx in range(MAX_SUBAGENT_ROUNDS):
                payload = {
                    "model": model,
                    "messages": messages,
                }
                if tool_registry.has_tools():
                    payload["tools"] = tool_registry.tools
                    payload["tool_choice"] = "auto"
                    
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                res_data = response.json()
                
                choice = res_data["choices"][0]
                message_out = choice["message"]
                assistant_content = message_out.get("content") or ""
                tool_calls = message_out.get("tool_calls")
                
                # Append response
                messages.append(message_out)
                
                if not tool_calls:
                    logger.log(f"[subagent] Sub-agent finished with response length: {len(assistant_content)}")
                    return assistant_content
                    
                # ── Parallel Execution & Dependency Injection (Sub-agent Orchestrator) ──
                all_tool_call_ids = {tc["id"] for tc in tool_calls}
                executed_results = {}
                completed_ids = set()
                pending_calls = list(tool_calls)
                
                final_results_map = {}
                
                while pending_calls:
                    ready_calls = []
                    for tc in pending_calls:
                        deps = get_tool_dependencies(tc["function"]["arguments"], all_tool_call_ids)
                        if deps.issubset(completed_ids):
                            ready_calls.append(tc)
                            
                    if not ready_calls:
                        logger.log(f"[subagent orchestrator] Cycle detected or unresolved dependencies. Executing remaining {len(pending_calls)} tools.")
                        ready_calls = list(pending_calls)
                        
                    for tc in ready_calls:
                        pending_calls.remove(tc)
                        
                    ready_calls_with_resolved = []
                    for tc in ready_calls:
                        unresolved_args = tc["function"]["arguments"]
                        resolved_args = resolve_placeholders(unresolved_args, executed_results)
                        ready_calls_with_resolved.append((tc, resolved_args))
                        
                    async def run_subagent_tool(tc_item, resolved_args_str):
                        logger.log(f"[subagent] Sub-agent calling tool: {tc_item['function']['name']}")
                        tool_result = await tool_registry.execute(tc_item["function"]["name"], resolved_args_str)
                        return tc_item, tool_result
                        
                    wave_tasks = [run_subagent_tool(tc, resolved_args) for tc, resolved_args in ready_calls_with_resolved]
                    wave_outputs = await asyncio.gather(*wave_tasks)
                    
                    for tc_item, tool_result in wave_outputs:
                        executed_results[tc_item["id"]] = tool_result
                        completed_ids.add(tc_item["id"])
                        final_results_map[tc_item["id"]] = tool_result
                        
                for tc in tool_calls:
                    tool_result = final_results_map[tc["id"]]
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result
                    })
                    
                    
            return messages[-1].get("content") or "Sub-agent execution exceeded maximum rounds."
            
    except Exception as e:
        logger.error(f"[subagent] Failed during execution: {e}")
        return f"Sub-agent execution failed: {str(e)}"

def get_tool_dependencies(arguments_str: str, all_ids: set[str]) -> set[str]:
    """Scans the arguments string for placeholders matching any tool call ID in the current round."""
    pattern_curly = r"\{\{([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_-]+)|\[['\"]([^'\"]+)['\"]\])?\}\}"
    pattern_angle = r"<([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_-]+))?>"
    
    deps = set()
    if not arguments_str:
        return deps
        
    for match in re.finditer(pattern_curly, arguments_str):
        cid = match.group(1)
        if cid in all_ids:
            deps.add(cid)
            
    for match in re.finditer(pattern_angle, arguments_str):
        cid = match.group(1)
        if cid in all_ids:
            deps.add(cid)
            
    return deps


def resolve_placeholders_in_obj(obj: Any, parent_results: dict[str, Any]) -> Any:
    """Recursively walks a JSON-like structure and resolves placeholders using parent tool results."""
    pattern_curly = r"\{\{([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_-]+)|\[['\"]([^'\"]+)['\"]\])?\}\}"
    pattern_angle = r"<([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_-]+))?>"

    def get_resolved_val(tool_id: str, key: str | None) -> Any:
        if tool_id not in parent_results:
            return None
            
        parent_raw = parent_results[tool_id]
        
        # Try parsing parent output as JSON
        parent_obj = None
        if isinstance(parent_raw, str):
            try:
                parent_obj = json.loads(parent_raw)
            except json.JSONDecodeError:
                pass
        else:
            parent_obj = parent_raw

        if key:
            if isinstance(parent_obj, dict) and key in parent_obj:
                return parent_obj[key]
            elif isinstance(parent_obj, list) and key.isdigit():
                idx = int(key)
                if 0 <= idx < len(parent_obj):
                    return parent_obj[idx]
            return None
            
        # No key specified, apply heuristic resolution
        if isinstance(parent_obj, dict):
            # Check for common key fields
            for k in ["id", "key", "value", "path", "filepath", "output", "result"]:
                if k in parent_obj:
                    return parent_obj[k]
            if len(parent_obj) == 1:
                return list(parent_obj.values())[0]
            return parent_raw
            
        if isinstance(parent_raw, str):
            # Check for workspace path pattern
            path_match = re.search(r"(/workspace/[a-zA-Z0-9_\.\-/]+)", parent_raw)
            if path_match:
                return path_match.group(1)
                
            # Check for ID pattern
            id_match = re.search(r"\b(?:ID|id|Id|key|Key)[:\s]+([a-zA-Z0-9_-]+)", parent_raw)
            if id_match:
                return id_match.group(1)
                
            return parent_raw.strip()
            
        return parent_raw

    def process_string(val_str: str) -> Any:
        # Check if the string is EXACTLY a single curly placeholder
        m_curly_exact = re.match(r"^" + pattern_curly + r"$", val_str)
        if m_curly_exact:
            tool_id = m_curly_exact.group(1)
            key = m_curly_exact.group(2) or (m_curly_exact.group(3) if len(m_curly_exact.groups()) > 2 else None)
            resolved = get_resolved_val(tool_id, key)
            if resolved is not None:
                return resolved
                
        # Check if the string is EXACTLY a single angle placeholder
        m_angle_exact = re.match(r"^" + pattern_angle + r"$", val_str)
        if m_angle_exact:
            tool_id = m_angle_exact.group(1)
            key = m_angle_exact.group(2)
            resolved = get_resolved_val(tool_id, key)
            if resolved is not None:
                return resolved

        # Otherwise, do string substitution for all matches
        def sub_curly(match):
            tool_id = match.group(1)
            key = match.group(2) or (match.group(3) if len(match.groups()) > 2 else None)
            resolved = get_resolved_val(tool_id, key)
            if resolved is not None:
                if isinstance(resolved, (dict, list)):
                    return json.dumps(resolved)
                return str(resolved)
            return match.group(0)

        def sub_angle(match):
            tool_id = match.group(1)
            key = match.group(2)
            resolved = get_resolved_val(tool_id, key)
            if resolved is not None:
                if isinstance(resolved, (dict, list)):
                    return json.dumps(resolved)
                return str(resolved)
            return match.group(0)

        res_str = re.sub(pattern_curly, sub_curly, val_str)
        res_str = re.sub(pattern_angle, sub_angle, res_str)
        return res_str

    if isinstance(obj, str):
        return process_string(obj)
    elif isinstance(obj, dict):
        return {k: resolve_placeholders_in_obj(v, parent_results) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [resolve_placeholders_in_obj(item, parent_results) for item in obj]
    else:
        return obj


def resolve_placeholders(arguments_str: str, parent_results: dict[str, Any]) -> str:
    """Resolves placeholders inside the arguments JSON string or plain string."""
    if not arguments_str:
        return arguments_str
    try:
        obj = json.loads(arguments_str)
        resolved_obj = resolve_placeholders_in_obj(obj, parent_results)
        return json.dumps(resolved_obj)
    except Exception:
        # Fallback to simple string replacement
        pattern_curly = r"\{\{([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_-]+)|\[['\"]([^'\"]+)['\"]\])?\}\}"
        pattern_angle = r"<([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_-]+))?>"
        
        def get_resolved_val(tool_id: str, key: str | None) -> Any:
            if tool_id not in parent_results:
                return None
            parent_raw = parent_results[tool_id]
            parent_obj = None
            if isinstance(parent_raw, str):
                try:
                    parent_obj = json.loads(parent_raw)
                except json.JSONDecodeError:
                    pass
            else:
                parent_obj = parent_raw
            if key:
                if isinstance(parent_obj, dict) and key in parent_obj:
                    return parent_obj[key]
                return None
            if isinstance(parent_obj, dict):
                for k in ["id", "key", "value", "path", "filepath", "output", "result"]:
                    if k in parent_obj:
                        return parent_obj[k]
                if len(parent_obj) == 1:
                    return list(parent_obj.values())[0]
                return parent_raw
            if isinstance(parent_raw, str):
                path_match = re.search(r"(/workspace/[a-zA-Z0-9_\.\-/]+)", parent_raw)
                if path_match:
                    return path_match.group(1)
                id_match = re.search(r"\b(?:ID|id|Id|key|Key)[:\s]+([a-zA-Z0-9_-]+)", parent_raw)
                if id_match:
                    return id_match.group(1)
                return parent_raw.strip()
            return parent_raw

        def sub_curly(match):
            tool_id = match.group(1)
            key = match.group(2) or (match.group(3) if len(match.groups()) > 2 else None)
            resolved = get_resolved_val(tool_id, key)
            if resolved is not None:
                if isinstance(resolved, (dict, list)):
                    return json.dumps(resolved)
                return str(resolved)
            return match.group(0)

        def sub_angle(match):
            tool_id = match.group(1)
            key = match.group(2)
            resolved = get_resolved_val(tool_id, key)
            if resolved is not None:
                if isinstance(resolved, (dict, list)):
                    return json.dumps(resolved)
                return str(resolved)
            return match.group(0)

        res_str = re.sub(pattern_curly, sub_curly, arguments_str)
        res_str = re.sub(pattern_angle, sub_angle, res_str)
        return res_str


# Maximum number of tool call rounds to prevent infinite loops
MAX_TOOL_ROUNDS = 10
MAX_MESSAGE_LENGTH = 10000


async def handler(context: Any) -> AsyncGenerator[str, None]:
    """EdgeOne Makers entry point.

    Streams LLM responses with EdgeOne platform tool calling support.
    Instruments key operations via context.tracer for observability.
    """
    cid = context.conversation_id
    logger.log(f"[handler] conversation_id: {cid}")

    body = context.request.body
    message = body.get("message") if isinstance(body, dict) else None
    # Allow request to override the default model
    request_model = body.get("model") if isinstance(body, dict) else None
    model = request_model or MODEL_CONFIG["model"]

    # ── Tracer: set request-level attributes ──
    context.tracer.set_attributes({
        "agent.scenario": "python_starter_chat",
        "chat.conversation_id": cid,
        "chat.has_message": bool(message),
    })

    if not message:
        yield sse_event("error", {"message": "'message' is required"})
        yield sse_event("done", {})
        return

    if len(message) > MAX_MESSAGE_LENGTH:
        yield sse_event("error", {"message": f"Message too long (max {MAX_MESSAGE_LENGTH} characters)"})
        yield sse_event("done", {})
        return

    # ── Session: load history + save user message ──
    session = ChatSession(context.store)

    session_span = context.tracer.start_span("session.load_and_save", {
        "session.conversation_id": cid,
    })
    try:
        history, _ = await asyncio.gather(
            session.get_history(cid),
            session.save_user_message(cid, message),
        )
        session_span.set_attributes({"session.history_count": len(history)})
    finally:
        session_span.end()

    # ── Tools: build registry from EdgeOne platform tools ──
    tools_span = context.tracer.start_span("tools.build")
    try:
        tool_registry = build_tools(context, logger)
        
        # Register custom sessions_spawn subagent tool
        async def sessions_spawn(objective: str, role: str) -> str:
            return await run_subagent_loop(context, role, objective, tool_registry)
            
        spawn_schema = {
            "type": "function",
            "function": {
                "name": "sessions_spawn",
                "description": (
                    "Spawn a specialized sub-agent to perform a specific, isolated task "
                    "(e.g. coding, file analysis, web research, debugging, multi-step computations) "
                    "and return its output. This keeps the main agent clean and responsive."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "objective": {
                            "type": "string",
                            "description": "The exact goal and instructions for the sub-agent. Be specific about inputs, outputs, and requirements."
                        },
                        "role": {
                            "type": "string",
                            "description": "The role/persona of the sub-agent (e.g. Coder, Code Reviewer, Researcher, Math Solver, Writer, Auditor)."
                        }
                    },
                    "required": ["objective", "role"]
                }
            }
        }
        tool_registry.register("sessions_spawn", spawn_schema, sessions_spawn)

        tools_span.set_attributes({
            "tools.count": len(tool_registry.tools),
            "tools.has_tools": tool_registry.has_tools(),
        })
    finally:
        tools_span.end()

    # ── Workspace: Load files and initialize sandbox ──
    files_dict = await load_workspace_files(context)
    await sync_workspace_to_sandbox(tool_registry, files_dict)

    # ── Inotify: Start filesystem watcher in sandbox ──
    await start_inotify_watcher(tool_registry)

    # ── Set active tool registry for workspace sync endpoint ──
    from ..workspace.files import set_active_tool_registry
    set_active_tool_registry(tool_registry)

    # ── Skills: Sync local skills to sandbox ──
    await sync_skills_to_sandbox(tool_registry)

    # Build messages list: system prompt + history + current user message
    messages: list[dict[str, Any]] = (
        [{"role": "system", "content": build_system_prompt(files_dict)}]
        + history
        + [{"role": "user", "content": message}]
    )

    # Get platform cancel signal
    cancel_signal = context.request.signal

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MODEL_CONFIG['api_key']}",
    }

    base_url = MODEL_CONFIG["base_url"].rstrip("/")
    url = f"{base_url}/chat/completions"

    logger.log(f"[handler] streaming from: {url}, model: {model}, tools: {tool_registry.has_tools()}")

    assistant_content = ""
    cancelled = False
    success = False

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            verify=ssl_verify,
            proxy=None,
        ) as client:

            for round_idx in range(MAX_TOOL_ROUNDS):
                if cancel_signal.is_set():
                    cancelled = True
                    break

                # Build payload
                payload: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if tool_registry.has_tools():
                    payload["tools"] = tool_registry.tools
                    payload["tool_choice"] = "auto"

                logger.log(f"[handler] round {round_idx + 1}, messages: {len(messages)}")

                # ── Tracer: LLM request span ──
                llm_span = context.tracer.start_span(f"llm.request.round_{round_idx + 1}", {
                    "openinference.span.kind": "LLM",
                    "llm.model_name": model,
                    "llm.provider": "openai",
                    "llm.request.type": "chat",
                    "llm.request.message_count": len(messages),
                    "llm.request.tools_included": "tools" in payload,
                    "llm.request.round": round_idx + 1,
                })

                round_result: LlmRoundResult | None = None
                async for item in stream_llm_round(
                    client=client,
                    url=url,
                    payload=payload,
                    headers=headers,
                    cancel_signal=cancel_signal,
                    llm_span=llm_span,
                    logger=logger,
                ):
                    if isinstance(item, str):
                        yield item
                    else:
                        round_result = item

                if round_result is None:
                    break

                if round_result.should_return:
                    return

                round_content = round_result.round_content
                tool_calls = round_result.tool_calls
                assistant_content += round_content

                if round_result.cancelled:
                    cancelled = True
                    break

                if not tool_calls:
                    break

                # Append assistant message with tool_calls to messages
                assistant_msg: dict[str, Any] = {"role": "assistant"}
                if round_content:
                    assistant_msg["content"] = round_content
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    for tc in tool_calls
                ]
                messages.append(assistant_msg)

                # ── Parallel Execution & Dependency Injection (Universal Tool Orchestrator) ──
                all_tool_call_ids = {tc["id"] for tc in tool_calls}
                executed_results = {}  # tool_call_id -> result string
                completed_ids = set()
                pending_calls = list(tool_calls)

                # Keep ordered maps of outputs to maintain results order
                final_results_map = {}
                final_extractions_map = {}
                final_durations_map = {}

                while pending_calls:
                    # Find tool calls in the wave that are ready (all their dependencies are completed)
                    ready_calls = []
                    for tc in pending_calls:
                        deps = get_tool_dependencies(tc["arguments"], all_tool_call_ids)
                        if deps.issubset(completed_ids):
                            ready_calls.append(tc)

                    # Fallback for circular dependencies or stuck state
                    if not ready_calls:
                        logger.log(f"[orchestrator] Cycle detected or unresolved dependencies. Executing remaining {len(pending_calls)} tools.")
                        ready_calls = list(pending_calls)

                    # Remove ready calls from pending list
                    for tc in ready_calls:
                        pending_calls.remove(tc)

                # 1. Resolve arguments and emit call events
                    # Take a read-only snapshot of workspace files from KV so the
                    # frontend can update its local state even when the sandbox
                    # has been reset between requests.
                    files_snapshot: dict[str, str] | None = None
                    try:
                        files_snapshot = await snapshot_workspace(context)
                        if not files_snapshot:
                            files_snapshot = None
                    except Exception as e:
                        logger.log(f"[orchestrator] Failed to snapshot workspace: {e}")

                    ready_calls_with_resolved = []
                    for tc in ready_calls:
                        unresolved_args = tc["arguments"]
                        resolved_args = resolve_placeholders(unresolved_args, executed_results)
                        ready_calls_with_resolved.append((tc, resolved_args))

                        yield sse_event("tool_called", {"tool": tc["name"], "files_snapshot": files_snapshot})
                        yield sse_event("tool_debug", {
                            "phase": "call",
                            "tool": tc["name"],
                            "id": tc["id"],
                            "argumentsPreview": safe_json_preview(resolved_args, 1200),
                            "files_snapshot": files_snapshot,
                        })

                    # 2. Run execution of ready calls in parallel
                    async def run_single_tool(tc_item, resolved_args_str):
                        ts = context.tracer.start_span(f"tool.{tc_item['name']}", {
                            "tool.name": tc_item["name"],
                            "tool.call_id": tc_item["id"],
                            "tool.arguments_length": len(resolved_args_str),
                        })
                        try:
                            started_at = time.perf_counter()
                            raw = await tool_registry.execute_raw(
                                tc_item["name"], resolved_args_str
                            )
                            extraction = extract_images_from_tool_result(raw)
                            result = _stringify_result(extraction.redacted_result)
                            duration_ms = int((time.perf_counter() - started_at) * 1000)

                            ts.set_attributes({"tool.result_length": len(result)})
                            return tc_item, result, extraction, duration_ms
                        finally:
                            ts.end()

                    # Execute wave in parallel
                    wave_tasks = [run_single_tool(tc, resolved_args) for tc, resolved_args in ready_calls_with_resolved]
                    wave_outputs = await asyncio.gather(*wave_tasks)

                    # Read inotify change events from sandbox
                    try:
                        change_events = await read_fs_change_events(tool_registry)
                        if change_events:
                            # Sync immediately and notify frontend
                            await sync_workspace_from_sandbox(context, tool_registry)
                            current_version = await load_workspace_version(context)
                            changed_paths = list(set(e["path"] for e in change_events if "path" in e))
                            yield sse_event("file_changed", {"version": current_version, "changed": changed_paths})
                    except Exception as e:
                        logger.log(f"[workspace] Failed to read fs change events: {e}")

                    # 3. Process outputs and emit result/image events
                    for tc_item, result, extraction, duration_ms in wave_outputs:
                        executed_results[tc_item["id"]] = result
                        completed_ids.add(tc_item["id"])

                        final_results_map[tc_item["id"]] = result
                        final_extractions_map[tc_item["id"]] = extraction
                        final_durations_map[tc_item["id"]] = duration_ms

                        # SSE ordering contract: image events fire AFTER tool_debug{phase:'call'} and BEFORE tool_debug{phase:'result'}.
                        for img in extraction.images:
                            yield sse_event("image", {
                                "imageId": img.image_id,
                                "base64": img.base64,
                                "mimeType": img.mime_type,
                                "size": img.size,
                                "toolName": tc_item["name"],
                                "toolCallId": tc_item["id"],
                            })

                        debug_payload: dict[str, Any] = {
                            "phase": "result",
                            "tool": tc_item["name"],
                            "id": tc_item["id"],
                            "resultPreview": safe_json_preview(result, 2000),
                            "resultLength": len(result),
                            "durationMs": duration_ms,
                            "imageCount": len(extraction.images),
                        }
                        if extraction.truncated:
                            debug_payload["imagesTruncated"] = True
                        yield sse_event("tool_debug", debug_payload)

                # Reconstruct ordered results to append to messages list
                for tc in tool_calls:
                    tool_result = final_results_map[tc["id"]]
                    logger.log(f"[tool] {tc['name']}: {tool_result[:200]}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })
            success = True

    except (httpx.HTTPError, httpx.StreamError) as e:
        logger.error(f"[handler] httpx error: {type(e).__name__}: {e}")
        context.tracer.set_attributes({
            "error.type": type(e).__name__,
            "error.message": str(e),
        })
        yield sse_event("error", {
            "message": "LLM service request failed, please try again later",
            "errorType": type(e).__name__,
            "detail": str(e),
        })
    except Exception as e:
        logger.error(f"[handler] unexpected error: {type(e).__name__}: {e}")
        context.tracer.set_attributes({
            "error.type": type(e).__name__,
            "error.message": str(e),
        })
        yield sse_event("error", {
            "message": "Internal server error",
            "errorType": type(e).__name__,
            "detail": str(e),
        })

    # ── Tracer: save assistant response ──
    if assistant_content:
        save_span = context.tracer.start_span("session.save_assistant_message", {
            "session.conversation_id": cid,
            "session.content_length": len(assistant_content),
        })
        try:
            await session.save_assistant_message(cid, assistant_content)
        finally:
            save_span.end()

    # ── Workspace: already synced during tool execution via inotify change events; skip redundant end-of-handler sync ──

    if 'tool_registry' in locals() and tool_registry:
        # ── Inotify: Stop filesystem watcher ──
        try:
            await stop_inotify_watcher(tool_registry)
        except Exception as e:
            logger.log(f"[inotify] Failed to stop watcher: {e}")

    # ── Clear active tool registry ──
    from ..workspace.files import clear_active_tool_registry
    clear_active_tool_registry()

    yield sse_event("done", {"stopped": cancelled})
