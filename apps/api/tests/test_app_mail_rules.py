"""Priority and matching for global app-side mail rules."""

from db.models.mail_message import MailMessage
from db.models.mail_rule import MailRule
from api.mail.rules import select_assignments
from api.mail.rule_schemas import RuleIn
from pydantic import ValidationError
import pytest


def test_rule_requires_app_side_destination_even_with_provider_action():
    with pytest.raises(ValidationError):
        RuleIn(name="Read", sender_domain="example.test", mark_read=True)


def test_first_folder_wins_but_labels_accumulate_across_all_matches():
    message = MailMessage(id="m", account_id="a", provider_uid="uid", sender_address="ada@example.test", subject="Hello")
    rules = [
        MailRule(name="second", position=20, sender_domain="example.test", folder_id="folder-b", label_ids=["label-b", "label-a"]),
        MailRule(name="first", position=10, sender_address="ada@example.test", folder_id="folder-a", label_ids=["label-a", "label-c"]),
        MailRule(name="other account", position=1, account_id="b", folder_id="wrong"),
    ]
    result = select_assignments(rules, message)
    assert result.folder_id == "folder-a"
    assert result.label_ids == ["label-a", "label-c", "label-b"]


def test_conditions_do_not_cross_accounts_or_match_domain_suffixes():
    message = MailMessage(account_id="a", provider_uid="uid", sender_address="ada@sub.example.test", subject="Status report", recipients_json='["Team <team@example.test>"]')
    rules = [
        MailRule(name="wrong account", account_id="b", folder_id="a"),
        MailRule(name="wrong domain", sender_domain="example.test", folder_id="b"),
        MailRule(name="all conditions", account_id="a", sender_domain="sub.example.test", subject_contains="REPORT", recipient="team@example.test", folder_id="c"),
    ]
    assert select_assignments(rules, message).folder_id == "c"


def test_rule_deduplicates_labels_and_limits_the_list():
    rule = RuleIn(name="Labels", sender_domain="example.org", label_ids=["a", "b", "a"])
    assert rule.label_ids == ["a", "b"]
    with pytest.raises(ValidationError):
        RuleIn(name="Labels", sender_domain="example.org", label_ids=[str(i) for i in range(21)])
    with pytest.raises(ValidationError):
        RuleIn(name="Labels", sender_domain="example.org", label_ids=[""])


def test_existing_rule_proposal_keeps_its_label():
    import json
    from api.mail.proposals import proposal_view
    from db.models.chat_message import ChatMessage
    proposal = {"kind": "rule", "reason": "", "items": [], "rule": {
        "name": "Legacy", "sender_domain": "example.org", "label_id": "legacy"}}
    card = ChatMessage(conversation_id="synthetic", role="proposal", proposal_json=json.dumps(proposal))
    assert proposal_view(card).rule.label_ids == ["legacy"]
