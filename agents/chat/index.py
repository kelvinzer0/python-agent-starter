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

import httpx

from .._model import MODEL_CONFIG, ssl_verify
from .._logger import create_logger
from .._session import ChatSession
from .._tools import build_tools, ToolRegistry, _stringify_result
from ._stream import LlmRoundResult, sse_event, stream_llm_round, safe_json_preview
from ._images import extract_images_from_tool_result


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
    Uses base64-encoded zip file sync to make it fast and support subdirectories.
    """
    # 1. Check if SKILL.md already exists in sandbox (e.g. /skills/onboard/SKILL.md)
    exists = await sandbox_read_file(tool_registry, "/skills/onboard/SKILL.md")
    if exists is not None:
        logger.log("[skills] Skills already exist in sandbox. Skipping sync.")
        return

    # 2. Package local skills directory into a zip in memory
    project_root = Path(__file__).resolve().parent.parent.parent
    skills_dir = project_root / "skills"
    if not skills_dir.exists():
        skills_dir = Path.cwd() / "skills"
    if not skills_dir.exists():
        logger.log("[skills] Local skills folder not found!")
        return

    logger.log("[skills] Packaging skills folder...")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for file_path in skills_dir.glob("**/*"):
            if file_path.is_file():
                if "node_modules" in file_path.parts:
                    continue
                relative_path = file_path.relative_to(skills_dir)
                zip_file.write(file_path, relative_path)
    
    zip_bytes = zip_buffer.getvalue()
    import base64
    zip_b64 = base64.b64encode(zip_bytes).decode("utf-8")
    
    # 3. Write base64 content to /tmp/skills.zip.b64 in sandbox
    logger.log("[skills] Writing b64 zip to sandbox...")
    await sandbox_write_file(tool_registry, "/tmp/skills.zip.b64", zip_b64)
    
    # 4. Write extract script to /tmp/extract.py in sandbox
    extract_script = """
import base64
import zipfile
import os

try:
    with open("/tmp/skills.zip.b64", "r") as f:
        b64_data = f.read()
    zip_data = base64.b64decode(b64_data)
    with open("/tmp/skills.zip", "wb") as f:
        f.write(zip_data)
        
    os.makedirs("/skills", exist_ok=True)
    with zipfile.ZipFile("/tmp/skills.zip", "r") as z:
        z.extractall("/skills")
    print("SUCCESS")
except Exception as e:
    print("ERROR:", str(e))
"""
    await sandbox_write_file(tool_registry, "/tmp/extract.py", extract_script)
    
    # 5. Execute extract script in sandbox
    logger.log("[skills] Extracting skills zip inside sandbox...")
    res = await run_sandbox_command(tool_registry, "python3 /tmp/extract.py")
    logger.log(f"[skills] Extract result: {res}")
    
    # 6. Clean up temporary files in sandbox
    await run_sandbox_command(tool_registry, "rm -f /tmp/skills.zip.b64 /tmp/skills.zip /tmp/extract.py")


# ── Workspace Persistence & Synchronization ──

async def load_workspace_files(context: Any) -> dict[str, str]:
    """Load workspace files from context.store KV storage.
    Falls back to project templates if store is empty.
    """
    cid = context.conversation_id
    store = context.store
    store_key = f"workspace_files_{cid}"
    files_dict = None

    try:
        if hasattr(store, "get"):
            res = store.get(store_key)
            if inspect.isawaitable(res):
                res = await res
            if res and isinstance(res, dict):
                files_dict = res
    except Exception as e:
        logger.log(f"[workspace] Failed to get files from store: {e}")
        
    if files_dict is not None:
        logger.log(f"[workspace] Loaded {len(files_dict)} files from store for {cid}")
        return files_dict

    logger.log(f"[workspace] No files in store for {cid}. Loading default templates.")
    files_dict = {}
    
    project_root = Path(__file__).resolve().parent.parent.parent
    workspace_dir = project_root / "workspace"
    if not workspace_dir.exists():
        workspace_dir = Path.cwd() / "workspace"
        
    if workspace_dir.exists() and workspace_dir.is_dir():
        filenames = [
            "BOOTSTRAP.md",
            "IDENTITY.md",
            "USER.md",
            "SOUL.md",
            "AGENTS.md",
            "TOOLS.md",
            "HEARTBEAT.md"
        ]
        for name in filenames:
            filepath = workspace_dir / name
            if filepath.exists() and filepath.is_file():
                try:
                    content = filepath.read_text(encoding="utf-8").strip()
                    files_dict[name] = content
                except Exception:
                    pass
    return files_dict


async def sync_workspace_to_sandbox(tool_registry: ToolRegistry, files_dict: dict[str, str]) -> None:
    """Initialize workspace files inside the stateless sandbox container under /workspace/."""
    for filename, content in files_dict.items():
        sandbox_path = f"/workspace/{filename}"
        success = await sandbox_write_file(tool_registry, sandbox_path, content)
        if success:
            logger.log(f"[workspace] Synced {filename} to sandbox path {sandbox_path}")
        else:
            logger.log(f"[workspace] Failed to sync {filename} to sandbox")


async def sync_workspace_from_sandbox(context: Any, tool_registry: ToolRegistry) -> None:
    """Read updated workspace files from the sandbox and save them back to context.store KV."""
    cid = context.conversation_id
    store = context.store
    store_key = f"workspace_files_{cid}"
    
    filenames = [
        "BOOTSTRAP.md",
        "IDENTITY.md",
        "USER.md",
        "SOUL.md",
        "AGENTS.md",
        "TOOLS.md",
        "HEARTBEAT.md"
    ]
    
    updated_files = {}
    for name in filenames:
        sandbox_path = f"/workspace/{name}"
        content = await sandbox_read_file(tool_registry, sandbox_path)
        if content is not None:
            updated_files[name] = content
            logger.log(f"[workspace] Read updated {name} from sandbox")
        else:
            # File missing = deleted (e.g. BOOTSTRAP.md deleted by agent)
            logger.log(f"[workspace] {name} is missing in sandbox (deleted or not created)")
            
    try:
        if hasattr(store, "put"):
            res = store.put(store_key, updated_files)
            if inspect.isawaitable(res):
                await res
            logger.log(f"[workspace] Saved updated workspace back to store for {cid}")
        elif hasattr(store, "set"):
            res = store.set(store_key, updated_files)
            if inspect.isawaitable(res):
                await res
            logger.log(f"[workspace] Saved updated workspace back to store for {cid}")
    except Exception as e:
        logger.log(f"[workspace] Failed to save updated workspace to store: {e}")


def get_available_skills() -> list[str]:
    """Retrieve all subdirectory names inside the local project skills directory."""
    project_root = Path(__file__).resolve().parent.parent.parent
    skills_dir = project_root / "skills"
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
                    
                # Execute tool calls
                for tc in tool_calls:
                    tc_name = tc["function"]["name"]
                    tc_args = tc["function"]["arguments"]
                    tc_id = tc["id"]
                    
                    logger.log(f"[subagent] Sub-agent calling tool: {tc_name}")
                    tool_result = await tool_registry.execute(tc_name, tc_args)
                    
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tool_result
                    })
                    
            return messages[-1].get("content") or "Sub-agent execution exceeded maximum rounds."
            
    except Exception as e:
        logger.error(f"[subagent] Failed during execution: {e}")
        return f"Sub-agent execution failed: {str(e)}"


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

                # Emit tool_called events and tool_debug call phase
                for tc in tool_calls:
                    yield sse_event("tool_called", {"tool": tc["name"]})
                    yield sse_event("tool_debug", {
                        "phase": "call",
                        "tool": tc["name"],
                        "id": tc["id"],
                        "argumentsPreview": safe_json_preview(tc["arguments"], 1200),
                    })

                # ── Tracer: tool execution spans ──
                tool_spans = []
                for tc in tool_calls:
                    ts = context.tracer.start_span(f"tool.{tc['name']}", {
                        "tool.name": tc["name"],
                        "tool.call_id": tc["id"],
                        "tool.arguments_length": len(tc["arguments"]),
                    })
                    tool_spans.append(ts)

                try:
                    results = []
                    for tc_item in tool_calls:
                        started_at = time.perf_counter()

                        # Pull the RAW handler value so we can sniff for base64
                        # images BEFORE serialization. Anything we find is
                        # replaced with a `[image:<id>]` placeholder; the
                        # redacted structure is what flows back into the
                        # model context (next chat-completions round).
                        raw = await tool_registry.execute_raw(
                            tc_item["name"], tc_item["arguments"]
                        )
                        extraction = extract_images_from_tool_result(raw)
                        result = _stringify_result(extraction.redacted_result)
                        duration_ms = int((time.perf_counter() - started_at) * 1000)
                        results.append(result)

                        # SSE ordering contract: image events fire AFTER
                        # tool_debug{phase:'call'} (already emitted at line
                        # ~233 above) and BEFORE tool_debug{phase:'result'}.
                        # The frontend uses this to attach images to the
                        # in-flight tool row.
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
                    for ts, result in zip(tool_spans, results):
                        ts.set_attributes({"tool.result_length": len(result)})
                finally:
                    for ts in tool_spans:
                        ts.end()

                for tc, tool_result in zip(tool_calls, results):
                    logger.log(f"[tool] {tc['name']}: {tool_result[:200]}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })

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

    # ── Workspace: save updated files back to context.store KV ──
    if 'tool_registry' in locals() and tool_registry:
        await sync_workspace_from_sandbox(context, tool_registry)

    yield sse_event("done", {"stopped": cancelled})
