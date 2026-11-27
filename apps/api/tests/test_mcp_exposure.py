"""What may become a Studio MCP tool.

Deployed apps are not turned into MCP tools automatically; importing the served OpenAPI as a
Studio HTTP connector does that, and every operation then becomes one tool named after its
operation id. There is no per-operation exclusion in the spec, so the surface itself decides:
only reading GETs may reach an agent, and every changing operation has to be registered here on
purpose — the inventory test fails on any new one until someone names it.
"""

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from mistralai_capabilities.fastapi.openapi import HTTP_METHODS

from api.mcp.exposure import BROWSER_ONLY_GET_OPERATIONS, read_only_get_operations
from conftest import build_app

# Every non-GET operation of the served app. Keep this set exact: adding an operation means
# deciding that it must not be reachable from a Studio agent.
MUTATING_OPERATIONS: frozenset[str] = frozenset(
    {
        "audio_transcribe",
        "audio_speech",
        "audio_voice_preview",
        "audio_live_status",
        "audio_live_speech",
        "audio_device_test_inference",
        "audio_device_test_record",
        "calendar_google_connect",
        "calendar_event_create",
        "calendar_event_update",
        "calendar_event_delete",
        "calendar_write_check",
        "calendar_feed_validate",
        "calendar_feed_subscribe",
        "calendar_feed_validation_discard",
        "calendar_source_delete",
        "calendar_source_sync",
        "calendar_source_update",
        "preference_create",
        "preference_candidate_propose",
        "preference_update",
        "preference_delete",
        "settings_update",
        "prompt_create",
        "prompt_update",
        "prompt_delete",
        "skill_create",
        "skill_update",
        "skill_delete",
        "chat_conversation_update",
        "chat_proposal_cancel",
        "chat_proposal_confirm",
        "chat_proposal_check",
        "chat_stream",
        "chat_turn",
        "mail_account_connect_connector",
        "mail_account_connect_imap",
        "mail_account_delete",
        "mail_account_retry_provider_actions",
        "mail_account_sync",
        "mail_account_update",
        "mail_actions_undo",
        "mail_folder_create",
        "mail_folder_create_and_assign",
        "mail_folder_update",
        "mail_folder_delete",
        "mail_filing_assign",
        "mail_rule_create",
        "mail_rule_create_from_message",
        "mail_rule_update",
        "mail_rule_delete",
        "mail_rule_apply",
        "mail_rules_preview",
        "mail_message_action",
        "mail_messages_bulk_action",
        "mail_sync_all",
        "settings_ui_update",
        "settings_ui_browser_locale_confirm",
        "chat_settings_update",
        "chat_model_settings_update",
        "calendar_settings_update",
        "mcp_settings_update",
        "agent_access_disable",  # The owner's switch for outside agent access (#197).
        "agent_access_enable",
        "tasks_archive",
        "tasks_create",
        "tasks_delete",
        "tasks_preview",
        "tasks_reconcile",
        "tasks_update",
        "tasks_occurrence_complete",
        "tasks_occurrence_reopen",
        "tasks_occurrence_skip",
    }
)

AGENTS_MD = Path(__file__).resolve().parents[3] / "AGENTS.md"
READ_ONLY_LIST_MARKER = "read-only operations:"


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return build_app()


def _operations(app: FastAPI) -> list[tuple[str, str]]:
    """``(method, operation id)`` for every operation in the served OpenAPI."""
    operations: list[tuple[str, str]] = []
    for path_item in app.openapi()["paths"].values():
        for method, operation in path_item.items():
            if method.lower() in HTTP_METHODS:
                operations.append((method.upper(), operation["operationId"]))
    return operations


def _mutating_ids(app: FastAPI) -> set[str]:
    return {operation_id for method, operation_id in _operations(app) if method != "GET"}


def _read_only_get_ids(app: FastAPI) -> set[str]:
    return {
        operation_id
        for method, operation_id in _operations(app)
        if method == "GET" and operation_id not in BROWSER_ONLY_GET_OPERATIONS
    }


def test_agent_read_operations_are_explicit_gets(app: FastAPI) -> None:
    """The separate facade adds exactly the curated read operations, never a mutation."""
    from api.mcp.registry import OPERATIONS

    expected = {(operation.method, operation.operation_id) for operation in OPERATIONS}
    served = {(method, operation_id) for method, operation_id in _operations(app)
              if operation_id.startswith("liam_mail_")}
    assert served == expected
    assert len(expected) == 4 and all(method == "GET" for method, _ in expected)


def test_mutating_operation_inventory(app: FastAPI) -> None:
    """Every operation that is not a GET is registered in MUTATING_OPERATIONS."""
    served = _mutating_ids(app)
    added = sorted(served - MUTATING_OPERATIONS)
    removed = sorted(MUTATING_OPERATIONS - served)
    assert not added and not removed, (
        f"new mutating operations: {added}; gone: {removed}. "
        "Register the operation in MUTATING_OPERATIONS in apps/api/tests/test_mcp_exposure.py "
        "and update the read-only rule in AGENTS.md (section Security)."
    )


def test_agents_md_read_only_list_matches_get_operations(app: FastAPI) -> None:
    """AGENTS.md names exactly the reading GETs as the surface that may be imported."""
    lines = [
        line for line in AGENTS_MD.read_text(encoding="utf-8").splitlines()
        if READ_ONLY_LIST_MARKER in line
    ]
    assert len(lines) == 1, (
        f"expected exactly one AGENTS.md line with '{READ_ONLY_LIST_MARKER}', found {len(lines)}"
    )
    listed = re.findall(r"`([^`]+)`", lines[0].split(READ_ONLY_LIST_MARKER, 1)[1])
    assert listed == sorted(_read_only_get_ids(app)), (
        "AGENTS.md lists the read-only operations differently from the served OpenAPI: "
        f"missing {sorted(_read_only_get_ids(app) - set(listed))}, "
        f"unexpected {sorted(set(listed) - _read_only_get_ids(app))}, "
        "or out of alphabetical order. Update the rule in AGENTS.md (section Security)."
    )


def test_runtime_read_only_set_matches_openapi_inventory(app: FastAPI) -> None:
    """The runtime x-api-key policy set uses the same single source of truth as AGENTS.md."""
    assert read_only_get_operations(app) == _read_only_get_ids(app)
