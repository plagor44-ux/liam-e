"""Read-only originals and defensive header parsing."""
import base64
import imaplib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from db.models.mail_account import MailAccount
from db.models.mail_message import MailMessage
from fastapi import HTTPException

from api.mail import connectors, original, service, transport
from api.mail.seed_ids import SEED_ACCOUNT_IDS
from api.routers.api.v1.mail import messages

pytestmark = pytest.mark.anyio


def test_headers():
    raw = b'Received: first\r\nReceived: second\r\nSubject: =?utf-8?q?Gr=C3=BC=C3=9Fe?=\r\nX-Test: one\r\n two\r\n\r\nbody'
    assert original.parse_headers(raw) == [
        {'name': 'Received', 'value': 'first'}, {'name': 'Received', 'value': 'second'},
        {'name': 'Subject', 'value': 'Grüße'}, {'name': 'X-Test', 'value': 'one two'},
    ]
    assert original.parse_headers(b'') == []
    malformed = original.parse_headers(b'Subject: =?bad?x?oops?=\r\nX: \xff')
    assert [header['name'] for header in malformed] == ['Subject', 'X']
    assert original.parse_headers(b'Subject: no blank line') == [{'name': 'Subject', 'value': 'no blank line'}]


def test_imap_readonly(monkeypatch):
    class Connection:
        def login(self, *args): return 'OK', []
        def select(self, name, readonly=False):
            assert name == '"INBOX"' and readonly
            return 'OK', []
        def uid(self, command, uid, query):
            assert (command, uid, query) == ('FETCH', '12', '(RFC822.SIZE BODY.PEEK[]<0.262144>)')
            return 'OK', [(b'1 (RFC822.SIZE 300000 BODY[]<0> {3}', b'abc')]
        def logout(self): self.closed = True
    connection = Connection()
    monkeypatch.setattr(original.transport, 'open_imap', lambda *args: connection)
    raw, truncated = original._imap_fetch(SimpleNamespace(email_address='a'), SimpleNamespace(folder='INBOX', provider_uid='12'), 'host', 993, 'password')
    assert raw == b'abc' and truncated and connection.closed


async def test_missing_and_unavailable(monkeypatch):
    monkeypatch.setattr(messages.message_access, 'get_message', AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as error:
        await messages.message_original('missing')
    assert error.value.status_code == 404
    monkeypatch.setattr(messages.message_access, 'get_message', AsyncMock(return_value=SimpleNamespace(account_id='a')))
    monkeypatch.setattr(messages.account_access, 'get_account', AsyncMock(return_value=None))
    fetch = AsyncMock()
    monkeypatch.setattr(original, 'fetch', fetch)
    with pytest.raises(HTTPException) as error:
        await messages.message_original('m')
    assert error.value.status_code == 409
    fetch.assert_not_awaited()


def account(provider='imap', auth_type='imap_password', account_id='a'):
    return MailAccount(id=account_id, provider=provider, auth_type=auth_type, email_address='a@example.test')


def message(**kwargs):
    return MailMessage(id='m', account_id='a', provider_uid='id/with space', rfc_message_id='<invented@example.test>', **kwargs)


@pytest.fixture
def http_transport(monkeypatch):
    real_client = httpx.AsyncClient
    def install(handler):
        monkeypatch.setattr(original.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(service, 'load_credentials', AsyncMock(return_value={'access_token': 'token'}))
    monkeypatch.setattr(service, 'valid_access_token', AsyncMock(return_value='token'))
    return install


@pytest.fixture
def imap_connection(monkeypatch):
    connection = Mock()
    connection.login.return_value = ('OK', [])
    connection.list.return_value = ('OK', [b'(\\Archive) "/" "Archiv"'])
    connection.select.return_value = ('OK', [])
    def uid(command, *args):
        if command == 'SEARCH':
            assert args == (None, 'HEADER', 'Message-ID', '"<invented@example.test>"')
            return 'OK', [b'91']
        assert command == 'FETCH'
        assert args == ('91', '(RFC822.SIZE BODY.PEEK[]<0.262144>)')
        return 'OK', [(b'1 (RFC822.SIZE 3 BODY[]<0> {3}', b'abc')]
    connection.uid.side_effect = uid
    monkeypatch.setattr(transport, 'open_imap', Mock(return_value=connection))
    monkeypatch.setattr(transport, 'check_mail_host', AsyncMock())
    monkeypatch.setattr(service, 'load_credentials', AsyncMock(return_value={'imap_host': 'imap.example.test'}))
    monkeypatch.setattr(service, 'imap_password', AsyncMock(return_value='password'))
    return connection


def mock_route(monkeypatch, row, acc):
    monkeypatch.setattr(messages.message_access, 'get_message', AsyncMock(return_value=row))
    monkeypatch.setattr(messages.account_access, 'get_account', AsyncMock(return_value=acc))


@pytest.mark.parametrize('body_size,truncated', [(4, False), (262144, True)])
async def test_gmail_raw_capped_and_url_quoted(http_transport, body_size, truncated):
    data = b'Subject: hello\r\n\r\n' + b'x' * body_size
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.params['format'] == 'minimal':
            assert request.url.params['fields'] == 'sizeEstimate'
            return httpx.Response(200, json={'sizeEstimate': len(data)})
        assert request.url.params['format'] == 'raw' and request.url.params['fields'] == 'raw'
        return httpx.Response(200, json={'raw': base64.urlsafe_b64encode(data).decode().rstrip('=')})
    http_transport(handler)
    result = await original.fetch(account('google', 'oauth2'), message())
    assert [r.url.params['format'] for r in calls] == ['minimal', 'raw']
    assert all(b'id%2Fwith%20space' in r.url.raw_path for r in calls)
    assert result['source'] == data[:original.MAX_SOURCE_BYTES].decode()
    assert result['headers'] == [{'name': 'Subject', 'value': 'hello'}]
    assert result['truncated'] is truncated
    service.valid_access_token.assert_awaited_once()
    assert all(r.headers['Authorization'] == 'Bearer token' for r in calls)


async def test_gmail_large_uses_metadata(http_transport):
    calls = []
    def handler(request):
        calls.append(request.url.params['format'])
        if len(calls) == 1:
            return httpx.Response(200, json={'sizeEstimate': 1_048_577})
        return httpx.Response(200, json={'payload': {'headers': [
            {'name': 'Received', 'value': 'first'}, {'name': 'Subject', 'value': 'hello'},
            {'name': 'Received', 'value': 'second'}]}})
    http_transport(handler)
    result = await original.fetch(account('google', 'oauth2'), message())
    assert calls == ['minimal', 'metadata']
    assert result['source'] == 'Received: first\r\nSubject: hello\r\nReceived: second'
    assert result['truncated'] is True
    assert [item['value'] for item in result['headers']] == ['first', 'hello', 'second']


@pytest.mark.parametrize('chunks,expected,truncated,consumed', [
    ([b'Subject: hi\r\n\r\nbody'], b'Subject: hi\r\n\r\nbody', False, 1),
    ([b'x' * 100_000] * 5, b'x' * 262_144, True, 3),
    ([b'x' * 262_144, b'y', b'z'], b'x' * 262_144, True, 2),
    ([b'x' * 262_144], b'x' * 262_144, False, 1),
])
async def test_graph_stream_is_capped(http_transport, chunks, expected, truncated, consumed):
    seen = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                seen.append(len(chunk))
                yield chunk
    def handler(request):
        assert request.method == 'GET'
        assert b'id%2Fwith%20space/$value' in request.url.raw_path
        return httpx.Response(200, stream=Stream())
    http_transport(handler)
    result = await original.fetch(account('microsoft', 'oauth2'), message())
    assert result['source'].encode() == expected
    assert result['truncated'] is truncated
    assert len(seen) == consumed
    assert sum(seen[:-1]) <= original.MAX_SOURCE_BYTES


@pytest.mark.parametrize('size,truncated', [(3, False), (262144, False), (262145, True)])
async def test_imap_archive_searches_message_id_readonly(imap_connection, size, truncated):
    def uid(command, *args):
        if command == 'SEARCH':
            assert args == (None, 'HEADER', 'Message-ID', '"<invented@example.test>"')
            return 'OK', [b'91']
        assert (command, args) == ('FETCH', ('91', '(RFC822.SIZE BODY.PEEK[]<0.262144>)'))
        return 'OK', [(f'1 (RFC822.SIZE {size} BODY[]<0> {{3}}'.encode(), b'abc')]
    imap_connection.uid.side_effect = uid
    result = await original.fetch(account(), message(folder='ARCHIVE'))
    assert result['source'] == 'abc' and result['truncated'] is truncated
    imap_connection.list.assert_called_once_with('""', '"*"')
    imap_connection.select.assert_called_once_with('"Archiv"', readonly=True)
    assert [call.args[0] for call in imap_connection.uid.call_args_list] == ['SEARCH', 'FETCH']
    imap_connection.logout.assert_called_once()


def test_unresolvable_imap_folder_returns_409(client, monkeypatch, imap_connection):
    mock_route(monkeypatch, message(folder='ARCHIVE'), account())
    imap_connection.list.return_value = ('OK', [])
    response = client.get('/api/v1/mail/messages/m/original')
    assert response.status_code == 409 and response.json()['detail']['code'] == 'original_unavailable'
    imap_connection.select.assert_not_called()
    imap_connection.uid.assert_not_called()
    imap_connection.logout.assert_called_once()


@pytest.mark.parametrize('acc', [None, account('gmail', connectors.CONNECTOR), account('imap', account_id=next(iter(SEED_ACCOUNT_IDS))), account('unknown', 'oauth2')])
def test_unavailable_accounts_never_contact_provider(client, monkeypatch, acc):
    row = message()
    if acc is not None:
        row.account_id = acc.id
    mock_route(monkeypatch, row, acc)
    http = Mock(side_effect=AssertionError('HTTP called'))
    imap = Mock(side_effect=AssertionError('IMAP called'))
    credentials = AsyncMock(side_effect=AssertionError('credentials loaded'))
    monkeypatch.setattr(original.httpx, 'AsyncClient', http)
    monkeypatch.setattr(transport, 'open_imap', imap)
    monkeypatch.setattr(service, 'load_credentials', credentials)
    response = client.get('/api/v1/mail/messages/m/original')
    assert response.status_code == 409 and response.json()['detail']['code'] == 'original_unavailable'
    http.assert_not_called()
    imap.assert_not_called()
    credentials.assert_not_awaited()


@pytest.mark.parametrize('failure', ['http', 'timeout', 'imap', 'expired'])
def test_provider_failure_returns_502(client, monkeypatch, http_transport, imap_connection, failure):
    acc = account('google', 'oauth2') if failure != 'imap' else account()
    mock_route(monkeypatch, message(), acc)
    if failure == 'imap':
        imap_connection.login.side_effect = imaplib.IMAP4.error('failed')
    else:
        # The IMAP fixture also stubs credentials; provide OAuth credentials here.
        monkeypatch.setattr(service, 'load_credentials', AsyncMock(return_value={}))
        monkeypatch.setattr(service, 'valid_access_token', AsyncMock(side_effect=service.CredentialError('expired') if failure == 'expired' else None, return_value='token'))
        def handler(request):
            if failure == 'timeout':
                raise httpx.ReadTimeout('timeout', request=request)
            return httpx.Response(503)
        http_transport(handler)
        if failure == 'expired':
            monkeypatch.setattr(service, 'valid_access_token', AsyncMock(side_effect=service.CredentialError('expired')))
    response = client.get('/api/v1/mail/messages/m/original')
    assert response.status_code == 502 and response.json()['detail']['code'] == 'provider_failed'
    if failure == 'imap':
        imap_connection.logout.assert_called_once()


@pytest.mark.parametrize('provider,auth_type,seed,expected', [
    ('imap', 'imap_password', False, True), ('google', 'oauth2', False, True),
    ('microsoft', 'oauth2', False, True), ('gmail', connectors.CONNECTOR, False, False),
    ('imap', 'imap_password', True, False), (None, None, False, False),
])
def test_message_detail_original_availability(client, monkeypatch, provider, auth_type, seed, expected):
    account_id = next(iter(SEED_ACCOUNT_IDS)) if seed else 'a'
    row = MailMessage(id='m', account_id=account_id, provider_uid='12')
    accounts = [] if provider is None else [account(provider, auth_type, account_id)]
    monkeypatch.setattr(messages.message_access, 'get_message', AsyncMock(return_value=row))
    monkeypatch.setattr(messages.account_access, 'list_accounts', AsyncMock(return_value=accounts))
    monkeypatch.setattr(messages.insight_access, 'get_for_messages', AsyncMock(return_value={}))
    monkeypatch.setattr(messages.filing_access, 'for_messages', AsyncMock(return_value={}))
    response = client.get('/api/v1/mail/messages/m')
    assert response.status_code == 200
    assert response.json()['can_show_original'] is expected


@pytest.mark.parametrize('response,truncated', [
    # The size may follow the body literal.
    ([(b'1 (UID 91 BODY[]<0> {3}', b'abc'), b' RFC822.SIZE 300000)'], True),
    ([(b'1 (UID 91 BODY[]<0> {3}', b'abc'), b' RFC822.SIZE 3)'], False),
    # Without a size, a body that fills the cap counts as truncated.
    ([(b'1 (BODY[]<0> {3}', b'abc'), b')'], False),
    ([(b'1 (BODY[]<0> {262144}', b'x' * 262144), b')'], True),
])
async def test_imap_size_position_and_missing_size(imap_connection, response, truncated):
    imap_connection.uid.side_effect = lambda command, *args: ('OK', [b'91'] if command == 'SEARCH' else response)
    result = await original.fetch(account(), message(folder='ARCHIVE'))
    assert result['source'] == response[0][1].decode() and result['truncated'] is truncated


def test_imap_without_message_id_outside_inbox_returns_409(client, monkeypatch, imap_connection):
    row = message(folder='ARCHIVE')
    row.rfc_message_id = ''
    mock_route(monkeypatch, row, account())
    response = client.get('/api/v1/mail/messages/m/original')
    assert response.status_code == 409 and response.json()['detail']['code'] == 'original_unavailable'
    imap_connection.select.assert_called_once_with('"Archiv"', readonly=True)
    imap_connection.uid.assert_not_called()
    imap_connection.logout.assert_called_once()


def test_imap_message_not_found_returns_502(client, monkeypatch, imap_connection):
    mock_route(monkeypatch, message(folder='ARCHIVE'), account())
    imap_connection.uid.side_effect = lambda command, *args: ('OK', [b''])
    response = client.get('/api/v1/mail/messages/m/original')
    assert response.status_code == 502 and response.json()['detail']['code'] == 'provider_failed'
    imap_connection.logout.assert_called_once()


@pytest.mark.parametrize('minimal,raw', [({}, {'raw': 'eA'}), ({'sizeEstimate': 'big'}, {'raw': 'eA'}), ({'sizeEstimate': 4}, {})])
def test_malformed_gmail_response_returns_502(client, monkeypatch, http_transport, minimal, raw):
    mock_route(monkeypatch, message(), account('google', 'oauth2'))
    http_transport(lambda request: httpx.Response(200, json=minimal if request.url.params['format'] == 'minimal' else raw))
    response = client.get('/api/v1/mail/messages/m/original')
    assert response.status_code == 502 and response.json()['detail']['code'] == 'provider_failed'


@pytest.mark.parametrize('message_id', ['<a@example.test>\r\nA1 UID STORE 1:* +FLAGS (\\Deleted)', '<a@example.test>\n', '<a\x00@example.test>'])
def test_imap_message_id_with_control_characters_is_never_sent(client, monkeypatch, imap_connection, message_id):
    row = message(folder='ARCHIVE')
    row.rfc_message_id = message_id
    mock_route(monkeypatch, row, account())
    response = client.get('/api/v1/mail/messages/m/original')
    assert response.status_code == 409 and response.json()['detail']['code'] == 'original_unavailable'
    imap_connection.uid.assert_not_called()
    imap_connection.logout.assert_called_once()


def test_imap_host_outside_the_allowlist_is_never_opened(client, monkeypatch, imap_connection):
    mock_route(monkeypatch, message(), account())
    monkeypatch.setattr(transport, 'check_mail_host', AsyncMock(side_effect=transport.MailHostNotAllowedError('Mail host is not allowed')))
    response = client.get('/api/v1/mail/messages/m/original')
    assert response.status_code == 502 and response.json()['detail']['code'] == 'provider_failed'
    transport.open_imap.assert_not_called()
    service.imap_password.assert_not_awaited()


@pytest.mark.parametrize('uid', ['id/with space', '12 (FLAGS)', '1:*', ''])
def test_imap_inbox_uid_that_is_not_a_number_is_never_fetched(client, monkeypatch, imap_connection, uid):
    row = message(folder='INBOX')
    row.provider_uid = uid
    mock_route(monkeypatch, row, account())
    response = client.get('/api/v1/mail/messages/m/original')
    assert response.status_code == 409 and response.json()['detail']['code'] == 'original_unavailable'
    imap_connection.select.assert_called_once_with('"INBOX"', readonly=True)
    imap_connection.uid.assert_not_called()
    imap_connection.logout.assert_called_once()
