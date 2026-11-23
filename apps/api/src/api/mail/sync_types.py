"""Shared helpers for provider message parsers."""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal
from email.utils import getaddresses
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from typing import Any

import httpx

from api.mail.headers import sender_address


@dataclass
class SyncChanges:
    upserts: list[dict[str, Any]] = field(default_factory=list)
    read_updates: dict[str, bool] = field(default_factory=dict)
    flag_updates: dict[str, bool] = field(default_factory=dict)
    moved_out: list[str] = field(default_factory=list)
    cursor: str = ""
    full_rebuild: bool = False
    initial_pending: bool = False
    initial_history_uids: set[str] = field(default_factory=set)
    initial_deferred_threads: set[str] = field(default_factory=set)
    deferred_threads: list[str] = field(default_factory=list)
    unconfirmed_uids: set[str] = field(default_factory=set)
    restored_members: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    confirmed_inbox: set[str] = field(default_factory=set)
    trashed: set[str] = field(default_factory=set)
    imap_undeleted: dict[str, str] = field(default_factory=dict)
    # A reversible IMAP flag keeps its physical INBOX identity outside the visible lists.
    imap_hidden: dict[str, str] = field(default_factory=dict)
    imap_new_hidden: list[dict[str, Any]] = field(default_factory=list)
    imap_retired: dict[str, str] = field(default_factory=dict)

    def moved_from_inbox(self, uid: str, labels) -> None:
        self.moved_out.append(uid)
        if isinstance(labels, list) and "TRASH" in labels:
            self.trashed.add(uid)

    def observe_current_labels(self, uid: str, labels: list[str] | None, row: dict | None) -> None:
        """A point read supersedes the folder and flags from a search or history page."""
        self.trashed.discard(uid)
        if labels is not None and inbox_labels(labels):
            self.confirmed_inbox.add(uid)
            self.read_updates[uid] = "UNREAD" not in labels
            self.flag_updates[uid] = "STARRED" in labels
            if row is not None:
                row.update(labels_json=json.dumps(labels), is_read=self.read_updates[uid], is_flagged=self.flag_updates[uid])
        else:
            self.moved_from_inbox(uid, labels)
            self.read_updates.pop(uid, None)
            self.flag_updates.pop(uid, None)


def inbox_labels(labels) -> bool:
    return "INBOX" in labels and "TRASH" not in labels


@dataclass
class SyncResult:
    status: Literal["ok", "error"]
    synced: int
    cursor: str
    error_code: str
    new_message_ids: list[str] = field(default_factory=list)
    refresh_insights: bool = False


def addresses(value: str) -> str:
    return json.dumps([address for _, address in getaddresses([value]) if address])


def common(sender: str, headers: dict[str, str]) -> dict[str, Any]:
    return {
        'sender_address': sender_address(sender),
        'rfc_message_id': headers.get('message-id', ''),
        'references': headers.get('references', ''),
        'list_unsubscribe': headers.get('list-unsubscribe', ''),
        'list_unsubscribe_post': headers.get('list-unsubscribe-post', ''),
    }


# Bump whenever body extraction changes: sync re-reads indexed bodies stored with an older version.
BODY_VERSION = 1
# Independent HTML extraction revision; text search and insight versions remain unchanged.
HTML_VERSION = 1
HTML_MAX_CHARS = 200_000


def html_body(parts: list[str]) -> tuple[str | None, str]:
    present = [part for part in parts if part.strip()]
    if not present:
        return None, "absent"
    if sum(map(len, present)) + len(present) - 1 > HTML_MAX_CHARS:
        return None, "too_large"
    content = "\n".join(present)
    return content, "available"

# Elements whose content is never readable text (style sheets, scripts, the document title). Mail
# clients run no scripts, so <noscript> content is text.
_SKIPPED = frozenset({"style", "script", "title", "template", "head"})
# What may stand in <head>; any other element ends a <head> that broken markup left open.
_HEAD_CONTENT = frozenset({"title", "style", "script", "meta", "link", "base", "template", "noscript"})
_PARAGRAPH = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote", "hr", "pre", "dl"})
_LINE = frozenset({"div", "tr", "li", "section", "article", "header", "footer", "center", "address", "dt", "dd", "body"})
# Zero-width and filler characters that mail templates use to pad the preview line.
_INVISIBLE = re.compile(r"[\u00ad\u034f\u200b-\u200d\u2060\ufeff]")
# Marks lines from <pre>, whose spacing is text and survives the final clean-up (private use area).
_PRE = "\ue000"
# Mail is untrusted input, and every end tag searches the stack of open skipped elements: deeper
# nesting is no real mail, only work. Openings beyond it are only counted (`overflow`), so their end
# tags close them and not the tracked ones around them. The rest of the parse is linear.
_MAX_SKIP_DEPTH = 32


class _TextExtractor(HTMLParser):
    """Collects text; block elements owe line breaks that are written only before the next text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip: list[str] = []
        self.overflow: Counter[str] = Counter()  # skipped elements opened beyond _MAX_SKIP_DEPTH, inside all tracked ones
        self.pre = 0
        self.breaks = 0  # 1 = new line, 2 = new paragraph
        self.bullet = False
        self.started = False  # body content has begun: a stray <head> after it is ignored

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            # A <head> left open by broken markup must not hide the whole mail.
            self.skip.clear()
            self.overflow.clear()
            self.started = True
        elif self.skip and self.skip[-1] == "head" and not self.overflow and tag not in _HEAD_CONTENT:
            self.skip.pop()  # body content without <body>: the browser ends <head> here too
        if tag == "head" and self.started:
            return
        if tag in _SKIPPED:
            if len(self.skip) < _MAX_SKIP_DEPTH:
                self.skip.append(tag)
            else:
                self.overflow[tag] += 1
        elif tag == "br":
            self.breaks = min(self.breaks + 1, 2)  # <br><br> is a blank line
        elif tag in _PARAGRAPH:
            self.breaks = 2
        elif tag in _LINE:
            self.breaks = max(self.breaks, 1)
        elif tag in ("td", "th"):
            self.out.append(" ")
        if tag == "li":
            self.bullet = True
        if tag == "pre":
            self.pre += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A self-closed <style/> or <pre/> holds nothing; opening it would hide or reflow the rest.
        if tag not in _SKIPPED and tag != "pre":
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            if self.overflow[tag]:
                self.overflow[tag] -= 1  # the innermost such element is an untracked one
            elif tag in self.skip:
                # Closes it and everything opened inside it, untracked ones included.
                del self.skip[len(self.skip) - 1 - self.skip[::-1].index(tag):]
                self.overflow.clear()
        elif tag in _PARAGRAPH:
            self.breaks = 2
        elif tag in _LINE:
            self.breaks = max(self.breaks, 1)
        if tag == "pre" and self.pre:
            self.pre -= 1
        if tag in ("li", "ul", "ol"):
            self.bullet = False  # an item without text (an image) leaves no bullet behind

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        # Outside <pre>, source line breaks and indentation are layout, not text.
        text = _PRE + data.replace("\r\n", "\n").replace("\n", "\n" + _PRE) if self.pre else re.sub(r"[ \t\r\n\f]+", " ", data)
        self.started = True
        if not _INVISIBLE.sub("", text).replace("\xa0", " ").replace(_PRE, "").strip():
            if not self.breaks:
                self.out.append(text)
            return
        if self.breaks:
            self.out.append("\n" * self.breaks)
            self.breaks = 0
        if self.bullet:
            self.out.append("• ")
            self.bullet = False
        self.out.append(text)


def html_to_text(html: str, *, _retries: int = 2) -> str:
    """Readable plain text from a mail's HTML: no markup, styles, scripts or comments; paragraphs kept."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = _INVISIBLE.sub("", "".join(parser.out)).replace("\xa0", " ")
    lines = [line.replace(_PRE, "").rstrip() if _PRE in line else re.sub(r"[ \t]+", " ", line).strip()
             for line in text.replace("\r\n", "\n").split("\n")]
    # Only a leading <pre> line may start with spaces: its indentation is text.
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n").rstrip()
    if not result and _retries and parser.skip and parser.skip[0] in ("title", "head"):
        # An unclosed <title> swallows everything after it (browsers do the same); a mail without
        # any text is worse than one whose title shows up as its first line.
        return html_to_text(re.sub(rf"<{parser.skip[0]}\b[^>]*>", "", html, count=1, flags=re.IGNORECASE), _retries=_retries - 1)
    return result


# Re-read bodies (sync.refresh_bodies), keyed by provider id: the parsed mail; None when the provider
# no longer has it or can never deliver it (the stored text stays and it is not asked for again); no
# entry when this run could not read it (the next run asks again).
Reread = dict[str, dict[str, Any] | None]


async def reread_http(uids: list[str], get: Callable[[str], Awaitable[httpx.Response]], parse: Callable[[Any], dict[str, Any]], *, prepare: Callable[[dict], Awaitable[None]] | None = None) -> Reread:
    """Ask a REST provider for each mail; one failing mail never costs the others their result."""
    found: Reread = {}
    for uid in uids:
        try:
            response = await get(uid)
        except httpx.TransportError:
            break  # provider unreachable: the rest waits for the next run
        status = response.status_code
        if status in (401, 403, 429):
            break  # token rejected or rate limited (Gmail limits with 403): stop asking
        if 400 <= status < 500 and status != 408:
            found[uid] = None  # gone (404, 410) or an id the provider rejects for good
            continue
        if not response.is_success:
            continue  # server error or timeout for this mail: asked again next run
        try:
            payload = response.json()
            if prepare is not None:
                await prepare(payload)
            found[uid] = parse(payload)
        except (ValueError, httpx.HTTPError):
            continue
    return found


# A plain-text part this short next to a much longer HTML part is a stub ("View this email in your browser").
_STUB_CHARS = 200


def body_text(plain: list[str], html: list[str]) -> str:
    """The text of a mail: its plain-text parts, or its HTML parts as text when the plain ones are empty or a stub."""
    text = "\n".join(part for part in plain if part.strip()).strip()
    if len(text) >= _STUB_CHARS or not any(part.strip() for part in html):
        return text
    converted = html_to_text("\n".join(html))
    return converted if len(converted) > 4 * len(text) else text


def attachments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{'name': item.get('name') or item.get('filename') or '',
             'size': item.get('size', 0),
             'content_type': item.get('contentType') or item.get('content_type') or ''} for item in items]
