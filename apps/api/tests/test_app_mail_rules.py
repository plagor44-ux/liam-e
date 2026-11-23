"""Priority and matching for global app-side mail rules."""

from db.models.mail_message import MailMessage
from db.models.mail_rule import MailRule
from api.mail.rules import select_assignments


def test_first_folder_wins_but_labels_accumulate_across_all_matches():
    message = MailMessage(id="m", account_id="a", provider_uid="uid", sender_address="ada@example.test", subject="Hello")
    rules = [
        MailRule(name="second", position=20, sender_domain="example.test", folder_id="folder-b", label_id="label-b"),
        MailRule(name="first", position=10, sender_address="ada@example.test", folder_id="folder-a", label_id="label-a"),
        MailRule(name="other account", position=1, account_id="b", folder_id="wrong"),
    ]
    result = select_assignments(rules, message)
    assert result.folder_id == "folder-a"
    assert set(result.label_ids) == {"label-a", "label-b"}


def test_conditions_do_not_cross_accounts_or_match_domain_suffixes():
    message = MailMessage(account_id="a", provider_uid="uid", sender_address="ada@sub.example.test", subject="Status report", recipients_json='["Team <team@example.test>"]')
    rules = [
        MailRule(name="wrong account", account_id="b", folder_id="a"),
        MailRule(name="wrong domain", sender_domain="example.test", folder_id="b"),
        MailRule(name="all conditions", account_id="a", sender_domain="sub.example.test", subject_contains="REPORT", recipient="team@example.test", folder_id="c"),
    ]
    assert select_assignments(rules, message).folder_id == "c"
