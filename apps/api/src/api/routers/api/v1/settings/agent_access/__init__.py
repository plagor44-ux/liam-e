"""The owner's agent access switch; reading and turning off need only the gateway (#197)."""

from fastapi import params

segment = "agent-access"
dependencies: tuple[params.Depends, ...] = ()
