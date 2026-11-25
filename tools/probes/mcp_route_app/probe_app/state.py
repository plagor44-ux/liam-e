"""Per-process state: a random instance ID, a fingerprint salt and the write counter.

Nothing survives a restart. The instance ID tells the probe whether two readings came from the
same process; the salt makes fingerprints comparable within one process and meaningless outside.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field


@dataclass
class ProbeState:
    instance: str = field(default_factory=lambda: secrets.token_hex(6))
    salt: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)
    count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def increment(self) -> int:
        async with self.lock:
            self.count += 1
            return self.count

    def snapshot(self) -> dict[str, object]:
        return {"instance": self.instance, "count": self.count}
