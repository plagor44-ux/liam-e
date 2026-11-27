"""Settings for the native MCP Streamable HTTP transport (``/api/mcp``).

``MCP_STUDIO_OWNER`` is intentionally a per-deployment environment variable rather than a
``WorkspaceSecret`` so a test deployment and a production deployment in the same Studio workspace
cannot share an owner binding implicitly.
"""

from env._base import BaseEnv


class Env(BaseEnv):
    # Per-deployment owner binding JSON: {"user_id": ..., "organization_id": ..., "workspace_id": ...}.
    # Unset or invalid leaves DenyReadAccess bound (fail closed).
    mcp_studio_owner: str | None = None

    # Additional allowed Host and Origin header values (comma-separated).
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""

    # Transport bounds for the stateless read-only endpoint.
    mcp_max_body_bytes: int = 65_536
    mcp_call_timeout_seconds: float = 15.0
    mcp_rate_limit_requests: int = 60
    mcp_rate_limit_window_seconds: float = 60.0


env = Env()
