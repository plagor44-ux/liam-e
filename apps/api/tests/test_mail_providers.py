"""IMAP preset lookup and the preset part of the mail host allowlist; no sockets, no DNS."""

import socket

import pytest

from api.mail import providers, transport

GMAIL = ("imap.gmail.com", 993, "smtp.gmail.com", 465, False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a preset test opened a socket or resolved DNS")

    for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, refuse)


@pytest.mark.parametrize("address", ["me@gmail.com", "me@GMAIL.com", "me@Gmail.Com"])
def test_guess_imap_settings_returns_gmail_preset_ignoring_case(address):
    assert providers.guess_imap_settings(address) == GMAIL


def test_googlemail_shares_gmail_servers():
    assert providers.guess_imap_settings("me@googlemail.com") == providers.guess_imap_settings("me@gmail.com")


@pytest.mark.parametrize("domain", sorted(providers.IMAP_PRESETS))
def test_guess_imap_settings_returns_preset_for_every_domain(domain):
    assert providers.guess_imap_settings(f"me@{domain}") == providers.IMAP_PRESETS[domain]


@pytest.mark.parametrize("address", ["me@example.org", "me@mail.example.org", "me@mail.gmail.com", "me@gmail.com.example"])
def test_guess_imap_settings_returns_none_without_exact_domain_match(address):
    assert providers.guess_imap_settings(address) is None


def test_allowed_hosts_holds_every_preset_host_in_lower_case(monkeypatch):
    monkeypatch.setattr(transport.mail_env, "mail_imap_allowed_hosts", "")
    expected = {host.lower() for imap, _, smtp, _, _ in providers.IMAP_PRESETS.values() for host in (imap, smtp)}
    assert transport.allowed_hosts() == expected
    assert {"imap.gmail.com", "smtp.gmail.com", "outlook.office365.com", "smtp.office365.com"} <= transport.allowed_hosts()


def test_allowed_hosts_lowercases_preset_hosts(monkeypatch):
    monkeypatch.setattr(transport.mail_env, "mail_imap_allowed_hosts", "")
    monkeypatch.setitem(providers.IMAP_PRESETS, "mail.test", ("IMAP.Mail.TEST", 993, "SMTP.Mail.TEST", 465, False))
    hosts = transport.allowed_hosts()
    assert {"imap.mail.test", "smtp.mail.test"} <= hosts
    assert "IMAP.Mail.TEST" not in hosts


def test_allowed_hosts_excludes_unknown_host(monkeypatch):
    monkeypatch.setattr(transport.mail_env, "mail_imap_allowed_hosts", "")
    assert "imap.example.org" not in transport.allowed_hosts()
