"""Browser boundary with synthetic frames and a stub provider."""
from contextlib import asynccontextmanager
import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from conftest import build_app


def test_live_transcription_requires_owner_and_opt_in(monkeypatch, ui_locale_in_browser_mode):
    from unittest.mock import Mock
    from api.audio.access import get_audio_owner_verifier
    from env.audio import env
    from api.routers.api.v1.audio.live import transcription

    ui_locale_in_browser_mode.return_value.locale = 'de'
    selected = []
    class Provider:
        async def append(self, data): assert data == b'\x00\x01'
        async def receive(self):
            await asyncio.sleep(0.01)
            return {'type': 'turn.start', 'time_s': 1.0}

    @asynccontextmanager
    async def fake_live(kind, **kwargs):
        assert kind == 'transcription'
        selected.append(kwargs['locale'])
        yield Provider()

    app = build_app()
    async def allow(_): return True
    app.dependency_overrides[get_audio_owner_verifier] = lambda: allow
    monkeypatch.setattr(transcription, 'mistral_live', fake_live)
    warm = Mock()
    monkeypatch.setattr(transcription, 'prewarm_catalog', warm)
    monkeypatch.setattr(transcription, 'prewarm_voices', lambda: None)
    monkeypatch.setattr(env, 'audio_studio_owner', json.dumps({'user_id': '00000000-0000-0000-0000-000000000001', 'organization_id': '00000000-0000-0000-0000-000000000002', 'workspace_id': '00000000-0000-0000-0000-000000000003', 'origin': 'https://test.example'}))
    monkeypatch.setattr(env, 'audio_live_test_origin', 'https://test.example')
    monkeypatch.setattr(env, 'audio_live_test_until', (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/api/v1/audio/live/transcription', headers={'origin': 'https://test.example'}): pass
        monkeypatch.setattr(env, 'audio_live_test_enabled', True)
        monkeypatch.setattr(env, 'audio_live_test_until', (datetime.now(timezone.utc) + timedelta(days=4)).isoformat())
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/api/v1/audio/live/transcription', headers={'origin': 'https://test.example'}): pass
        monkeypatch.setattr(env, 'audio_live_test_until', (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/api/v1/audio/live/transcription', headers={'origin': 'https://other.example'}): pass
        warm.assert_not_called()
        with client.websocket_connect('/api/v1/audio/live/transcription?locale=en', headers={'origin': 'https://test.example'}) as ws:
            ws.send_bytes(b'\x00\x01')
            assert ws.receive_json() == {'type': 'turn.start', 'time_s': 1.0}
            ws.close()
        assert selected == ['de']
        warm.assert_called_once_with()


@pytest.mark.anyio
async def test_reconnect_waits_for_previous_provider_cleanup(monkeypatch):
    from api.routers.api.v1.audio.live import transcription

    slot = asyncio.Semaphore(1)
    await slot.acquire()  # The previous session still owns the slot while its provider closes.
    monkeypatch.setattr(transcription, '_slot', slot)
    monkeypatch.setattr(transcription, 'live_remaining', lambda _: 60)
    accepted = asyncio.Event()
    class Socket:
        async def accept(self): accepted.set()
        async def close(self, code): pass
        async def receive_bytes(self): raise RuntimeError('synthetic disconnect')
        async def send_json(self, value): pass
    class Provider:
        async def receive(self): await asyncio.Future()
        async def append(self, value): pass
    @asynccontextmanager
    async def fake_live(kind, **kwargs):
        yield Provider()
    monkeypatch.setattr(transcription, 'mistral_live', fake_live)
    task = asyncio.create_task(transcription.transcribe(Socket()))
    await asyncio.sleep(0.02)
    assert not accepted.is_set()
    slot.release()
    await asyncio.wait_for(task, timeout=1)
    assert accepted.is_set()
    assert not slot.locked()


@pytest.mark.anyio
async def test_reconnect_timeout_and_expired_window_fail_closed(monkeypatch):
    from api.routers.api.v1.audio.live import transcription

    class Socket:
        def __init__(self): self.closed = None; self.accepted = False
        async def close(self, code): self.closed = code
        async def accept(self): self.accepted = True
    slot = asyncio.Semaphore(1)
    await slot.acquire()
    monkeypatch.setattr(transcription, '_slot', slot)
    monkeypatch.setattr(transcription, 'live_remaining', lambda _: 0.02)
    waiting = Socket()
    await transcription.transcribe(waiting)
    assert waiting.closed == 1013 and not waiting.accepted
    slot.release()
    values = iter([1, 0])
    monkeypatch.setattr(transcription, 'live_remaining', lambda _: next(values))
    expired = Socket()
    await transcription.transcribe(expired)
    assert expired.closed == 1008 and not expired.accepted and not slot.locked()


@pytest.mark.parametrize(('locale', 'chosen'), [('de', 'voice_123'), ('en', 'voice_456')])
def test_voice_turn_requires_owner_origin_and_speaks_server_deltas(monkeypatch, capsys, locale, chosen):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api.audio.access import get_audio_owner_verifier
    from api.audio import voice_turn as voice_bridge
    from api.mail.chat_events import StartEvent, DeltaEvent, DoneEvent
    from api.routers.api.v1.audio.live import turn
    from env.audio import env

    spoken = []
    agent_options = []
    voice_options = []
    class Provider:
        async def append(self, text): spoken.append(text)
        async def finish(self): self.first = True
        async def receive(self):
            if self.first:
                self.first = False
                return bytes(4)
            return None
    @asynccontextmanager
    async def fake_live(kind, **kwargs):
        voice_options.append(kwargs)
        yield Provider()
    async def fake_events(**kwargs):
        agent_options.append(kwargs)
        yield StartEvent(conversation_id='c', user_message_id='u', model='test')
        yield DeltaEvent(text='Hallo.')
        yield DoneEvent(conversation_id='c', message_id='new', stopped=False)
    monkeypatch.setattr(turn.chat_conversation, 'get_conversation', AsyncMock(return_value=object()))
    monkeypatch.setattr(turn, 'resolve_context', AsyncMock(return_value=(object(), SimpleNamespace(id='ctx'), None)))
    monkeypatch.setattr(turn, 'run_turn_events', fake_events)
    monkeypatch.setattr(turn.ui_settings, 'get_or_create', AsyncMock(return_value=SimpleNamespace(locale='browser', browser_locale=None, speech_pace='normal', voice_model='mistral-small-latest', voice_de='voice_123', voice_en='voice_456')))
    monkeypatch.setattr(turn, 'resolve_locale', AsyncMock(return_value=locale))
    monkeypatch.setattr(turn, 'effective_voice', AsyncMock(side_effect=lambda value, lang: value))
    monkeypatch.setattr(voice_bridge, 'mistral_live', fake_live)
    monkeypatch.setattr(voice_bridge.chat_context, 'is_live', AsyncMock(return_value=True))
    monkeypatch.setattr(env, 'audio_studio_owner', json.dumps({'user_id': '00000000-0000-0000-0000-000000000001', 'organization_id': '00000000-0000-0000-0000-000000000002', 'workspace_id': '00000000-0000-0000-0000-000000000003', 'origin': 'https://test.example'}))
    monkeypatch.setattr(env, 'audio_live_test_enabled', True)
    monkeypatch.setattr(env, 'audio_live_test_origin', 'https://test.example')
    monkeypatch.setattr(env, 'audio_live_test_until', (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    app = build_app()
    async def allow(_): return True
    async def deny(_): return False
    app.dependency_overrides[get_audio_owner_verifier] = lambda: deny
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/api/v1/audio/live/turn', headers={'origin': 'https://test.example'}): pass
        app.dependency_overrides[get_audio_owner_verifier] = lambda: allow
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/api/v1/audio/live/turn', headers={'origin': 'https://other.example'}): pass
        with client.websocket_connect('/api/v1/audio/live/turn', headers={'origin': 'https://test.example'}) as ws:
            ws.send_json({'conversation_id': 'c', 'text': 'Synthetische Frage', 'voice_response': True})
            events = []
            while True:
                event = ws.receive_json(); events.append(event)
                if event['event'] == 'voice_audio_end': break
            assert [e['event'] for e in events] == ['start', 'delta', 'voice_text_done', 'voice_audio', 'voice_audio_end']
            assert spoken == ['Hallo.']
            ws.send_json({'event': 'playback_finished'})
            assert ws.receive_json()['message_id'] == 'new'
    assert voice_options[0]['voice'] == chosen and voice_options[0]['locale'] == locale
    assert agent_options[0]['voice_model'] == 'mistral-small-latest'
    assert agent_options[0]['live_voice_turn'] is True
    assert capsys.readouterr().err.count('voice_metrics ') == 1


def test_voice_turn_preserves_context_refusals_and_missing_conversation(monkeypatch, capsys):
    from unittest.mock import AsyncMock
    from api.audio.access import get_audio_owner_verifier
    from api.errors import refuse
    from api.routers.api.v1.audio.live import turn

    monkeypatch.setattr(turn, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(turn.chat_conversation, 'get_conversation', AsyncMock(return_value=object()))
    monkeypatch.setattr(turn, 'resolve_context', AsyncMock(side_effect=refuse(409, 'chat_settings_changed', 'private detail')))
    app = build_app()
    async def allow(_): return True
    app.dependency_overrides[get_audio_owner_verifier] = lambda: allow
    with TestClient(app) as client:
        with client.websocket_connect('/api/v1/audio/live/turn') as ws:
            ws.send_json({'conversation_id': 'c', 'text': 'synthetic', 'voice_response': True})
            assert ws.receive_json() == {'event': 'refusal', 'code': 'chat_settings_changed'}
        monkeypatch.setattr(turn.chat_conversation, 'get_conversation', AsyncMock(return_value=None))
        with client.websocket_connect('/api/v1/audio/live/turn') as ws:
            ws.send_json({'conversation_id': 'missing', 'text': 'synthetic', 'voice_response': True})
            assert ws.receive_json() == {'event': 'refusal', 'code': 'conversation_not_found'}
    assert capsys.readouterr().err.count('voice_metrics outcome=pre_turn') == 2


def test_voice_turn_creates_conversation_when_started_from_empty_chat(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api.audio.access import get_audio_owner_verifier
    from api.audio import voice_turn as voice_bridge
    from api.errors import refuse
    from api.mail.chat_events import StartEvent, DeltaEvent, DoneEvent
    from api.routers.api.v1.audio.live import turn

    spoken = []
    class Provider:
        async def append(self, text): spoken.append(text)
        async def finish(self): self.first = True
        async def receive(self):
            if self.first:
                self.first = False
                return bytes(4)
            return None
    @asynccontextmanager
    async def fake_live(kind, **kwargs): yield Provider()
    async def fake_events(**kwargs):
        assert kwargs.get('conversation_id') is None
        yield StartEvent(conversation_id='created-conv', user_message_id='u1', model='mistral-small-latest')
        yield DeltaEvent(text='Erste Antwort.')
        yield DoneEvent(conversation_id='created-conv', message_id='m1', stopped=False)

    monkeypatch.setattr(turn, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(voice_bridge, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(turn, 'list_chat_models', AsyncMock(return_value=[SimpleNamespace(id='mistral-small-latest', resolved_id=None, availability='available')]))
    resolve_mock = AsyncMock(return_value=(SimpleNamespace(id='created-conv'), SimpleNamespace(id='ctx-new'), None))
    monkeypatch.setattr(turn, 'resolve_context', resolve_mock)
    monkeypatch.setattr(turn, 'run_turn_events', fake_events)
    monkeypatch.setattr(voice_bridge, 'mistral_live', fake_live)
    monkeypatch.setattr(voice_bridge.chat_context, 'is_live', AsyncMock(return_value=True))
    app = build_app()
    async def allow(_): return True
    app.dependency_overrides[get_audio_owner_verifier] = lambda: allow
    with TestClient(app) as client:
        with client.websocket_connect('/api/v1/audio/live/turn') as ws:
            ws.send_json({'text': 'Erster gesprochener Beitrag', 'voice_response': True, 'model': 'mistral-small-latest'})
            events = []
            while True:
                event = ws.receive_json(); events.append(event)
                if event['event'] == 'voice_audio_end': break
            assert events[0] == {'event': 'start', 'conversation_id': 'created-conv', 'user_message_id': 'u1', 'model': 'mistral-small-latest'}
            assert spoken == ['Erste Antwort.']
            ws.send_json({'event': 'playback_finished'})
            done = ws.receive_json()
            assert done['conversation_id'] == 'created-conv' and done['message_id'] == 'm1'
            assert resolve_mock.await_args.kwargs['conversation_id'] is None
            assert resolve_mock.await_args.kwargs['model'] == 'mistral-small-latest'

        monkeypatch.setattr(turn, 'require_available_model', lambda *_: (_ for _ in ()).throw(refuse(404, 'model_unknown', 'unknown')))
        with client.websocket_connect('/api/v1/audio/live/turn') as ws:
            ws.send_json({'text': 'Mit unbekanntem Modell', 'voice_response': True, 'model': 'gone'})
            assert ws.receive_json() == {'event': 'refusal', 'code': 'model_unknown'}


def test_voice_client_disconnect_is_not_logged_as_provider_failure(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from api.audio.access import get_audio_owner_verifier
    from api.routers.api.v1.audio.live import turn

    monkeypatch.setattr(turn, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(turn.chat_conversation, 'get_conversation', AsyncMock(return_value=object()))
    monkeypatch.setattr(turn, 'resolve_context', AsyncMock(return_value=(object(), SimpleNamespace(id='ctx'), None)))
    monkeypatch.setattr(turn, 'run_voice_turn', AsyncMock(side_effect=WebSocketDisconnect(code=1000)))
    logger = Mock()
    monkeypatch.setattr(turn, '_LOG', logger)
    app = build_app()
    async def allow(_): return True
    app.dependency_overrides[get_audio_owner_verifier] = lambda: allow
    with TestClient(app) as client:
        with client.websocket_connect('/api/v1/audio/live/turn') as ws:
            ws.send_json({'conversation_id': 'c', 'text': 'synthetic', 'voice_response': True})
            with pytest.raises(WebSocketDisconnect): ws.receive_json()
    logger.warning.assert_not_called()
    assert any(call.args[0] == 'live voice turn ended code=client_disconnect' for call in logger.info.call_args_list)
