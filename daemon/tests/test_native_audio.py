"""Protocol integration without opening hardware; live AEC is tested separately."""
import asyncio
import json
import sys

import pytest

from voz.native_audio import NativeAudio


def test_native_capture_play_stop_and_shutdown_protocol(tmp_path, monkeypatch):
    helper = tmp_path / 'fake-audio'
    commands = tmp_path / 'commands.jsonl'
    helper.write_text('#!' + sys.executable + '\n' + '''
import json, sys, base64
print(json.dumps({'type':'ready','voice_processing':True,'sample_rate':16000}),flush=True)
print(json.dumps({'type':'audio','pcm':base64.b64encode(b'\\x01\\x00'*512).decode()}),flush=True)
active=None
for line in sys.stdin:
 c=json.loads(line)
 with open(COMMAND_PATH, 'a') as log: log.write(line)
 if c['type']=='play': active=c['id']
 elif c['type']=='stop' and active is not None:
  print(json.dumps({'type':'played','id':active,'cancelled':True}),flush=True);active=None
 elif c['type']=='quit': break
'''.replace('COMMAND_PATH', repr(str(commands))))
    helper.chmod(0o700)
    monkeypatch.setattr('voz.native_audio.prepare_binary', lambda: helper)
    async def scenario():
        capture = []
        audio = NativeAudio(capture.append)
        await asyncio.to_thread(audio.start)
        for _ in range(20):
            if capture:
                break
            await asyncio.sleep(0.01)
        assert capture == [b'\x01\x00'*512]
        pending = audio.play_file('/unused-test.wav')
        assert not pending.done()
        audio.stop_playback()
        await asyncio.wait_for(pending, 1)
        faster = audio.play_file('/faster-test.wav', rate=1.4)
        audio.set_rate(0.8)
        audio.pause_playback()
        audio.resume_playback()
        audio.stop_playback()
        await asyncio.wait_for(faster, 1)
        audio.mute(True)
        audio.stop()
        assert audio.process.poll() == 0
        received = [json.loads(line) for line in commands.read_text().splitlines()]
        assert [c for c in received if c['type'] == 'play'] == [
            {'type': 'play', 'id': 1, 'path': '/unused-test.wav', 'rate': 1.0},
            {'type': 'play', 'id': 2, 'path': '/faster-test.wav', 'rate': 1.4},
        ]
        assert received[3:6] == [{'type': 'rate', 'value': 0.8}, {'type': 'pause'}, {'type': 'resume'}]
    asyncio.run(scenario())


@pytest.mark.parametrize('rate', [float('nan'), float('inf'), -float('inf'), 0.49, 2.01, True, 'fast', None])
def test_invalid_rate_never_reaches_native_helper(monkeypatch, rate):
    async def scenario():
        audio = NativeAudio(lambda _: None)
        audio.info = {'voice_processing': True}
        sent = []
        monkeypatch.setattr(audio, 'send', sent.append)
        with pytest.raises(ValueError, match='rate'):
            audio.play_file('/unused.wav', rate=rate)
        with pytest.raises(ValueError, match='rate'):
            audio.set_rate(rate)
        assert sent == []
    asyncio.run(scenario())


def test_route_change_updates_device_and_drops_partial_audio():
    chunks, routes = [], []
    audio = NativeAudio(chunks.append, on_route=routes.append)
    audio._event({'type': 'ready', 'voice_processing': True, 'mic': 'MacBook Pro Microphone', 'output': 'Speakers'})
    audio._event({'type': 'audio', 'pcm': 'AAAA'})  # 3 bytes: a partial chunk from the old device
    audio._event({'type': 'route', 'mic': 'AirPods Pro', 'output': 'AirPods Pro'})
    assert audio.info['mic'] == 'AirPods Pro' and audio.info['voice_processing'] is True
    assert routes == [audio.info]
    assert not audio._pcm and not chunks
