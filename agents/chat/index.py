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
import re
import os
import shlex
import io
import zipfile
import base64

import httpx

from .._model import MODEL_CONFIG, ssl_verify
from .._logger import create_logger
from .._session import ChatSession
from .._tools import build_tools, ToolRegistry, _stringify_result
from ._stream import LlmRoundResult, sse_event, stream_llm_round, safe_json_preview
from ._images import extract_images_from_tool_result
from ..workspace.files import snapshot_workspace, load_workspace_files, save_workspace_files, load_workspace_version, _load_workspace_raw, _unwrap_files


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
    """Retrieve parameter keys for the specified tool.
    Tries handler signature first (works for internal/hidden tools),
    then falls back to schema properties in tool_registry.tools.
    """
    # Primary: inspect handler signature (works for internal tools not in tool_registry.tools)
    params = tool_registry.get_params(tool_name)
    if params:
        return params
    # Fallback: parse OpenAI schema properties
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
    
    res = await tool_registry.execute_raw(tool_name, json.dumps(args), system=True)
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
    res = await tool_registry.execute_raw(tool_name, json.dumps(args), system=True)
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


async def run_sandbox_command_system(tool_registry: ToolRegistry, command: str) -> str | None:
    """Directly execute a shell command in the sandbox container without wrapping (system mode)."""
    tool_name = find_command_tool_name(tool_registry)
    if not tool_name:
        return None
    params = get_tool_param_keys(tool_registry, tool_name)
    cmd_key = "command"
    if "cmd" in params:
        cmd_key = "cmd"
    args = {cmd_key: command}
    res = await tool_registry.execute_raw(tool_name, json.dumps(args), system=True)
    if isinstance(res, dict) and "error" in res:
        return None
    if isinstance(res, str):
        return res
    if isinstance(res, dict) and "output" in res:
        return str(res["output"])
    return str(res)


async def run_sandbox_command(tool_registry: ToolRegistry, command: str) -> str | None:
    """Execute a shell command inside the sandbox, in /workspace (created if needed)."""
    wrapped_command = f"mkdir -p /workspace 2>/dev/null; cd /workspace 2>/dev/null || cd ~; ({command})"
    return await run_sandbox_command_system(tool_registry, wrapped_command)



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
    res = await run_sandbox_command_system(tool_registry, "python3 /tmp/extract.py")
    logger.log(f"[skills] Extract result: {res}")
    
    # 8. Clean up temporary files in sandbox
    await run_sandbox_command_system(tool_registry, "rm -f /tmp/skills.zip.b64 /tmp/skills.zip /tmp/local_manifest.json /tmp/extract.py")




async def sync_workspace_to_sandbox(tool_registry: ToolRegistry, files_dict: dict[str, str]) -> None:
    """Push workspace files to sandbox /workspace/ using Python-based shell writes.
    
    Uses base64+Python instead of platform file tool to avoid parameter-name
    detection issues with internal (hidden) platform tools.
    """
    # Clean up any existing workspace files to ensure deleted files are removed
    await run_sandbox_command_system(tool_registry, "rm -rf /workspace && mkdir -p /workspace")

    if not files_dict:
        return

    # Write files via Python embedded in shell commands.
    # Content is base64-encoded to avoid any quoting/escaping issues with arbitrary text.
    for filepath, content in files_dict.items():
        safe_path = f"/workspace/{filepath}"
        content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        py_cmd = (
            f"python3 -c \""
            f"import base64, os; "
            f"p='{safe_path}'; "
            f"os.makedirs(os.path.dirname(p) or '.', exist_ok=True); "
            f"open(p,'wb').write(base64.b64decode('{content_b64}'))"
            f"\""
        )
        await run_sandbox_command_system(tool_registry, py_cmd)

    logger.log(f"[workspace] Synced {len(files_dict)} files to sandbox via Python writes")


async def ensure_sandbox_initialized(context: Any, tool_registry: ToolRegistry) -> None:
    """Check if the sandbox container has lost its workspace or skills, and reinitialize on the fly if needed."""
    init_res = await run_sandbox_command_system(tool_registry, "cat /tmp/.sandbox_init 2>/dev/null")
    
    current_version = await load_workspace_version(context)
    sandbox_version_str = await sandbox_read_file(tool_registry, "/tmp/.workspace_version")
    try:
        sandbox_version = int(sandbox_version_str.strip()) if sandbox_version_str else -1
    except Exception:
        sandbox_version = -1

    needs_workspace_sync = (not init_res or "/tmp/.sandbox_init" not in init_res or sandbox_version != current_version)

    if needs_workspace_sync:
        logger.log(f"[sandbox] Workspace sync needed (sandbox version: {sandbox_version}, cloud version: {current_version}). Syncing...")
        files_dict = await load_workspace_files(context)
        await sync_workspace_to_sandbox(tool_registry, files_dict)
        await sandbox_write_file(tool_registry, "/tmp/.workspace_version", str(current_version))

    if not init_res or "/tmp/.sandbox_init" not in init_res:
        logger.log("[sandbox] Sandbox sentinel missing. Deploying skills...")
        # Re-sync skills
        await sync_skills_to_sandbox(tool_registry)
        # Write the sentinel file
        await sandbox_write_file(tool_registry, "/tmp/.sandbox_init", "1")
        logger.log("[sandbox] Sandbox successfully initialized!")


async def sync_workspace_from_sandbox(context: Any, tool_registry: ToolRegistry) -> dict[str, str]:
    """Read updated workspace files from the sandbox and save them back to context.store KV."""
    cid = context.conversation_id

    # Read files from sandbox using a Python script that outputs JSON
    list_script = """import os, json
target = '/workspace'
res = []
if os.path.exists(target):
    for root, dirs, files in os.walk(target):
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, target)
            try:
                with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                res.append({"path": rel_path, "content": content})
            except Exception:
                pass
print(json.dumps(res))
"""
    await sandbox_write_file(tool_registry, "/tmp/read_workspace.py", list_script)
    output = await run_sandbox_command_system(tool_registry, "python3 /tmp/read_workspace.py")
    await run_sandbox_command_system(tool_registry, "rm -f /tmp/read_workspace.py")

    if not output:
        logger.log("[workspace] Failed to read workspace from sandbox")
        return {}

    try:
        file_entries = json.loads(output.strip())
        updated_files = {entry["path"]: entry["content"] for entry in file_entries}
    except Exception as e:
        logger.log(f"[workspace] Failed to parse workspace output: {e}")
        return {}

    logger.log(f"[workspace] Read {len(updated_files)} files from sandbox via direct read")
    try:
        await save_workspace_files(context, updated_files)
    except Exception as e:
        logger.log(f"[workspace] Failed to save updated workspace to store: {e}")

    return updated_files


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
    await run_sandbox_command_system(
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
            await run_sandbox_command_system(tool_registry, f"kill {pid} 2>/dev/null")
    except Exception as e:
        logger.log(f"[inotify] Failed to stop watcher: {e}")

    # Clean up temp files
    await run_sandbox_command_system(tool_registry, "rm -f /tmp/fs_watcher.pid /tmp/fs_watcher.log /tmp/fs_events.jsonl /tmp/fs_watcher.py")
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
        "- local files / fs: read, write, list, or delete files in the user's local workspace (`local_read_file`, `local_write_file`, etc.).\n"
        "- sandbox files / fs: read, write, list, or delete files in the stateless sandboxed container (`sandbox_read_file`, `sandbox_write_file`, etc.).\n"
        "- code_interpreter / interpreter: run code in an isolated interpreter (python, javascript, bash, ...).\n"
        "- browser: fetch web pages, take screenshots, click, type, evaluate scripts.\n\n"
        "=== TWO FILESYSTEMS ARCHITECTURE ===\n"
        "You have access to two distinct filesystems:\n"
        "1. Local Workspace Filesystem (via `local_` tools):\n"
        "   - These tools (`local_read_file`, `local_write_file`, `local_delete_file`, `local_list_files`) operate on the user's persistent local workspace (shown on their local editor).\n"
        "   - Any change here updates the files directly on the user's machine.\n"
        "   - Use these tools to save and manage code, markdown files, configuration, and other project source files.\n"
        "2. Sandbox Filesystem (via `sandbox_` tools):\n"
        "   - These tools (`sandbox_read_file`, `sandbox_write_file`, `sandbox_delete_file`, `sandbox_list_files`) operate on the stateless sandboxed execution container.\n"
        "   - This container is stateless and isolated. Changes here do NOT propagate to the user's local machine unless you explicitly read them and write them to the local workspace.\n"
        "   - Use this to store temporary build artifacts, compiled code, virtual environments, or run scripts without cluttering the user's host machine.\n"
        "   - Note: The `/workspace/` directory in the sandbox is initialized with a copy of your local workspace files at the start of the session, but is NOT automatically synced back. To edit code for the user, use the `local_` tools.\n\n"
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

    return base_prompt + workspace_content


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
                        
                    # Ensure sandbox is still initialized before running tools
                    await ensure_sandbox_initialized(context, tool_registry)

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

        # ── Local Workspace Tools ──
        tool_registry.local_fs_dirty = False

        def _local_path(filename: str) -> str:
            """Strip any leading /workspace/ prefix so the KV dict key is always relative."""
            for prefix in ("/workspace/", "workspace/", "./workspace/"):
                if filename.startswith(prefix):
                    return filename[len(prefix):]
            return filename.lstrip("./")

        async def local_read_file(filename: str) -> str:
            filename = _local_path(filename)
            files_dict = await load_workspace_files(context)
            if filename in files_dict:
                return files_dict[filename]
            return f"Error: File '{filename}' not found in local workspace."

        async def local_write_file(filename: str, content: str) -> str:
            filename = _local_path(filename)
            current_files = await load_workspace_files(context)
            current_files[filename] = content
            await save_workspace_files(context, current_files)
            await sandbox_write_file(tool_registry, f"/workspace/{filename}", content)
            tool_registry.local_fs_dirty = True
            return f"Successfully wrote file '{filename}' to local workspace."

        async def local_delete_file(filename: str) -> str:
            filename = _local_path(filename)
            current_files = await load_workspace_files(context)
            if filename in current_files:
                del current_files[filename]
                await save_workspace_files(context, current_files)
                await run_sandbox_command_system(tool_registry, f"rm -f /workspace/{shlex.quote(filename)}")
                tool_registry.local_fs_dirty = True
                return f"Successfully deleted file '{filename}' from local workspace."
            return f"Error: File '{filename}' not found in local workspace."

        async def local_list_files() -> str:
            current_files = await load_workspace_files(context)
            if not current_files:
                return "Local workspace is empty."
            files_list = [f"- {name} ({len(content)} bytes)" for name, content in current_files.items()]
            return "Files in local workspace:\n" + "\n".join(files_list)

        # Register local tools schemas
        tool_registry.register("local_read_file", {
            "type": "function",
            "function": {
                "name": "local_read_file",
                "description": "Read the contents of a file in the user's local workspace (frontend filesystem). Use this to inspect code files that the user sees on their machine.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "The relative path of the file to read (e.g. 'main.py' or 'src/utils.py')."
                        }
                    },
                    "required": ["filename"]
                }
            }
        }, local_read_file)

        tool_registry.register("local_write_file", {
            "type": "function",
            "function": {
                "name": "local_write_file",
                "description": "Write or update a file in the user's local workspace (frontend filesystem). This immediately syncs and updates the file on the user's computer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "The relative path of the file to write (e.g. 'main.py' or 'src/utils.py')."
                        },
                        "content": {
                            "type": "string",
                            "description": "The full text content to write into the file."
                        }
                    },
                    "required": ["filename", "content"]
                }
            }
        }, local_write_file)

        tool_registry.register("local_delete_file", {
            "type": "function",
            "function": {
                "name": "local_delete_file",
                "description": "Delete a file from the user's local workspace (frontend filesystem).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "The relative path of the file to delete (e.g. 'main.py' or 'src/utils.py')."
                        }
                    },
                    "required": ["filename"]
                }
            }
        }, local_delete_file)

        tool_registry.register("local_list_files", {
            "type": "function",
            "function": {
                "name": "local_list_files",
                "description": "List all files present in the user's local workspace (frontend filesystem).",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        }, local_list_files)

        # ── Sandbox Workspace Tools (for the stateless backend container) ──
        async def sandbox_read_file_tool(filename: str) -> str:
            path = filename if filename.startswith("/") else f"/workspace/{filename}"
            content = await sandbox_read_file(tool_registry, path)
            if content is None:
                return f"Error: File '{filename}' not found or could not be read in sandbox."
            return content

        async def sandbox_write_file_tool(filename: str, content: str) -> str:
            path = filename if filename.startswith("/") else f"/workspace/{filename}"
            parent_dir = os.path.dirname(path)
            if parent_dir and parent_dir != "/":
                await run_sandbox_command_system(tool_registry, f"mkdir -p {parent_dir}")
            success = await sandbox_write_file(tool_registry, path, content)
            if success:
                return f"Successfully wrote file '{filename}' in sandbox container."
            return f"Error: Failed to write file '{filename}' in sandbox container."

        async def sandbox_delete_file_tool(filename: str) -> str:
            path = filename if filename.startswith("/") else f"/workspace/{filename}"
            await run_sandbox_command_system(tool_registry, f"rm -f {shlex.quote(path)}")
            return f"Successfully deleted file '{filename}' from sandbox container."

        async def sandbox_list_files_tool() -> str:
            list_script = """import os, json
target = '/workspace'
res = []
if os.path.exists(target):
    for root, dirs, files in os.walk(target):
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, target)
            try:
                size = os.path.getsize(full_path)
                res.append(f"- {rel_path} ({size} bytes)")
            except Exception:
                pass
if res:
    print("\\n".join(res))
else:
    print("Sandbox workspace is empty.")
"""
            await sandbox_write_file(tool_registry, "/tmp/list_sandbox_workspace.py", list_script)
            output = await run_sandbox_command_system(tool_registry, "python3 /tmp/list_sandbox_workspace.py")
            await run_sandbox_command_system(tool_registry, "rm -f /tmp/list_sandbox_workspace.py")
            if not output:
                return "Sandbox workspace (/workspace) is empty or could not be read."
            return "Files in sandbox workspace:\n" + output.strip()

        # Register sandbox tools schemas
        tool_registry.register("sandbox_read_file", {
            "type": "function",
            "function": {
                "name": "sandbox_read_file",
                "description": "Read the contents of a file inside the stateless sandboxed execution container. Use this to inspect runtime outputs, build artifacts, or scripts.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "The path of the file to read. Relative paths are resolved against /workspace/."
                        }
                    },
                    "required": ["filename"]
                }
            }
        }, sandbox_read_file_tool)

        tool_registry.register("sandbox_write_file", {
            "type": "function",
            "function": {
                "name": "sandbox_write_file",
                "description": "Write or update a file inside the stateless sandboxed execution container. This file will only exist in the container and will NOT be synced back to the user's host machine.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "The path of the file to write. Relative paths are resolved against /workspace/."
                        },
                        "content": {
                            "type": "string",
                            "description": "The full text content to write into the file."
                        }
                    },
                    "required": ["filename", "content"]
                }
            }
        }, sandbox_write_file_tool)

        tool_registry.register("sandbox_delete_file", {
            "type": "function",
            "function": {
                "name": "sandbox_delete_file",
                "description": "Delete a file from the stateless sandboxed execution container.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "The path of the file to delete. Relative paths are resolved against /workspace/."
                        }
                    },
                    "required": ["filename"]
                }
            }
        }, sandbox_delete_file_tool)

        tool_registry.register("sandbox_list_files", {
            "type": "function",
            "function": {
                "name": "sandbox_list_files",
                "description": "List all files present inside the /workspace directory of the stateless sandboxed execution container.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        }, sandbox_list_files_tool)

        tools_span.set_attributes({
            "tools.count": len(tool_registry.tools),
            "tools.has_tools": tool_registry.has_tools(),
        })
    finally:
        tools_span.end()

    # ── Workspace & Skills: Ensure sandbox is initialized with sentinel ──
    files_dict = await load_workspace_files(context)
    await ensure_sandbox_initialized(context, tool_registry)

    # ── Set active tool registry for workspace sync endpoint ──
    from ..workspace.files import set_active_tool_registry
    set_active_tool_registry(tool_registry)

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

                    # Ensure sandbox is still initialized before running tools
                    # (This already conditionally syncs workspace if version is stale)
                    await ensure_sandbox_initialized(context, tool_registry)

                    # Execute wave in parallel
                    wave_tasks = [run_single_tool(tc, resolved_args) for tc, resolved_args in ready_calls_with_resolved]
                    wave_outputs = await asyncio.gather(*wave_tasks)

                    # ── Post-toolcall sync: pull sandbox changes back after execution ──
                    try:
                        post_files = await sync_workspace_from_sandbox(context, tool_registry)
                        if post_files:
                            logger.log(f"[sync] Post-toolcall: synced {len(post_files)} files from sandbox")
                    except Exception as sync_err:
                        logger.log(f"[sync] Post-toolcall sync failed: {sync_err}")

                    # Check if local workspace was modified by custom tools
                    if getattr(tool_registry, "local_fs_dirty", False):
                        tool_registry.local_fs_dirty = False
                        current_version = await load_workspace_version(context)
                        yield sse_event("file_changed", {"version": current_version})

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


    # ── Workspace: Merge sandbox changes back to KV (never overwrite user's external edits) ──
    if 'tool_registry' in locals() and tool_registry:
        try:
            logger.log("[workspace] Merging sandbox workspace changes back to KV...")
            sandbox_files = await sync_workspace_from_sandbox(context, tool_registry)

            if sandbox_files is not None:
                # Load current KV state (may include user's frontend edits made during this turn)
                current_kv = await load_workspace_files(context)
                # pre-turn snapshot was loaded at handler start into files_dict
                pre_turn = files_dict  # dict captured at top of handler

                merged = dict(current_kv)  # start from latest KV (includes user edits)
                agent_changed = 0
                for fname, sb_content in sandbox_files.items():
                    pre_content = pre_turn.get(fname)
                    kv_content = current_kv.get(fname)
                    # Agent changed this file if sandbox content differs from pre-turn snapshot
                    agent_modified = sb_content != pre_content
                    # User externally edited if KV content differs from pre-turn snapshot
                    user_modified = kv_content != pre_content
                    if agent_modified and not user_modified:
                        # Safe to take agent's version
                        merged[fname] = sb_content
                        agent_changed += 1
                    elif agent_modified and user_modified:
                        # Conflict: agent and user both changed — prefer user's edit (KV wins)
                        logger.log(f"[workspace] Merge conflict on '{fname}': keeping user's KV version")
                    # else: neither changed, or only user changed → KV already correct

                # Also remove files that agent deleted (existed in pre-turn but not in sandbox)
                for fname in list(pre_turn.keys()):
                    if fname not in sandbox_files and fname in merged:
                        # Agent deleted it (and user didn't re-create it externally)
                        if current_kv.get(fname) == pre_turn.get(fname):
                            del merged[fname]
                            agent_changed += 1

                if agent_changed > 0:
                    await save_workspace_files(context, merged)
                    logger.log(f"[workspace] Merged {agent_changed} agent-changed files into KV")

            # Update sandbox version sentinel and notify frontend
            current_version = await load_workspace_version(context)
            await sandbox_write_file(tool_registry, "/tmp/.workspace_version", str(current_version))
            yield sse_event("file_changed", {"version": current_version})
        except Exception as e:
            logger.error(f"[workspace] Failed to merge workspace from sandbox at end of turn: {e}")

    # ── Clear active tool registry ──
    from ..workspace.files import clear_active_tool_registry
    clear_active_tool_registry()

    yield sse_event("done", {"stopped": cancelled})
