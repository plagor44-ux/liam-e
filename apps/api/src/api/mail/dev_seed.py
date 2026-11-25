"""Safety checks for the local development seed."""

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from email.utils import formataddr
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from db.accessors import chat_message as chat_access, dev_seed as seed_access, mail_insight
from db.models.chat_conversation import ChatConversation
from db.models.mail_account import MailAccount
from db.models.mail_message import MailMessage
from db.models.mail_job import MailJob
from db.models.mail_sync_state import MailSyncState
from env.db import env as db_env

from api.mail import newsletters, proposals
from api.mail.agent_tools import unsubscribe_subtitle
from api.mail.chat_schemas import ChatExtrasOut
from api.mail.seed_ids import SEED_NAMESPACE, seed_id

BERLIN = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True)
class SeedResult:
    accounts: int
    mails: int
    unread: int
    insights: int
    conversations: int
    proposals: int


class SeedRefusedError(Exception):
    """The configured database is not an explicitly local host."""


def require_local_database() -> None:
    """Reject remote hosts before any database access without exposing credentials."""
    try:
        url = urlsplit(db_env.database_url)
        host = url.hostname or ""
        # A connection parameter must not override the authority's host.
        overrides = [value for key, value in parse_qsl(url.query) if key.lower() in {"host", "hostaddr"}]
    except ValueError:
        raise SeedRefusedError("Database host refused: <invalid>") from None
    if host.lower() not in {"localhost", "127.0.0.1", "::1"} or overrides:
        # Only print a validated hostname, never arbitrary URL components.
        shown = host if host and all(c.isalnum() or c in ".:-" for c in host) else "<empty or invalid>"
        raise SeedRefusedError(f"Database host refused: {shown}")


# key, account, sender, address, subject, snippet, days ago, local time, read, flagged
BOARD_MAILS = [
    ("hausverwaltung-nebenkosten", "privat", "Hausverwaltung Kurz", "abrechnung@hv-kurz.example", "Nebenkostenabrechnung 2025", "Sehr geehrte Mieterinnen und Mieter, anbei erhalten Sie die Abrechnung …", 0, "08:57", False, True),
    ("paketdienst-paket-heute", "privat", "Paketdienst Nord", "post@paketdienst-nord.example", "Dein Paket kommt heute", "Zustellung zwischen 13 und 15 Uhr. Du kannst den Termin noch ändern …", 0, "07:42", False, False),
    ("meyer-angebot-4471", "arbeit", "Meyer GmbH", "angebot@meyer-gmbh.example", "Angebot Nr. 4471", "Guten Tag, wie besprochen sende ich Ihnen unser Angebot. Bitte bestätigen …", 1, "17:20", False, True),
    ("sportverein-hallenzeiten", "verein", "Sportverein Lindenau", "post@sportverein-lindenau.example", "Hallenzeiten im Dezember", "Liebe Mitglieder, über die Feiertage ändern sich die Trainingszeiten …", 1, "14:05", False, False),
    ("stadtwerke-abschlag", "privat", "Stadtwerke Lindenau", "post@stadtwerke-lindenau.example", "Dein Abschlag ab Januar", "Ab dem 1. Januar beträgt dein monatlicher Abschlag 86 € …", 1, "09:31", True, False),
    ("kita-elternabend", "familie", "Kita Sonnenblume", "post@kita-sonnenblume.example", "Elternabend am Donnerstag", "Liebe Eltern, am Donnerstag um 19 Uhr findet unser Elternabend statt …", 2, "10:00", True, False),
    ("techdigest-woche", "arbeit", "TechDigest Weekly", "newsletter@techdigest-weekly.example", "Die Woche in fünf Minuten", "Diese Woche: neue Modelle, ein Sicherheitsupdate und drei Lesetipps …", 3, "10:00", True, False),
]

HOUSE_BODY = "Sehr geehrte Mieterinnen und Mieter,\n\nanbei erhalten Sie die Nebenkostenabrechnung für das Jahr 2025. Für Ihre Wohnung ergibt sich eine Nachzahlung von 184,20 €, die wir am 15.12. von Ihrem Konto einziehen.\n\nDer Anstieg geht vor allem auf die Heizkosten zurück, die gegenüber dem Vorjahr um 18 % gestiegen sind. Die Einzelaufstellung finden Sie im Anhang.\n\nEinwände gegen die Abrechnung können Sie bis zum 20.11. schriftlich bei uns einreichen.\n\nMit freundlichen Grüßen\nIhre Hausverwaltung Kurz"

NEWSLETTERS = [
    ("Wohnwelt Deals", "wohnwelt-deals", "privat", 23, False),
    ("Laufschuh-Club", "laufschuh-club", "privat", 11, True),
    ("TechDigest Weekly", "techdigest-weekly", "arbeit", 13, False),
    ("Gartenfreunde Post", "gartenfreunde-post", "familie", 6, True),
    ("ReiseRadar", "reiseradar", "arbeit", 7, False),
]


async def seed_tasks(now: datetime) -> None:
    """Replace only synthetic task series through the normal transactional services."""
    from db.accessors import task_occurrence, task_series
    from db.engine import get_session_maker
    from api.tasks import create, lifecycle, reconcile, state

    today = now.astimezone(BERLIN).date()
    specs = [
        ('once', 'Unterlagen prüfen', today, None),
        ('daily', 'Post prüfen', today, 'FREQ=DAILY'),
        ('weekly', 'Wochenplanung', today, 'FREQ=WEEKLY'),
        ('monthly', 'Belege ablegen', today, 'FREQ=MONTHLY'),
        ('overdue', 'Angebot beantworten', today - timedelta(days=3), None),
        ('completed', 'Notizen ordnen', today - timedelta(days=1), None),
        ('archived', 'Termin vorbereiten', today, None),
        ('mail', 'Abrechnung prüfen', today, None),
    ]
    async with get_session_maker()() as session:
        after = None
        while True:
            rows = await task_series.list_page(session, scope='app', due_from='0001-01-01', due_to='9999-12-31', archived=None, source='all', source_account_id=None, after=after, limit=100)
            if not rows:
                break
            after = rows[-1].id
            for row in rows:
                if row.creation_key.startswith('board-seed-task:'):
                    await lifecycle.delete(session, row.id, expected_version=row.version, confirm_history_loss=True)
        for key, title, start, rule in specs:
            source = {'source_account_id': seed_id('account', 'privat'), 'source_message_id': seed_id('mail', 'hausverwaltung-nebenkosten')} if key == 'mail' else {}
            row, _ = await create.create(session, creation_key=f'board-seed-task:{key}', title=title, start=start, zone='Europe/Berlin', rule=rule, **source)
            _, cursor, complete = await reconcile.reconcile_current(session, series_id=row.id, now=now)
            while not complete:
                _, cursor, complete = await reconcile.reconcile_current(session, series_id=row.id, cursor=cursor, now=now)
            if key == 'completed':
                instances = await task_occurrence.list_page(session, scope='app', status='open', due_from=None, due_to=None, after=None, limit=1, series_id=row.id)
                await state.transition(session, instances[0].id, version=instances[0].version, action='complete')
            if key == 'archived':
                await lifecycle.archive(session, row.id, expected_version=row.version, now=now)
        await session.commit()


async def seed_filing() -> None:
    """Install synthetic app-side folders, labels and rules in a local database."""
    from db.models.mail_folder import MailFolder
    from db.models.mail_filing import MailFiling
    from db.models.mail_label_assignment import MailLabelAssignment
    from db.models.mail_rule import MailRule
    from db.models.mail_rule_action import MailRuleAction
    from db.models.mail_rule_run import MailRuleRun
    from db.models.mail_filing_usage import MailFilingUsage

    require_local_database()
    folder = MailFolder(id=seed_id("folder", "projects"), name="Projekte (Beispiel)", kind="folder", color="moss")
    label = MailFolder(id=seed_id("label", "important"), name="Wichtig (Beispiel)", kind="label", color="teal")
    messages = [seed_id("mail", "meyer-angebot-4471"), seed_id("mail", "kita-elternabend")]
    filings = [MailFiling(message_id=message_id, folder_id=folder.id) for message_id in messages]
    labels = [MailLabelAssignment(message_id=message_id, label_id=label.id)
              for message_id in (messages[0], seed_id("mail", "hausverwaltung-nebenkosten"))]
    rules = [MailRule(id=seed_id("rule", "projects"), name="Angebote zuordnen (Beispiel)",
                      sender_domain="meyer-gmbh.example", folder_id=folder.id, label_ids=[label.id]),
             MailRule(id=seed_id("rule", "studio-skip"), name="Studio-Aktion (Beispiel)",
                      account_id=seed_id("account", "privat"), sender_domain="hv-kurz.example",
                      label_ids=[label.id], mark_read=True)]
    runs = [MailRuleRun(id=seed_id("rule-run", "studio-skip"), rule_id=rules[1].id, rule_version=1,
                        message_id=seed_id("mail", "hausverwaltung-nebenkosten"), account_id=seed_id("account", "privat"),
                        status="done", provider_results_json=json.dumps({"mark_read": "skipped_unsupported"}))]
    actions = [MailRuleAction(id=seed_id("rule-action", "studio-skip"), rule_id=runs[0].rule_id, message_id=runs[0].message_id,
                              kind="mark_read", run_id=runs[0].id, status="skipped_unsupported")]
    usage = [MailFilingUsage(destination_id=folder.id, use_count=7), MailFilingUsage(destination_id=label.id, use_count=4)]
    await seed_access.replace_filing([folder, label], filings, labels, rules, runs, usage, actions)


async def seed(now: datetime | None = None) -> SeedResult:
    """Replace local board sample mail data, never accessing a remote database."""
    require_local_database()
    local_now = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    today = local_now.date()

    def at(day_offset: int, clock: str) -> datetime:
        hour, minute = map(int, clock.split(":"))
        return datetime.combine(today - timedelta(days=day_offset), time(hour, minute), BERLIN).astimezone(UTC)

    specs = [
        ("privat", "privat@example.com", "gmail", "mistral_connector"),
        ("arbeit", "arbeit@example.org", "microsoft", "oauth2"),
        ("verein", "verein@example.com", "imap", "imap_password"),
        ("familie", "familie@example.net", "imap", "imap_password"),
    ]
    accounts = [MailAccount(id=seed_id("account", key), email_address=email, provider=provider, auth_type=auth,
                            created_at=local_now.astimezone(UTC) + timedelta(seconds=i), updated_at=local_now.astimezone(UTC))
                for i, (key, email, provider, auth) in enumerate(specs)]
    by_key = dict(zip((spec[0] for spec in specs), accounts, strict=True))
    states = [
        MailSyncState(id=seed_id("sync", key), account_id=by_key[key].id, status=status, last_error=error,
                      last_sync_at=last, updated_at=updated)
        for key, status, error, last, updated in [
            ("privat", "ok", "", at(0, "08:57"), at(0, "08:57")),
            ("arbeit", "error", "auth_expired", None, local_now.astimezone(UTC)),
            ("verein", "ok", "", at(0, "08:55"), at(0, "08:55")),
            ("familie", "syncing", "", None, (local_now - timedelta(minutes=1)).astimezone(UTC)),
        ]
    ]
    messages = []
    for key, account, sender, address, subject, snippet, day, clock, read, flagged in BOARD_MAILS:
        house = key == "hausverwaltung-nebenkosten"
        tech = key == "techdigest-woche"
        messages.append(MailMessage(id=seed_id("mail", key), account_id=by_key[account].id,
            provider_uid=f"seed-{key}", thread_id=f"seed-{key}", sender=formataddr((sender, address)), sender_address=address,
            subject=subject, snippet=snippet, body_text=HOUSE_BODY if house else snippet,
            recipients_json=json.dumps([by_key[account].email_address]), internal_date=at(day, clock),
            is_read=read, is_flagged=flagged, has_attachments=house,
            attachments_json=json.dumps([{"name": "Abrechnung_2025.pdf", "size": 212 * 1024, "content_type": "application/pdf"}]) if house else "[]",
            list_unsubscribe="<mailto:abmelden@techdigest-weekly.example>" if tech else ""))
    index = 0
    for sender, slug, account, count, read in NEWSLETTERS:
        for number in range(count, 0, -1):
            key = f"{slug}-{number}"
            snippet = "Die neuen Angebote und Tipps der Woche."
            messages.append(MailMessage(id=seed_id("mail", key), account_id=by_key[account].id,
                provider_uid=f"seed-{key}", thread_id=f"seed-{key}", sender=formataddr((sender, f"newsletter@{slug}.example")),
                sender_address=f"newsletter@{slug}.example", subject=f"{sender}: Ausgabe {number}",
                snippet=snippet, body_text=snippet,
                body_html=(
                    ('<h1>Synthetischer Newsletter</h1><p>Eine einfache Nachricht ohne eigene Farben.</p>'
                     '<p><a href="https://example.test/news">Beispiel öffnen</a></p>'
                     + '<p>Ein längerer synthetischer Abschnitt für den Lesebereich.</p>' * 30)
                    if index == 0 else
                    '<div style="background-color:#ffffff;color:#111111;padding:20px"><h1>Synthetisches Angebot</h1>'
                    '<p>Eine gestaltete Nachricht mit hellem Hintergrund.</p><table><tr><td>Nur Testdaten</td></tr></table>'
                    '<img src="https://example.test/tracking-pixel" width="1" height="1" alt=""></div>'
                    if index == 1 else
                    '<meta name="color-scheme" content="light dark"><style>@media (prefers-color-scheme: dark) '
                    '{ .panel { background-color:#173a34; color:#ffffff } }</style>'
                    '<div class="panel" style="background-color:#ffffff;color:#111111;padding:20px">'
                    '<h1>Synthetische dunkle Variante</h1><p>Vom Absender gestaltete Farben.</p></div>'
                    if index == 2 else None),
                html_status='available' if index < 3 else 'absent', html_version=1, recipients_json=json.dumps([by_key[account].email_address]),
                list_unsubscribe=f"<mailto:abmelden@{slug}.example>", is_read=read,
                internal_date=(local_now - timedelta(days=5 + 115 * index / 59)).astimezone(UTC)))
            index += 1
    tomorrow = today + timedelta(days=1)
    deadline = tomorrow + timedelta(days=(4 - tomorrow.weekday()) % 7)
    insight = mail_insight.seed_row(id=seed_id("insight", "hausverwaltung-nebenkosten"),
        message_id=seed_id("mail", "hausverwaltung-nebenkosten"), deadline=deadline,
        points=["Du sollst für 2025 184,20 € nachzahlen; der Betrag wird am 15.12. abgebucht.",
                "Grund sind vor allem die Heizkosten, sie sind um 18 % gestiegen.",
                "Einwände nimmt die Hausverwaltung bis Fr., 20.11. an."],
        followups=["Einwand entwerfen", "Mit der Abrechnung 2024 vergleichen"])
    started = (local_now - timedelta(minutes=1)).astimezone(UTC)
    jobs = [MailJob(id=seed_id("job", "familie"), kind="sync", status="running",
                    payload_json=json.dumps({"account_id": by_key["familie"].id}),
                    dedupe_key=f"seed-sync-{by_key['familie'].id}", locked_at=started,
                    created_at=started, updated_at=started)]
    await seed_access.replace_mail_data(accounts, states, messages, [insight], jobs)
    await seed_tasks(local_now.astimezone(UTC))
    await seed_filing()

    conversation_specs = [
        ("was-ist-heute-wichtig", "Was ist heute wichtig?", at(0, "08:57")),
        ("newsletter-aufraeumen", "Newsletter aufräumen", at(1, "10:00")),
        ("angebot-meyer-gmbh", "Angebot Meyer GmbH", at(3, "10:00")),
        ("belege-fuer-die-steuer", "Belege für die Steuer", at(12, "10:00")),
        ("skill-offene-rechnungen", "Skill für offene Rechnungen", at(0, "08:30")),
    ]
    conversations = [ChatConversation(id=seed_id("conversation", key), title=title,
                                     model="mistral-medium-latest", created_at=updated, updated_at=updated)
                     for key, title, updated in conversation_specs]
    await seed_access.replace_conversations(conversations)
    first, second, third, fourth, fifth = conversations
    await chat_access.add_message(conversation_id=first.id, role="user", content="Was ist heute wichtig?")
    extras = ChatExtrasOut(
        mails=[seed_id("mail", key) for key in ("hausverwaltung-nebenkosten", "meyer-angebot-4471", "kita-elternabend")],
        suggestions=["Antworten entwerfen", "Nur die Hausverwaltung", "Was steht in der Abrechnung?"],
        model="mistral-medium-latest",
    )
    await chat_access.add_message(conversation_id=first.id, role="assistant", extras_json=extras.model_dump_json(),
        content="Auf drei Mails solltest du heute antworten.\n\nAm dringendsten ist die Nebenkostenabrechnung der Hausverwaltung Kurz, denn Einwände nimmt sie nur bis nächsten Freitag an. Die Meyer GmbH möchte bis Montag wissen, ob du ihr Angebot annimmst, und die Kita fragt, ob du zum Elternabend kommst.")
    await chat_access.add_message(conversation_id=second.id, role="user", content="Räum meine Newsletter auf.")
    await chat_access.add_message(conversation_id=second.id, role="assistant",
        content="Ich habe 14 Absender gefunden, die dir regelmäßig Newsletter schicken. Fünf davon hast du seit über drei Monaten nicht geöffnet; die schlage ich zum Abbestellen vor. Die anderen neun liest du, sie bleiben, wie sie sind.",
        extras_json=ChatExtrasOut(model="mistral-medium-latest").model_dump_json())
    items = []
    for sender, slug, account, _, _ in NEWSLETTERS:
        address = f"newsletter@{slug}.example"
        sender_mails = [m for m in messages if m.account_id == by_key[account].id and m.sender_address == address]
        link = f"<mailto:abmelden@{slug}.example>"
        method, uri = newsletters.unsubscribe_method(link, "")
        subtitle = unsubscribe_subtitle(len(sender_mails), address, method, uri, locale="de")
        items.append(proposals.ProposalItem(
            id=f"{by_key[account].id}|{address}", kind="sender", title=sender, subtitle=subtitle,
            count=len(sender_mails), unread=sum(not m.is_read for m in sender_mails),
            last_read_at=max((m.internal_date for m in sender_mails if m.is_read and m.internal_date), default=None),
            method=method, target=uri,
        ))
    await proposals.create_proposal(second.id, proposals.Proposal(
        kind="unsubscribe", reason="Du hast sie seit über drei Monaten nicht geöffnet.", items=items))
    for conversation in (third, fourth):
        await chat_access.add_message(conversation_id=conversation.id, role="user", content=conversation.title)
    skill_card = await seed_skill_card(fifth)
    await seed_calendars(local_now)
    return SeedResult(len(accounts), len(messages), sum(not m.is_read for m in messages), 1, len(conversations), 1 + skill_card)


async def seed_skill_card(conversation: ChatConversation) -> bool:
    """Add a pending skill-creator card bound to the current chat grant.

    The card renders in full and can be confirmed locally while that grant stays unchanged;
    confirming creates an ordinary custom skill. Returns False when the creator is unavailable.
    """
    from api.mail.agent_prompt import APP_PROMPT_VERSION
    from api.skills.catalog import ensure_builtin_skills
    from api.skills.types import SKILL_CREATOR_SLUG, SkillDraft, SkillExecutionRef, SkillWorkflowStep
    from db.accessors import chat_settings, skill_definition
    from db.models.chat_context import ChatContext

    await ensure_builtin_skills()
    creator = await skill_definition.get_skill(SKILL_CREATOR_SLUG)
    if creator is None or creator.origin != "builtin" or creator.status != "active" or not creator.revision_id:
        return False
    settings = await chat_settings.get()
    context = await seed_access.add_context(ChatContext(
        conversation_id=conversation.id, app_prompt_version=APP_PROMPT_VERSION, mail_scope_version=settings.version,
        account_scope_json=json.dumps({"selection": None, "account_ids": settings.account_ids}),
        skill_id=creator.id, skill_revision_id=creator.revision_id))
    await chat_access.add_message(conversation_id=conversation.id, role="user", context_id=context.id,
        content="Ich möchte morgens sehen, welche Rechnungen noch offen sind.")
    extras = ChatExtrasOut(model="mistral-medium-latest", skill=SkillExecutionRef(
        id=creator.id, slug=creator.slug, revision_id=creator.revision_id, version=creator.version, origin="builtin"))
    await chat_access.add_message(conversation_id=conversation.id, role="assistant", context_id=context.id,
        extras_json=extras.model_dump_json(),
        content="Hier ist ein Entwurf für den Skill „Offene Rechnungen“. Er sucht Mails mit Rechnungen und zeigt dir Betrag und Frist. Prüf ihn und speichere ihn, wenn er passt.")
    draft = SkillDraft(
        slug="offene-rechnungen", name="Offene Rechnungen",
        description="Zeigt Mails mit offenen Rechnungen der letzten 30 Tage mit Betrag und Frist.",
        instructions=("Suche in den freigegebenen Konten nach Mails mit Rechnungen der letzten 30 Tage. "
                      "Nenne Absender, Betrag und Frist, wenn sie in der Mail stehen. "
                      "Schlage keine Mailaktionen vor, wenn sie nicht ausdrücklich gewünscht sind."),
        workflow_steps=[
            SkillWorkflowStep(id="suchen", title="Rechnungen suchen", description="Mails mit Rechnungen der letzten 30 Tage finden.", tool_names=["search_messages"]),
            SkillWorkflowStep(id="zeigen", title="Übersicht zeigen", description="Treffer mit Betrag und Frist auflisten.", tool_names=["get_message", "show_mails"]),
        ],
        allowed_tools=["search_messages", "get_message", "show_mails"],
    )
    await proposals.create_proposal(conversation.id, proposals.Proposal(
        kind="skill", reason="Skill-Entwurf „Offene Rechnungen“ (/offene-rechnungen) liegt zur Bestätigung vor.", items=[], skill=draft),
        context_id=context.id)
    return True


async def seed_calendars(now: datetime) -> None:
    """Install synthetic Google/ICS presentations without registering adapters."""
    from db.accessors import calendar_seed
    from db.models.calendar_source import CalendarSource
    from db.models.calendar_event import CalendarEvent
    from db.models.calendar_occurrence import CalendarOccurrence
    from db.models.calendar_sync_state import CalendarSyncState
    from api.calendar.seed_ids import SEED_CALENDAR_KEYS

    require_local_database()
    today = datetime.combine(now.astimezone(UTC).date(), time.min, UTC)
    sources, events, occurrences, states = [], [], [], []
    names = ("Arbeit", "Verein", "Alter Stand", "Ohne Abdeckung", "Pausierter Kalender")
    for index, (key, name) in enumerate(zip(SEED_CALENDAR_KEYS, names, strict=True)):
        source_id = seed_id("calendar", key)
        sources.append(CalendarSource(id=source_id, provider="google" if index == 0 else "ics",
            connection_ref="test-only", external_id=key, name=name, timezone="Europe/Berlin",
            capabilities_json='["read","write"]' if index == 0 else '["read"]', is_active=key != "paused"))
        states.append(CalendarSyncState(source_id=source_id,
            coverage_start=None if key == "uncovered" else today - timedelta(days=30),
            coverage_end=None if key == "uncovered" else today + timedelta(days=60),
            last_success_at=None if key == "uncovered" else now.astimezone(UTC) - timedelta(days=7 if key == "old" else 0),
            incomplete=key == "uncovered"))
        if key == "uncovered":
            continue
        title = "Planung" if index < 2 else "Gemeinsame Vorbereitung des Sommerfestes mit dem Team und allen Helfenden aus dem Verein"
        event_id = seed_id("calendar-event", key)
        events.append(CalendarEvent(id=event_id, source_id=source_id, external_id=key, title=title))
        occurrences.append(CalendarOccurrence(id=seed_id("calendar-occurrence", key), source_id=source_id,
            event_id=event_id, external_id=key, title=title, editable=index == 0,
            start_date=today.date() if key == "old" else None,
            end_date=(today + timedelta(days=1)).date() if key == "old" else None,
            start_at=None if key == "old" else today + timedelta(hours=10),
            end_at=None if key == "old" else today + timedelta(hours=11),
            timezone=None if key == "old" else "Europe/Berlin"))
    await calendar_seed.replace(sources, events, occurrences, states)


def main() -> None:
    try:
        result = asyncio.run(seed())
    except SeedRefusedError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    print(f"Seeded {result.accounts} accounts, {result.mails} mails, {result.unread} unread, {result.insights} insights, {result.conversations} conversations, {result.proposals} proposals.")


if __name__ == "__main__":
    main()
