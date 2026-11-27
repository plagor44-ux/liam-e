"""The versioned API (``/api/v1/...``).

Single-user app served behind the Mistral Studio gateway, which authenticates browser callers.
Callers presenting a workspace key in ``x-api-key`` are treated as outside agents by
``agent_caller_policy``: refused while the owner's switch is off, and limited to the read-only GET
inventory while on. Requests without ``x-api-key`` pass with no database read.
"""

from fastapi import Depends, params

from api.mcp.exposure import agent_caller_policy

segment = "v1"

dependencies: tuple[params.Depends, ...] = (Depends(agent_caller_policy),)
