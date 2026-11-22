"""Synthetic tests of the authorized live chat-to-speech bridge."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from api.audio import voice_turn
from api.mail.chat_events import StartEvent, DeltaEvent, DoneEvent

pytestmark = pytest.mark.anyio


class Socket:
    def __init__(self): self.output = asyncio.Queue(); self.input = asyncio.Queue()
    async def send_json(self, value): await self.output.put(value)
    async def receive_json(self): return await self.input.get()


async def test_speaks_each_current_visible_sentence_before_chat_done(monkeypatch):
    release = asyncio.Event()
    spoken = []
    class Provider:
        async def append(self, text): spoken.append(text)
        async def finish(self): self.first = True
        async def receive(self):
            if self.first:
                self.first = False
                return b'\x00\x00\x00\x00'
            return None
    @asynccontextmanager
    async def provider(kind, **kwargs): yield Provider()
    monkeypatch.setattr(voice_turn, 'mistral_live', provider)
    monkeypatch.setattr(voice_turn, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(voice_turn.chat_context, 'is_live', AsyncMock(return_value=True))
    async def events():
        yield StartEvent(conversation_id='c', user_message_id='u', model='test')
        yield DeltaEvent(text='Hallo. ')
        await release.wait()
        yield DeltaEvent(text='Wie geht es dir?')
        yield DoneEvent(conversation_id='c', message_id='new', stopped=False)
    socket = Socket()
    task = asyncio.create_task(voice_turn.run_voice_turn(socket, events(), 'context'))
    assert (await asyncio.wait_for(socket.output.get(), 1))['event'] == 'start'
    assert (await asyncio.wait_for(socket.output.get(), 1))['event'] == 'delta'
    assert (await asyncio.wait_for(socket.output.get(), 1))['event'] == 'voice_audio'
    assert spoken == ['Hallo.']
    release.set()
    messages = []
    while True:
        event = await asyncio.wait_for(socket.output.get(), 1)
        messages.append(event)
        if event['event'] == 'voice_audio_end': break
    assert ''.join(spoken) == 'Hallo. Wie geht es dir?'
    assert not task.done()  # The server still watches revocation during browser playback.
    await socket.input.put({'event': 'playback_finished'})
    assert (await asyncio.wait_for(socket.output.get(), 1))['message_id'] == 'new'
    await asyncio.wait_for(task, 1)


async def test_revocation_cancels_speech_and_discards_later_content(monkeypatch):
    live = True
    started = asyncio.Event()
    closed = asyncio.Event()
    class Provider:
        async def append(self, text): started.set()
        async def finish(self): pass
        async def receive(self): await asyncio.Future()
    @asynccontextmanager
    async def provider(kind, **kwargs):
        try: yield Provider()
        finally: closed.set()
    monkeypatch.setattr(voice_turn, 'mistral_live', provider)
    monkeypatch.setattr(voice_turn, 'live_remaining', lambda _: 60)
    async def is_live(_): return live
    monkeypatch.setattr(voice_turn.chat_context, 'is_live', is_live)
    async def events():
        yield DeltaEvent(text='Hallo. ')
        await asyncio.Future()
    socket = Socket()
    task = asyncio.create_task(voice_turn.run_voice_turn(socket, events(), 'context'))
    await asyncio.wait_for(started.wait(), 1)
    live = False
    await asyncio.wait_for(task, 1)
    assert closed.is_set()
    output = []
    while not socket.output.empty(): output.append(await socket.output.get())
    assert any(event.get('code') == 'context_revoked' for event in output)
    assert not any(event['event'] == 'voice_audio' for event in output)


async def test_disconnect_cancels_provider_and_chat(monkeypatch):
    started, provider_closed, chat_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Provider:
        async def append(self, text): started.set()
        async def finish(self): pass
        async def receive(self): await asyncio.Future()
    @asynccontextmanager
    async def provider(kind, **kwargs):
        try: yield Provider()
        finally: provider_closed.set()
    class DisconnectedSocket(Socket):
        async def receive_json(self):
            await self.input.get()
            raise ConnectionError('synthetic disconnect')
    monkeypatch.setattr(voice_turn, 'mistral_live', provider)
    monkeypatch.setattr(voice_turn, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(voice_turn.chat_context, 'is_live', AsyncMock(return_value=True))
    async def events():
        try:
            yield DeltaEvent(text='Hallo. ')
            await asyncio.Future()
        finally: chat_closed.set()
    socket = DisconnectedSocket()
    task = asyncio.create_task(voice_turn.run_voice_turn(socket, events(), 'context'))
    await asyncio.wait_for(started.wait(), 1)
    await socket.input.put(None)
    with pytest.raises(ConnectionError): await asyncio.wait_for(task, 1)
    assert provider_closed.is_set() and chat_closed.is_set()


async def test_transient_speech_error_keeps_the_saved_chat_reply(monkeypatch):
    class Provider:
        async def append(self, text): raise ValueError('private provider content')
    @asynccontextmanager
    async def provider(kind, **kwargs): yield Provider()
    monkeypatch.setattr(voice_turn, 'mistral_live', provider)
    monkeypatch.setattr(voice_turn, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(voice_turn.chat_context, 'is_live', AsyncMock(return_value=True))
    async def events():
        yield DeltaEvent(text='Hallo. ')
        yield DoneEvent(conversation_id='c', message_id='saved', stopped=False)
    socket = Socket()
    task = asyncio.create_task(voice_turn.run_voice_turn(socket, events(), 'context'))
    output = []
    while True:
        event = await asyncio.wait_for(socket.output.get(), 1); output.append(event)
        if event['event'] == 'voice_audio_end': break
    assert [item['event'] for item in output] == ['delta', 'voice_text_done', 'voice_audio_error', 'voice_audio_end']
    await socket.input.put({'event': 'playback_finished'})
    assert (await asyncio.wait_for(socket.output.get(), 1))['message_id'] == 'saved'
    await asyncio.wait_for(task, 1)


async def test_revocation_after_audio_is_sent_prevents_done_until_playback(monkeypatch):
    live = True
    class Provider:
        async def append(self, text): pass
        async def finish(self): self.first = True
        async def receive(self):
            if self.first:
                self.first = False
                return bytes(4)
            return None
    @asynccontextmanager
    async def provider(kind, **kwargs): yield Provider()
    monkeypatch.setattr(voice_turn, 'mistral_live', provider)
    monkeypatch.setattr(voice_turn, 'live_remaining', lambda _: 60)
    async def is_live(_): return live
    monkeypatch.setattr(voice_turn.chat_context, 'is_live', is_live)
    async def events():
        yield DeltaEvent(text='Synthetisch.')
        yield DoneEvent(conversation_id='c', message_id='new', stopped=False)
    socket = Socket()
    task = asyncio.create_task(voice_turn.run_voice_turn(socket, events(), 'context'))
    output = []
    while True:
        event = await asyncio.wait_for(socket.output.get(), 1); output.append(event)
        if event['event'] == 'voice_audio_end': break
    live = False
    event = await asyncio.wait_for(socket.output.get(), 1)
    assert event == {'event': 'error', 'code': 'context_revoked'}
    await asyncio.wait_for(task, 1)
    assert not any(e['event'] == 'done' for e in output)


async def test_provider_401_marks_speech_failure_terminal_without_losing_text(monkeypatch):
    from websockets.exceptions import InvalidStatus
    @asynccontextmanager
    async def provider(kind, **kwargs):
        raise InvalidStatus(SimpleNamespace(status_code=401))
        yield
    monkeypatch.setattr(voice_turn, 'mistral_live', provider)
    monkeypatch.setattr(voice_turn, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(voice_turn.chat_context, 'is_live', AsyncMock(return_value=True))
    async def events():
        yield DeltaEvent(text='Hallo. ')
        yield DoneEvent(conversation_id='c', message_id='saved', stopped=False)
    socket = Socket()
    task = asyncio.create_task(voice_turn.run_voice_turn(socket, events(), 'context'))
    output = []
    while True:
        event = await asyncio.wait_for(socket.output.get(), 1); output.append(event)
        if event['event'] == 'voice_audio_end': break
    assert any(event['event'] == 'voice_audio_error' and event['terminal'] for event in output)
    await socket.input.put({'event': 'playback_finished'})
    assert (await asyncio.wait_for(socket.output.get(), 1))['message_id'] == 'saved'
    await asyncio.wait_for(task, 1)


async def test_proposal_cards_are_forwarded_but_never_sent_to_speech(monkeypatch):
    from datetime import datetime, timezone
    from api.mail.chat_events import ProposalEvent, StatusEvent
    from api.mail.chat_schemas import ChatExtrasOut, ChatMessageOut
    spoken = []
    class Provider:
        async def append(self, text): spoken.append(text)
        async def finish(self): pass
        async def receive(self): return None
    @asynccontextmanager
    async def provider(kind, **kwargs): yield Provider()
    monkeypatch.setattr(voice_turn, 'mistral_live', provider)
    monkeypatch.setattr(voice_turn, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(voice_turn.chat_context, 'is_live', AsyncMock(return_value=True))
    async def events():
        yield StatusEvent(code='preparing_proposal')
        yield DeltaEvent(text='Bitte bestätige die Karte.')
        yield ProposalEvent(message=ChatMessageOut(id='p', conversation_id='c', role='proposal', content='synthetic card payload', tool_name='', created_at=datetime.now(timezone.utc), proposal=None, proposal_status='pending', stopped=False, extras=ChatExtrasOut()))
        yield DoneEvent(conversation_id='c', message_id='saved', stopped=False)
    socket = Socket()
    task = asyncio.create_task(voice_turn.run_voice_turn(socket, events(), 'context'))
    output = []
    while True:
        event = await asyncio.wait_for(socket.output.get(), 1); output.append(event)
        if event['event'] == 'voice_audio_end': break
    assert any(event['event'] == 'proposal' for event in output)
    assert spoken == ['Bitte bestätige die Karte.']
    await socket.input.put({'event': 'playback_finished'})
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize(('chunks', 'expected'), [
    (['Um 10.', '30 Uhr.'], ['Um 10.30 Uhr.']),
    (['Der Wert ist 3.', '14 Euro.'], ['Der Wert ist 3.14 Euro.']),
    (['Datum 22.', '11.2026.'], ['Datum 22.11.2026.']),
    (['Zum Beispiel z.', 'B. heute.'], ['Zum Beispiel z.B.', ' heute.']),
])
async def test_sentence_boundaries_do_not_depend_on_delta_chunks(monkeypatch, chunks, expected):
    spoken = []
    class Provider:
        async def append(self, text): spoken.append(text)
        async def finish(self): pass
        async def receive(self): return None
    @asynccontextmanager
    async def provider(kind, **kwargs): yield Provider()
    monkeypatch.setattr(voice_turn, 'mistral_live', provider)
    monkeypatch.setattr(voice_turn, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(voice_turn.chat_context, 'is_live', AsyncMock(return_value=True))
    async def events():
        for chunk in chunks: yield DeltaEvent(text=chunk)
        yield DoneEvent(conversation_id='c', message_id='m', stopped=False)
    socket = Socket()
    task = asyncio.create_task(voice_turn.run_voice_turn(socket, events(), 'context'))
    while (await asyncio.wait_for(socket.output.get(), 1))['event'] != 'voice_audio_end': pass
    assert spoken == expected
    await socket.input.put({'event': 'playback_finished'})
    await asyncio.wait_for(task, 1)

@pytest.mark.parametrize(('pace', 'expected'), [('slow', '[pace=slow]Hallo.'), ('normal', 'Hallo.'), ('fast', '[pace=fast]Hallo.')])
async def test_pace_tags_are_only_sent_to_provider(monkeypatch, pace, expected):
    spoken = []
    class Provider:
        async def append(self, text): spoken.append(text)
        async def finish(self): pass
        async def receive(self): return None
    @asynccontextmanager
    async def provider(kind, **kwargs): yield Provider()
    monkeypatch.setattr(voice_turn, 'mistral_live', provider)
    monkeypatch.setattr(voice_turn, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(voice_turn.chat_context, 'is_live', AsyncMock(return_value=True))
    async def events():
        yield DeltaEvent(text='Hallo.')
        yield DoneEvent(conversation_id='c', message_id='m', stopped=False)
    socket = Socket()
    task = asyncio.create_task(voice_turn.run_voice_turn(socket, events(), 'context', locale='de', pace=pace))
    output = []
    while True:
        event = await asyncio.wait_for(socket.output.get(), 1); output.append(event)
        if event['event'] == 'voice_audio_end': break
    assert spoken == [expected]
    assert next(event['text'] for event in output if event['event'] == 'delta') == 'Hallo.'
    await socket.input.put({'event': 'playback_finished'})
    await asyncio.wait_for(task, 1)

async def test_voice_metrics_are_content_free(monkeypatch, caplog):
    import logging
    class Provider:
        async def append(self, text): pass
        async def finish(self): self.sent = False
        async def receive(self):
            if self.sent: return None
            self.sent = True
            return bytes(9600)
    @asynccontextmanager
    async def provider(kind, **kwargs): yield Provider()
    monkeypatch.setattr(voice_turn, 'mistral_live', provider)
    monkeypatch.setattr(voice_turn, 'live_remaining', lambda _: 60)
    monkeypatch.setattr(voice_turn.chat_context, 'is_live', AsyncMock(return_value=True))
    async def events():
        yield DeltaEvent(text='Synthetic private sentence.')
        yield DoneEvent(conversation_id='private-conversation', message_id='private-message', stopped=False)
    caplog.set_level(logging.INFO, logger=voice_turn.__name__)
    socket = Socket()
    task = asyncio.create_task(voice_turn.run_voice_turn(socket, events(), 'private-context', locale='de', pace='slow', stt_elapsed_ms=480))
    while (await asyncio.wait_for(socket.output.get(), 1))['event'] != 'voice_audio_end': pass
    await socket.input.put({'event': 'playback_finished'})
    await asyncio.wait_for(task, 1)
    assert 'voice_metrics locale=de pace=slow stt_elapsed_ms=480' in caplog.text
    assert 'first_token_ms=' in caplog.text and 'first_sentence_ms=' in caplog.text and 'first_audio_ms=' in caplog.text
    assert 'gap_ms=' in caplog.text and 'audio_seconds_per_character=' in caplog.text
    assert 'Synthetic private sentence' not in caplog.text and 'private-conversation' not in caplog.text
    assert 'private-message' not in caplog.text and 'private-context' not in caplog.text
