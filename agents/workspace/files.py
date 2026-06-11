"""
Workspace files API endpoint -- EdgeOne Makers
========================================

File path agents/workspace/files.py maps to **POST /workspace/files**
"""

import json
from typing import Any
from pathlib import Path
import inspect

from .._logger import create_logger
from ..chat.index import load_workspace_files, save_workspace_files

logger = create_logger("workspace_files")

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

    elif action == "write":
        if not filename:
            return {"error": "filename is required for write action"}
        if content is None:
            return {"error": "content is required for write action"}
            
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
