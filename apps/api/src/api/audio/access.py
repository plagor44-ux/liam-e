"""Server-side owner verification boundary for browser audio.

The check itself lives in :mod:`api.owner_access`, shared with the agent access switch.
"""

from typing import Annotated

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection

from api.owner_access import OwnerVerifier, StudioOwner as _StudioOwner, verify_studio_owner as _verify_studio_owner

AudioOwnerVerifier = OwnerVerifier

__all__ = ["AudioOwnerVerifier", "_StudioOwner", "get_audio_owner_verifier", "require_audio_owner"]


def get_audio_owner_verifier() -> AudioOwnerVerifier:
    return _verify_studio_owner


async def require_audio_owner(
    request: HTTPConnection,
    verify: Annotated[AudioOwnerVerifier, Depends(get_audio_owner_verifier)],
) -> None:
    if await verify(request) is not True:
        raise HTTPException(status_code=401, detail="unauthorized", headers={"Cache-Control": "no-store"})
