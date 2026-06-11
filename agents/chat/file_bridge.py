"""
WebSocket File Bridge — Persistent IDB ↔ Ephemeral Sandbox sync.

Replaces the broken KV-as-bridge pattern with a direct WebSocket connection
between the frontend (IndexedDB = source of truth) and the sandbox container.

Protocol:
  Frontend → Sandbox:
    {"type": "file_write", "path": "...", "content": "..."}
    {"type": "file_delete", "path": "..."}
    {"type": "file_list_request"}
    {"type": "sync_all", "files": {"path": "content", ...}}

  Sandbox → Frontend:
    {"type": "file_write", "path": "...", "content": "..."}
    {"type": "file_delete", "path": "..."}
    {"type": "file_list_response", "files": {"path": "content", ...}}
    {"type": "sync_ack", "count": N}
    {"type": "error", "message": "..."}

Flow:
  1. Frontend connects via WebSocket after auth
  2. Frontend sends full IDB state as initial sync_all
  3. Sandbox tools write locally → push changes back via file_write
  4. Frontend receives changes → saves to IDB → updates sidebar
  5. On reconnect, frontend re-syncs full IDB state
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .._logger import create_logger

logger = create_logger("file_bridge")


class FileBridge:
    """Manages WebSocket connections for file sync between frontend and sandbox."""

    def __init__(self) -> None:
        self._connections: dict[str, Any] = {}  # cid -> websocket
        self._file_caches: dict[str, dict[str, str]] = {}  # cid -> files

    def register(self, cid: str, ws: Any) -> None:
        """Register a WebSocket connection for a conversation."""
        self._connections[cid] = ws
        if cid not in self._file_caches:
            self._file_caches[cid] = {}
        logger.log(f"[bridge] Registered connection for {cid}")

    def unregister(self, cid: str) -> None:
        """Unregister a WebSocket connection."""
        self._connections.pop(cid, None)
        logger.log(f"[bridge] Unregistered connection for {cid}")

    def is_connected(self, cid: str) -> bool:
        """Check if a conversation has an active WebSocket connection."""
        return cid in self._connections

    async def send_to_frontend(self, cid: str, message: dict[str, Any]) -> bool:
        """Send a message to the frontend via WebSocket."""
        ws = self._connections.get(cid)
        if not ws:
            return False
        try:
            await ws.send(json.dumps(message))
            return True
        except Exception as e:
            logger.log(f"[bridge] Failed to send to {cid}: {e}")
            self.unregister(cid)
            return False

    async def push_file_write(self, cid: str, path: str, content: str) -> bool:
        """Push a file write from sandbox to frontend (for IDB persistence)."""
        # Update cache
        if cid in self._file_caches:
            self._file_caches[cid][path] = content
        return await self.send_to_frontend(cid, {
            "type": "file_write",
            "path": path,
            "content": content,
        })

    async def push_file_delete(self, cid: str, path: str) -> bool:
        """Push a file deletion from sandbox to frontend."""
        if cid in self._file_caches:
            self._file_caches[cid].pop(path, None)
        return await self.send_to_frontend(cid, {
            "type": "file_delete",
            "path": path,
        })

    async def push_full_sync(self, cid: str, files: dict[str, str]) -> bool:
        """Push full file state to frontend."""
        if cid in self._file_caches:
            self._file_caches[cid] = dict(files)
        return await self.send_to_frontend(cid, {
            "type": "sync_all",
            "files": files,
            "count": len(files),
        })

    def get_cached_files(self, cid: str) -> dict[str, str]:
        """Get cached files for a conversation."""
        return dict(self._file_caches.get(cid, {}))

    def update_cache(self, cid: str, files: dict[str, str]) -> None:
        """Update the file cache from frontend sync."""
        if cid not in self._file_caches:
            self._file_caches[cid] = {}
        self._file_caches[cid].update(files)


# Singleton instance
_bridge = FileBridge()


def get_bridge() -> FileBridge:
    """Get the global FileBridge instance."""
    return _bridge


async def handle_websocket(context: Any) -> None:
    """Handle WebSocket connection for file bridge.

    This is the EdgeOne entry point for the /ws/file-bridge route.
    """
    cid = context.conversation_id
    ws = context.websocket

    bridge = get_bridge()
    bridge.register(cid, ws)

    try:
        # Send initial acknowledgment
        await ws.send(json.dumps({
            "type": "connected",
            "conversation_id": cid,
        }))

        # Listen for messages from frontend
        async for raw_message in ws:
            try:
                msg = json.loads(raw_message)
                msg_type = msg.get("type")

                if msg_type == "sync_all":
                    # Frontend sends full IDB state
                    files = msg.get("files", {})
                    bridge.update_cache(cid, files)
                    logger.log(f"[bridge] Received sync_all from {cid}: {len(files)} files")
                    await ws.send(json.dumps({
                        "type": "sync_ack",
                        "count": len(files),
                    }))

                elif msg_type == "file_write":
                    # Frontend sends a single file update
                    path = msg.get("path", "")
                    content = msg.get("content", "")
                    if path:
                        bridge.update_cache(cid, {path: content})
                        logger.log(f"[bridge] Received file_write from {cid}: {path}")

                elif msg_type == "file_delete":
                    # Frontend deletes a file
                    path = msg.get("path", "")
                    if path and cid in bridge._file_caches:
                        bridge._file_caches[cid].pop(path, None)
                        logger.log(f"[bridge] Received file_delete from {cid}: {path}")

                elif msg_type == "file_list_request":
                    # Frontend requests current file list
                    files = bridge.get_cached_files(cid)
                    await ws.send(json.dumps({
                        "type": "file_list_response",
                        "files": files,
                    }))

                elif msg_type == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

                else:
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type}",
                    }))

            except json.JSONDecodeError:
                await ws.send(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON",
                }))
            except Exception as e:
                logger.log(f"[bridge] Error processing message: {e}")

    except Exception as e:
        logger.log(f"[bridge] Connection error for {cid}: {e}")
    finally:
        bridge.unregister(cid)
