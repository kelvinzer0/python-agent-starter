"""
Models API endpoint -- EdgeOne Makers
========================================

File path agents/models/index.py maps to **GET /models**

Proxies the upstream AI gateway's /models endpoint and returns available
model list as JSON. Handles errors gracefully — returns an empty list
if the gateway is unreachable.
"""

from typing import Any
import os

import httpx

from .._model import MODEL_CONFIG, ssl_verify
from .._logger import create_logger

logger = create_logger("models")


async def handler(context: Any) -> dict[str, Any]:
    """Return available models from the AI gateway."""
    base_url = MODEL_CONFIG["base_url"].rstrip("/")
    api_key = MODEL_CONFIG["api_key"]

    if not base_url or not api_key:
        logger.log("[models] missing AI_GATEWAY_BASE_URL or AI_GATEWAY_API_KEY")
        return {"models": []}

    url = f"{base_url}/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            verify=ssl_verify,
            proxy=None,
        ) as client:
            response = await client.get(url, headers=headers)

            if response.status_code != 200:
                logger.log(f"[models] gateway returned {response.status_code}")
                return {"models": []}

            data = response.json()
            # OpenAI-compatible format: { data: [{ id, owned_by, ... }] }
            raw_models = data.get("data", [])
            models = [
                {"id": m.get("id", ""), "owned_by": m.get("owned_by", "")}
                for m in raw_models
                if isinstance(m, dict) and m.get("id")
            ]
            return {"models": models}

    except (httpx.HTTPError, httpx.StreamError) as e:
        logger.log(f"[models] gateway request failed: {type(e).__name__}: {e}")
        return {"models": []}
    except Exception as e:
        logger.log(f"[models] unexpected error: {type(e).__name__}: {e}")
        return {"models": []}
