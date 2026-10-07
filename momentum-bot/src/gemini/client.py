"""Gemini API client wrapper with 10s timeout and 2s retry logic.

Complies with Section 4.1 of the Momentum specification (v17).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional
from google import genai
from google.genai import types

from src.config import Config
from src.gemini.prompts import SYSTEM_PROMPT
from src.gemini.schemas import GeminiResponseSchema

logger = logging.getLogger(__name__)


class GeminiApiError(Exception):
    """Raised when Gemini API call fails after retry or cannot be parsed."""
    pass


class GeminiClient:
    """Wrapper around Google GenAI client with robust retry and structured output enforcement."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.model_name = config.GEMINI_MODEL
        # Initialize GenAI Client with optional api_key
        api_key = config.GEMINI_API_KEY
        http_options = types.HttpOptions(
            timeout=int(10000),
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        self.client = (
            genai.Client(api_key=api_key, http_options=http_options)
            if api_key
            else genai.Client(http_options=http_options)
        )

    async def parse_natural_language(
        self,
        user_prompt: str,
        system_prompt: str = SYSTEM_PROMPT,
        timeout_seconds: float = 10.0,
        retry_wait_seconds: float = 2.0,
    ) -> GeminiResponseSchema:
        """Call Gemini API with structured JSON output, 10s timeout, and 1 retry after 2s wait.

        Specification (Section 4.1):
          - Timeout: 10 seconds
          - Retry: wait 2 seconds, retry 1 time (total max ~22s)
          - JSON Mode with response_mime_type="application/json" and response_schema
        """
        last_error: Optional[Exception] = None

        for attempt in range(1, 3):  # 1st attempt, then 2nd attempt if failed
            try:
                # Wrap SDK call with strict asyncio timeout
                response_text = await asyncio.wait_for(
                    self._call_generate_content(user_prompt, system_prompt),
                    timeout=timeout_seconds,
                )

                # Validate and parse JSON into Pydantic schema
                data = json.loads(response_text)
                parsed = GeminiResponseSchema.model_validate(data)
                return parsed

            except Exception as e:
                last_error = e
                logger.warning(
                    f"Gemini API attempt {attempt} failed: {type(e).__name__} ({e})"
                )
                if attempt == 1:
                    await asyncio.sleep(retry_wait_seconds)

        raise GeminiApiError(
            f"Gemini API call failed after 1 retry: {type(last_error).__name__} ({last_error})"
        ) from last_error

    async def _call_generate_content(
        self,
        user_prompt: str,
        system_prompt: str,
    ) -> str:
        """Internal asynchronous SDK caller."""
        # Using asynchronous SDK client
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=GeminiResponseSchema,
        )

        # Call aio generate_content
        response = await self.client.aio.models.generate_content(
            model=self.model_name,
            contents=user_prompt,
            config=config,
        )

        if not response.text:
            raise ValueError("Gemini returned empty response text")

        return response.text
