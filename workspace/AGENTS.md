# AGENTS.md - Standard Operating Procedures

This file defines the operational rules, task management, and constraints governing your execution loops, tool-calling workflows, and sub-agent coordination.

## 1. Multi-Agent Coordination (sessions_spawn)
- **Delegation Rules**:
  - Spawn specialized sub-agents for heavy computations, complex code generation, database debugging, or deep file audits.
  - Keep the main turn responsive. Do not run excessive recursive tasks directly in the main turn; delegate them.
- **Objective Definition**: Before calling `sessions_spawn`, outline a clear, actionable directive, inputs, and expected output formats.
- **Evidence Synthesis**: Treat the response of the sub-agent as evidence. Read, verify, and synthesize it into your final response.

## 2. Workspace & Filesystem Integrity
- **Dual Filesystem Rule**:
  - Always remember you have two distinct filesystems: **Local Workspace** (`local_` tools) and **Stateless Sandbox** (`sandbox_` tools).
  - Project source code, manuals, and workspace documents (`IDENTITY.md`, `USER.md`, etc.) belong in the **Local Workspace**.
  - Temporary files, test script runs, dependency installations, and output checks belong in the **Sandbox Workspace**.
- **No Redundant Sync**: Do not write temporary build files to `local_` files. Use `sandbox_` files to compile or run scripts to keep the user's host machine clean.

## 3. Tool Execution Protocol
- **Wave Execution**: Call tools in parallel waves when tasks are independent, utilizing dependency resolution placeholders.
- **Self-Healing Check**: If a toolcall fails or the sandbox is unresponsive, the sentinel `ensure_sandbox_initialized` will automatically restore files. If you run into persistent terminal failures, explain it to the user.
- **Command Constraints**: Run commands inside `/workspace` in the sandbox. Avoid destructive shell operations (like `rm -rf /`) unless explicitly commanded.

## 4. Onboarding & State Maintenance
- **State Files**: Read and maintain `IDENTITY.md`, `USER.md`, `SOUL.md`, and `TOOLS.md` to persist settings across turns.
- **Bootstrap Protocol**: If `BOOTSTRAP.md` is present in the workspace, execute the onboarding sequence immediately to guide the user in setting up identity files, and delete `BOOTSTRAP.md` once onboarding is complete.
