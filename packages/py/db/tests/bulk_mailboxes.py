"""Four synthetic messages per account, served at the actual provider adapters' I/O boundaries."""

import json
from datetime import UTC, datetime
from email.utils import format_datetime
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx

from api.mail import connector_mailbox, connectors, providers, service, sync, transport
from db.accessors import mail_sync_state

UIDS = ("11", "12", "13", "14")


class Mailboxes:
    def __init__(self, names, partial, variant):
        self.names = names
        self.partial = partial
        self.variant = variant
        self.replay = False
        self.date = datetime.now(UTC)
        self.labels = {name: {uid: {"INBOX"} for uid in UIDS} for name in names}
        self.accounts = {}
        self.yahoo_folders = {"INBOX": {uid: uid for uid in UIDS}, "TRASH": {}}

    def observed(self, name, uid, *, replica):
        labels = self.labels[name][uid]
        # An old INBOX page for completed moves. Point reads see current labels.
        if self.replay and replica and labels == {"TRASH"}:
            return ["INBOX"]
        return sorted(labels)

    async def connect(self, monkeypatch):
        host, port, *_ = providers.guess_imap_settings("x@yahoo.com")
        monkeypatch.setattr(service, "load_credentials", AsyncMock(return_value={"imap_host": host, "imap_port": port}))
        monkeypatch.setattr(service, "imap_password", AsyncMock(return_value="test-only"))
        monkeypatch.setattr(service, "valid_access_token", AsyncMock(return_value="test-only"))
        monkeypatch.setattr(transport, "check_mail_host", AsyncMock())

        def open_imap(address, imap_port):
            assert (address, imap_port) == ("imap.mail.yahoo.com", 993)
            return YahooConnection(self)

        monkeypatch.setattr(transport, "open_imap", open_imap)
        client = httpx.AsyncClient

        def http_client(*args, **kwargs):
            if kwargs.get("base_url") == providers.GMAIL_API:
                kwargs["transport"] = httpx.MockTransport(self.gmail)
            else:
                assert "transport" in kwargs, "Unexpected HTTP connection"
            return client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", http_client)
        monkeypatch.setattr(connectors, "_get", AsyncMock(return_value={
            "credentials": [{"name": "named", "authentication_type": "oauth2"}]}))
        monkeypatch.setattr(connectors, "_post", self.studio)
        for name in self.names:
            provider, auth_type = {"Yahoo": ("imap", "imap_password"), "Studio": ("outlook", connectors.CONNECTOR),
                                   "OAuth": ("google", "oauth2")}[name]
            incoming_only = name == "Studio" and self.variant == "inbox-only"
            address = "outlook@connector.mistral" if incoming_only else f"{name}@example.test"
            account = await service.upsert_account(provider=provider, auth_type=auth_type, email_address=address)
            self.accounts[name] = account
            if name == "Studio" and not incoming_only:
                await mail_sync_state.save_state(account_id=account.id, connector_verified=True,
                    connector_cursor=connector_mailbox.encode(("named", "anchor", "proof"), ""))
            assert (await sync.sync_account(account.id)).status == "ok"
            # A resumed history page can contain messages that are already indexed.
            if name == "Studio":
                progress = "initial:" + json.dumps([str(int(self.date.timestamp())), "second-page"])
                if not incoming_only:
                    progress = connector_mailbox.encode(("named", "anchor", "proof"), progress)
                await mail_sync_state.save_state(account_id=account.id,
                    connector_cursor=progress)
            elif name == "Yahoo":
                await mail_sync_state.save_state(account_id=account.id, cursor="7:10")

    async def studio(self, path, payload):
        tool = urlsplit(path).path.split("/")[-2]
        args = payload["arguments"]
        if tool in ("label_message", "unlabel_message"):
            uid = args["messageId"]
            labels = self.labels["Studio"][uid]
            if self.partial and uid == "13" and tool == "unlabel_message" and "INBOX" in args["labelIds"]:
                return {"isError": True, "content": [{"type": "text", "text": "refused"}]}
            if tool == "label_message":
                labels.update(args["labelIds"])
            else:
                labels.difference_update(args["labelIds"])
            data = {"id": uid, "labelIds": sorted(labels)}
        elif args.get("threadId") == "anchor":
            data = {"id": "anchor", "messages": [{"id": "proof", "sender": self.accounts["Studio"].email_address,
                                                  "labelIds": ["SENT"]}]}
        else:
            assert tool in ("search_threads", "get_thread")
            rows = [{"id": uid, "labelIds": self.observed("Studio", uid, replica=args.get("messageFormat") != "MINIMAL"),
                     "subject": f"Studio {uid}", "sender": "sender@example.test", "date": self.date.isoformat()} for uid in UIDS]
            thread = {"id": "mixed-thread", "messages": rows}
            data = {"threads": [thread]} if tool == "search_threads" else thread
        return {"content": [{"type": "text", "text": json.dumps(data)}]}

    def gmail(self, request):
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        if path == "/profile":
            data = {"historyId": "1"}
        elif path == "/history":
            data = {"historyId": "2", "history": [{"id": "2", "labelsAdded": [{"message": {"id": uid}} for uid in UIDS]}]}
        elif path == "/messages":
            data = {"messages": [{"id": uid} for uid in UIDS]}
        else:
            _, collection, uid, *operation = path.split("/")
            assert collection == "messages" and uid in UIDS
            labels = self.labels["OAuth"][uid]
            if request.method == "POST":
                if operation == ["trash"]:
                    if self.partial and uid == "13":
                        return httpx.Response(403, json={"error": "refused"})
                    labels.discard("INBOX")
                    labels.add("TRASH")
                elif operation == ["untrash"]:
                    labels.discard("TRASH")
                else:
                    assert operation == ["modify"]
                    args = json.loads(request.content)
                    labels.update(args["addLabelIds"])
                    labels.difference_update(args["removeLabelIds"])
            data = {"id": uid, "threadId": "mixed-thread", "internalDate": str(int(self.date.timestamp() * 1000)),
                    "labelIds": self.observed("OAuth", uid, replica=request.url.params.get("format") == "full"),
                    "payload": {"headers": [{"name": "Subject", "value": f"OAuth {uid}"}]}}
        return httpx.Response(200, json=data)


class YahooConnection:
    def __init__(self, provider):
        self.provider = provider
        self.capabilities = ("UIDPLUS",) if provider.variant == "uidplus" else ("MOVE", "UIDPLUS")
        self.folder = "INBOX"
        self.copied = None
        self.deleted = set()

    def login(self, *_):
        return "OK", []

    def logout(self):
        return "BYE", []

    def list(self, *_):
        return "OK", [b'(\\Trash) "/" "Trash"']

    def select(self, folder, readonly=False):
        self.folder = folder.strip('"').upper()
        return "OK", []

    def response(self, item):
        if item == "COPYUID":
            return item, [self.copied]
        assert item == "UIDVALIDITY"
        return item, [b"7" if self.folder == "INBOX" else b"8"]

    def uid(self, cmd, *args):
        cmd = cmd.upper()
        box = self.provider.yahoo_folders[self.folder]
        if cmd == "SEARCH":
            matches = dict(box)
            if "HEADER" in args:
                wanted = args[-1].strip('"')
                matches = {key: uid for key, uid in matches.items() if wanted == f"<yahoo-{uid}@example.test>"}
                if "UID" in args:
                    matches = {key: uid for key, uid in matches.items() if key == args[2]}
            elif "UID" in args:
                low = int(args[-1].split(":")[0])
                matches = {key: uid for key, uid in matches.items() if int(key) >= low}
            return "OK", [" ".join(matches).encode()]
        key = args[0]
        if cmd == "FETCH":
            if args[1] == "(FLAGS)":
                if ":" in key:
                    low, high = map(int, key.split(":"))
                    keys = [uid for uid in box.copy() if low <= int(uid) <= high]
                else:
                    keys = [uid for uid in key.split(",") if uid in box]
                return "OK", [f"1 (UID {uid} FLAGS ())".encode() for uid in keys]
            uid = box[key]
            raw = (f"From: sender@example.test\r\nSubject: Yahoo {uid}\r\nMessage-ID: <yahoo-{uid}@example.test>\r\n"
                   f"Date: {format_datetime(self.provider.date)}\r\n\r\nSynthetic message {uid}.")
            return "OK", [(f"1 (UID {key} FLAGS ())".encode(), raw.encode())]
        uid = box[key]
        labels = self.provider.labels["Yahoo"][uid]
        if cmd == "STORE":
            if args[2] == "(\\Deleted)":
                if self.provider.partial and uid == "13":
                    return "NO", []  # COPY succeeded, but the source remains in INBOX.
                self.deleted.add(key)
            else:
                assert args[2] == "(\\Seen)"
            return "OK", []
        if cmd == "EXPUNGE":
            assert key in self.deleted
            del box[key]
            labels.remove(self.folder)
            return "OK", []
        assert cmd in ("MOVE", "COPY")
        dest = args[1].strip('"').upper()
        if cmd == "MOVE" and self.provider.partial and uid == "13" and dest == "TRASH":
            return "NO", []
        new_key = str(int(key) + 100)
        self.provider.yahoo_folders[dest][new_key] = uid
        labels.add(dest)
        if cmd == "MOVE":
            del box[key]
            labels.remove(self.folder)
        self.copied = f"{7 if dest == 'INBOX' else 8} {key} {new_key}".encode()
        return "OK", []
