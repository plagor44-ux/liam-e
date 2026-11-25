"""Synthetic app that probes the Studio MCP-route sign-in (#157).

Test-only and deliberately empty of data: it holds an in-memory counter and nothing else. It calls
no model, connector or mail provider, and it never stores or logs a token. The only outgoing call
is the identity lookup at the gateway proxy, made with the caller token of the request being
served. The MCP endpoint is a hand-written stub for probing the gateway; the product transport in
#36 uses the pinned MCP SDK instead.

Runbook: docs/superpowers/probes/2026-11-25-mcp-route-sign-in.md
"""
