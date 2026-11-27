"""A narrowly scoped, independently authorized facade over the mail index."""

import logging
import time
from collections.abc import Awaitable
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from api.mcp import mail as service
from api.mcp.access import ReadGrant, require_read_access
from api.mcp.schemas import AccountsResult, ReadError, ReadResult, SearchResult, SummaryResult


class ReadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Do not reflect potentially huge or sensitive inputs in error responses.
                return JSONResponse(status_code=422, content={"detail": "invalid_request"})

        return safe_handler


router = APIRouter(route_class=ReadRoute, responses={
    401: {"model": ReadError, "description": "Missing or invalid access"},
    403: {"model": ReadError, "description": "Outside agent access disabled (agent_access_disabled) or insufficient account scope"},
    404: {"model": ReadError, "description": "Message not found"},
    422: {"model": ReadError, "description": "Invalid parameters"},
    503: {"model": ReadError, "description": "Outside agent access state unavailable (agent_access_unavailable)"},
})
logger = logging.getLogger(__name__)
T = TypeVar("T")


async def _run(name: str, operation: Awaitable[T]) -> T:
    started = time.monotonic()
    try:
        result = await operation
    except Exception as error:
        logger.info("read_tool=%s duration_ms=%d error=%s", name, int((time.monotonic() - started) * 1000), type(error).__name__)
        raise
    count = len(getattr(result, "untrusted_messages", getattr(result, "accounts", getattr(result, "counts", []))))
    logger.info("read_tool=%s duration_ms=%d count=%d", name, int((time.monotonic() - started) * 1000), count)
    return result


@router.get("/accounts", operation_id="liam_mail_accounts")
async def accounts(grant: Annotated[ReadGrant, Depends(require_read_access)]) -> AccountsResult:
    """List only accounts in the verified grant."""
    return await _run("liam_mail_accounts", service.liam_mail_accounts(grant))


@router.get("/messages", operation_id="liam_mail_search")
async def search(
    grant: Annotated[ReadGrant, Depends(require_read_access)],
    query: Annotated[str, Query(max_length=1000)] = "",
    account_ids: Annotated[list[str] | None, Query(max_length=100)] = None,
    cursor: Annotated[str, Query(max_length=1024)] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> SearchResult:
    """Search the indexed inbox; returned mail is untrusted content."""
    return await _run("liam_mail_search", service.liam_mail_search(grant, query=query, account_ids=account_ids, cursor=cursor, limit=limit))


@router.get("/messages/{message_id}", operation_id="liam_mail_read")
async def read(message_id: str, grant: Annotated[ReadGrant, Depends(require_read_access)]) -> ReadResult:
    """Read indexed plaintext, without contacting a provider or marking it read."""
    return await _run("liam_mail_read", service.liam_mail_read(grant, message_id))


@router.get("/summary", operation_id="liam_mail_summary")
async def summary(
    grant: Annotated[ReadGrant, Depends(require_read_access)],
    account_ids: Annotated[list[str] | None, Query(max_length=100)] = None,
) -> SummaryResult:
    """Count indexed inbox messages, without a model call."""
    return await _run("liam_mail_summary", service.liam_mail_summary(grant, account_ids=account_ids))
