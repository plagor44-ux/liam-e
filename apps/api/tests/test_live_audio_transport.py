"""Browser boundary with synthetic frames and a stub provider."""
from contextlib import asynccontextmanager
import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from conftest import build_app


def test_live_transcription_requires_owner_and_opt_in(monkeypatch):
    from api.audio.access import get_audio_owner_verifier
    from env.audio import env
    from api.routers.api.v1.audio.live import transcription

    class Provider:
        async def append(self, data): assert data == b'\x00\x01'
        async def receive(self):
            await asyncio.sleep(0.01)
            return {'type': 'turn.start', 'time_s': 1.0}

    @asynccontextmanager
    async def fake_live(kind):
        assert kind == 'transcription'
        yield Provider()

    app = build_app()
    async def allow(_): return True
    app.dependency_overrides[get_audio_owner_verifier] = lambda: allow
    monkeypatch.setattr(transcription, 'mistral_live', fake_live)
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
        with client.websocket_connect('/api/v1/audio/live/transcription', headers={'origin': 'https://test.example'}) as ws:
            ws.send_bytes(b'\x00\x01')
            assert ws.receive_json() == {'type': 'turn.start', 'time_s': 1.0}
            ws.close()


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
    async def fake_live(kind):
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
