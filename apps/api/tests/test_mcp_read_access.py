"""The future read surface must fail closed and never expand a verified grant."""

from unittest.mock import AsyncMock

from db.accessors import agent_access
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.mcp.access import ReadGrant, get_read_access_verifier, require_read_access, restrict_accounts
from api.mcp.registry import OPERATIONS


def test_inventory_is_explicit_and_unique():
    assert {operation.operation_id for operation in OPERATIONS} == {
        "liam_mail_accounts", "liam_mail_search", "liam_mail_read", "liam_mail_summary"
    }
    assert len({operation.path for operation in OPERATIONS}) == 4
    assert all(operation.method == "GET" for operation in OPERATIONS)


def test_access_defaults_to_denial_and_selection_cannot_expand(monkeypatch):
    monkeypatch.setattr(agent_access, "is_enabled", AsyncMock(return_value=True))
    app = FastAPI()

    @app.get("/test")
    async def endpoint(account_id: str | None = None, grant: ReadGrant = Depends(require_read_access)):
        return sorted(restrict_accounts(grant, [account_id] if account_id else None))

    client = TestClient(app)
    assert client.get("/test").status_code == 401

    class TestVerifier:
        async def verify(self, request):
            return ReadGrant(frozenset({"account-a", "account-b"}))

    app.dependency_overrides[get_read_access_verifier] = TestVerifier
    assert client.get("/test").json() == ["account-a", "account-b"]
    assert client.get("/test", params={"account_id": "account-a"}).json() == ["account-a"]
    assert client.get("/test", params={"account_id": "account-c"}).status_code == 403


def test_empty_selection_is_not_the_whole_grant():
    assert restrict_accounts(ReadGrant(frozenset({"account-a"})), []) == frozenset()
