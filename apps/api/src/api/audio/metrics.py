"""Ephemeral, content-free measurements for a single voice turn."""
import asyncio
from dataclasses import dataclass, field


@dataclass
class VoiceMetrics:
    resolved_model: str | None = None
    reasoning_effort: str | None = None
    tool_rounds: int = 0
    thinking_chunks: int = 0
    tts_connect_ms: int | None = None
    tts_reopen: int = 0
    model_source: str = 'unresolved'
    catalog_wait_ms: int = 0
    catalog_wait_started: float | None = None
    model_ms: float = 0
    tool_ms: float = 0
    model_started: float | None = None
    tool_started_at: float | None = None
    tool_started: asyncio.Event = field(default_factory=asyncio.Event)
    tool_category: str = 'general'
    logged: bool = False
