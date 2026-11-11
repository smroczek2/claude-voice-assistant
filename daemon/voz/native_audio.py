"""Shared macOS voice-processing capture/playback; JSONL over an owned subprocess."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import subprocess
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger('voz.native_audio')
ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'native' / 'VoiceAudio.swift'
BINARY = ROOT / '.native' / 'voice-audio'
# Owner-only build cache next to the binary, not in the shared /private/tmp.
SWIFT_CACHE = ROOT / '.native' / 'swift-cache'


def prepare_binary() -> Path:
    if not SOURCE.is_file():
        raise RuntimeError('Native voice-processing source is missing')
    if not BINARY.is_file() or BINARY.stat().st_mtime < SOURCE.stat().st_mtime:
        BINARY.parent.mkdir(mode=0o700, exist_ok=True)
        SWIFT_CACHE.mkdir(mode=0o700, exist_ok=True)
        result = subprocess.run(
            ['swiftc', '-swift-version', '5', '-O', '-module-cache-path', str(SWIFT_CACHE), str(SOURCE), '-o', str(BINARY)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode:
            raise RuntimeError(f'Native audio build failed: {result.stderr[-1500:]}')
    return BINARY


class NativeAudio:
    def __init__(self, on_audio: Callable[[bytes], None], on_failure: Callable[[str], None] | None = None,
                 on_route: Callable[[dict], None] | None = None):
        self.on_audio = on_audio
        self.on_failure = on_failure
        self.on_route = on_route
        self.info: dict = {}
        self.process: subprocess.Popen | None = None
        self._ready = threading.Event()
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._error = ''
        self._pcm = bytearray()
        self._next_id = 0
        self._plays: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}

    def start(self) -> None:
        binary = prepare_binary()
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError('Audio startup was cancelled')
            self.process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self._read, name='native-capture', daemon=True).start()
        threading.Thread(target=self._errors, name='native-diagnostics', daemon=True).start()
        if not self._ready.wait(30):
            self.stop()
            raise RuntimeError('Native audio did not become ready; check microphone permission')
        if self._error or self._closed or not self.info.get('voice_processing'):
            self.stop()
            raise RuntimeError(self._error or 'Echo-cancelled audio did not start')

    def _read(self) -> None:
        try:
            assert self.process and self.process.stdout
            for line in self.process.stdout:
                self._event(json.loads(line))
        except Exception as exc:
            self._error = str(exc)
        finally:
            if not self.info:
                self._error = self._error or 'Native audio exited before opening the microphone'
            self._ready.set()
            for ident in list(self._plays):
                self._finish_play(ident, RuntimeError('Native audio closed'))
            if not self._closed and self.on_failure:
                self.on_failure(self._error or 'Native audio process exited unexpectedly')

    def _errors(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            log.info('native audio: %s', line.rstrip())

    def _event(self, event: dict) -> None:
        kind = event.get('type')
        if kind == 'ready':
            self.info = event
            self._ready.set()
        elif kind == 'audio' and not self._closed:
            self._pcm.extend(base64.b64decode(event['pcm'], validate=True))
            while len(self._pcm) >= 1024:
                chunk = bytes(self._pcm[:1024])
                del self._pcm[:1024]
                self.on_audio(chunk)
        elif kind == 'route':
            self.info = {**self.info, 'mic': event.get('mic'), 'output': event.get('output')}
            self._pcm.clear()
            log.info('audio device changed: mic=%s output=%s', event.get('mic'), event.get('output'))
            if self.on_route:
                self.on_route(self.info)
        elif kind == 'played':
            self._finish_play(int(event['id']))
        elif kind in {'error', 'fatal'}:
            self._error = str(event.get('message') or event.get('error') or 'Native audio failed')
            if 'id' in event:
                self._finish_play(int(event['id']), RuntimeError(self._error))
            else:
                self._ready.set()

    def _finish_play(self, ident: int, error: Exception | None = None) -> None:
        pending = self._plays.pop(ident, None)
        if not pending:
            return
        loop, future = pending
        def finish():
            if not future.done():
                if error:
                    future.set_exception(error)
                else:
                    future.set_result(None)
        if not loop.is_closed():
            loop.call_soon_threadsafe(finish)

    def send(self, command: dict) -> None:
        with self._write_lock:
            if not self.process or self.process.poll() is not None or not self.process.stdin:
                raise RuntimeError('Native audio is not running')
            self.process.stdin.write(json.dumps(command) + '\n')
            self.process.stdin.flush()

    @staticmethod
    def _rate(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.5 <= value <= 2.0:
            raise ValueError('Playback rate must be a finite number between 0.5 and 2.0')
        return float(value)

    def play_file(self, path: str, rate: float = 1.0) -> asyncio.Future:
        rate = self._rate(rate)
        if self._closed or not self.info.get('voice_processing'):
            raise RuntimeError('Echo-cancelled audio is not ready')
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._next_id += 1
        ident = self._next_id
        self._plays[ident] = (loop, future)
        try:
            self.send({'type': 'play', 'id': ident, 'path': path, 'rate': rate})
        except Exception:
            self._plays.pop(ident, None)
            raise
        return future

    def set_rate(self, value: float) -> None:
        self.send({'type': 'rate', 'value': self._rate(value)})

    def stop_playback(self) -> None:
        if not self._closed and self.process and self.process.poll() is None:
            self.send({'type': 'stop'})

    def pause_playback(self) -> None:
        self.send({'type': 'pause'})

    def resume_playback(self) -> None:
        self.send({'type': 'resume'})

    def mute(self, on: bool) -> None:
        self._pcm.clear()
        if self.process and self.process.poll() is None:
            self.send({'type': 'mute', 'on': on})

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._closed = True
            process = self.process
        if process and process.poll() is None:
            try:
                self.send({'type': 'quit'})
                process.wait(timeout=1)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        self._ready.set()
