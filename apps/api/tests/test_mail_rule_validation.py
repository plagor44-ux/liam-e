"""Both rule-creation routes reject missing app-side destinations."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers.api.v1.mail.rules import router


def test_rule_creation_requires_a_folder_or_label_on_both_routes():
    app = FastAPI()
    app.include_router(router, prefix="/rules")
    client = TestClient(app)
    body = {"name": "Example", "sender_domain": "example.org", "mark_read": True}
    assert client.post("/rules", json=body).status_code == 422
    assert client.post("/rules/from-message", json=body | {
        "source_message_id": "synthetic", "source_account_id": "synthetic",
    }).status_code == 422
