"""Ephemeral, content-free measurements for a single voice turn."""
from dataclasses import dataclass


@dataclass
class VoiceMetrics:
    resolved_model: str | None = None
    reasoning_effort: str | None = None
    tool_rounds: int = 0
    thinking_chunks: int = 0
    tts_connect_ms: int | None = None
    tts_reopen: int = 0
    logged: bool = False
