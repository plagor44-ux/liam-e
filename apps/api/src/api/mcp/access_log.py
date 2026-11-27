"""Prevent the ASGI server's access log from recording read-facade query strings.

Gateway and reverse-proxy logging must independently apply the same policy before exposure.
"""

import logging
import re

PREFIXES = ("/api/agent-read/", "/api/mcp")
# Everything from the "?" up to the next whitespace: a request target never contains raw
# whitespace, and quotes inside the query must not end the redaction early.
_QUERY = re.compile(r"(/api/(?:agent-read/|mcp(?:/|(?=\?)))[^\s?]*)\?\S*")


def _redact(text: str) -> str:
    return _QUERY.sub(r"\1", text)


class RedactAgentReadQuery(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            # Keep the argument shape (uvicorn's formatter unpacks its 5-tuple) and redact every
            # facade path among the arguments, wherever a format puts it.
            record.args = tuple(
                _redact(item) if isinstance(item, str) and any(prefix in item for prefix in PREFIXES) else item
                for item in args
            )
        # Then check the rendered line: a query can still appear when a format splits path and
        # query into separate arguments, carries the path in the message itself or passes a
        # mapping. Only such lines lose their arguments; uvicorn's own line no longer matches.
        try:
            message = record.getMessage()
        except Exception:
            return True
        if _QUERY.search(message):
            record.msg, record.args = _redact(message), None
        return True


def install() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, RedactAgentReadQuery) for item in logger.filters):
        logger.addFilter(RedactAgentReadQuery())
