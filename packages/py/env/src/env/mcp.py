"""Settings for the native MCP Streamable HTTP transport (``/api/mcp``)."""

from env._base import BaseEnv


class Env(BaseEnv):
    # Additional allowed Host and Origin header values (comma-separated).
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""

    # Transport bounds for the stateless read-only endpoint.
    mcp_max_body_bytes: int = 65_536
    mcp_call_timeout_seconds: float = 15.0
    mcp_rate_limit_requests: int = 60
    mcp_rate_limit_window_seconds: float = 60.0


env = Env()
