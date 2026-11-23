"""Synthetic bounded HTML extraction; plain text stays independent."""
import pytest
from api.mail.sync_types import HTML_MAX_CHARS, html_body


@pytest.mark.parametrize(('parts', 'state'), [
    ([], 'absent'),
    (['<p>Sample</p>'], 'available'),
    (['x' * (HTML_MAX_CHARS + 1)], 'too_large'),
])
def test_html_body_is_bounded_and_does_not_truncate(parts, state):
    result, actual = html_body(parts)
    assert actual == state
    assert result == ('<p>Sample</p>' if state == 'available' else None)


def test_all_provider_parsers_keep_html_separate_from_text():
    import base64
    from api.mail import sync_connector, sync_gmail, sync_graph, sync_imap
    html = '<p>Synthetic newsletter</p>'
    encoded = base64.urlsafe_b64encode(html.encode()).decode()
    rows = [
        sync_connector.parse_message({'id': 'm', 'htmlBody': html, 'plaintextBody': 'plain'}, thread_id='t', folder='INBOX', max_body_chars=100),
        sync_gmail.parse_message({'id': 'm', 'payload': {'mimeType': 'text/html', 'body': {'data': encoded}}}, max_body_chars=100),
        sync_graph.parse_message({'id': 'm', 'body': {'contentType': 'html', 'content': html}}, folder='INBOX', max_body_chars=100),
        sync_imap.parse_rfc822(b'Content-Type: text/html; charset=utf-8\r\n\r\n<p>Synthetic newsletter</p>', max_body_chars=100),
    ]
    assert all(row['body_html'] == html and row['html_status'] == 'available' for row in rows)
    assert rows[0]['body_text'] == 'plain'


@pytest.mark.anyio
async def test_html_backfill_retries_unknown_and_marks_absent_and_gone(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api.mail import sync
    now = datetime.now(timezone.utc)
    sync._html_reread_backoff.clear()
    saved = AsyncMock()
    monkeypatch.setattr(sync.message_access, 'set_html', saved)
    rows = [SimpleNamespace(id=str(i), provider_uid=str(i), rfc_message_id='', imap_uidvalidity=None) for i in range(4)]
    await sync._apply_html_bodies(rows, {
        '0': {'body_html': '<p>ok</p>', 'html_status': 'available', 'html_version': 1},
        '1': {'body_html': None, 'html_status': 'absent', 'html_version': 1},
        '2': None,
        '3': {'html_status': 'pending', 'html_version': None},
    }, now)
    assert [call.kwargs['status'] for call in saved.await_args_list] == ['available', 'absent', 'unavailable']
    assert '3' in sync._html_reread_backoff
    sync._html_reread_backoff.clear()


@pytest.mark.anyio
async def test_gmail_external_html_part_is_fetched_and_bounded():
    import base64
    import httpx
    from api.mail import sync_gmail
    content = '<p>External synthetic HTML part</p>'
    encoded = base64.urlsafe_b64encode(content.encode()).decode()
    message = {'id': 'm', 'payload': {'mimeType': 'text/html', 'body': {'attachmentId': 'part', 'size': 100}}}
    calls = []
    def reply(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'data': encoded})
    async with httpx.AsyncClient(base_url='https://example.test', transport=httpx.MockTransport(reply)) as client:
        await sync_gmail._hydrate_html_parts(client, message, {})
    parsed = sync_gmail.parse_message(message, max_body_chars=100)
    assert calls == ['/messages/m/attachments/part']
    assert parsed['body_html'] == content and parsed['html_status'] == 'available'
    too_large = {'id': 'm', 'payload': {'mimeType': 'text/html', 'body': {'attachmentId': 'part', 'size': 800_001}}}
    async with httpx.AsyncClient(base_url='https://example.test', transport=httpx.MockTransport(reply)) as client:
        await sync_gmail._hydrate_html_parts(client, too_large, {})
    parsed = sync_gmail.parse_message(too_large, max_body_chars=100)
    assert parsed['body_html'] is None and parsed['html_status'] == 'too_large'
    assert len(calls) == 1


def test_html_endpoint_returns_only_bounded_stored_markup_and_no_store(monkeypatch, client):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api.routers.api.v1.mail import messages
    row = SimpleNamespace(body_html='<p>Synthetic</p>', html_status='available')
    monkeypatch.setattr(messages.message_access, 'get_message', AsyncMock(return_value=row))
    response = client.get('/api/v1/mail/messages/synthetic/html')
    assert response.status_code == 200 and response.json() == {'html': '<p>Synthetic</p>'}
    assert response.headers['cache-control'] == 'no-store'
    row.html_status = 'too_large'
    assert client.get('/api/v1/mail/messages/synthetic/html').json() == {'html': None}


@pytest.mark.anyio
async def test_gmail_html_attachment_non_object_response_stays_pending():
    import httpx
    from api.mail import sync_gmail
    message = {'id': 'm', 'payload': {'mimeType': 'text/html', 'body': {'attachmentId': 'part'}}}
    async with httpx.AsyncClient(base_url='https://example.test', transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))) as client:
        await sync_gmail._hydrate_html_parts(client, message, {})
    assert sync_gmail.parse_message(message, max_body_chars=100)['html_status'] == 'pending'


def test_full_connector_content_without_html_marks_it_absent():
    from api.mail.sync_connector import _indexed_message
    parsed = _indexed_message({'id': 'synthetic', 'date': '2026-01-01T12:00:00+00:00', 'plaintextBody': 'text'}, 'thread', 100)
    assert parsed['body_html'] is None and parsed['html_status'] == 'absent' and parsed['html_version'] == 1
