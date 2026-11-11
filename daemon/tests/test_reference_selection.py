"""Recorded references are available choices, never implicit startup overrides."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from voz.config import DEFAULTS, load_config
from voz.daemon import Daemon


def test_existing_home_recording_does_not_override_selected_preset(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    recording = home / '.voz' / 'ref.wav'
    recording.parent.mkdir(parents=True)
    recording.write_bytes(b'fixture recording, never opened')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    config = tmp_path / 'config.toml'
    config.write_text('[tts]\nbackend="mlx"\nmlx_voice="Ryan"\nmlx_ref_audio=""\n')
    selected = load_config(config)['tts']
    assert selected['mlx_ref_audio'] == ''
    assert selected['mlx_voice'] == 'Ryan'
    assert recording.is_file()


def test_explicit_reference_is_preserved_instead_of_home_recording(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.voz').mkdir(parents=True)
    (home / '.voz' / 'ref.wav').write_bytes(b'fixture')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    config = tmp_path / 'config.toml'
    config.write_text('[tts]\nmlx_ref_audio="chosen.wav"\n')
    assert load_config(config)['tts']['mlx_ref_audio'] == str(tmp_path / 'chosen.wav')


def test_default_backend_is_approved_turbo():
    assert DEFAULTS['tts']['backend'] == 'turbo'
    assert DEFAULTS['tts']['turbo_model'] == 'mlx-community/chatterbox-turbo-8bit'


def test_failed_voice_switch_reports_failure_to_controls():
    async def scenario():
        cfg = load_config()
        cfg['keywords']['dynamic'] = False
        daemon = Daemon(cfg, 0)
        daemon.ouvido = SimpleNamespace(recalc=lambda: None)
        emitted = []
        daemon.out = lambda kind, **data: emitted.append((kind, data))
        async def reject(_):
            raise ValueError('Invalid voice selection')
        daemon.fala = SimpleNamespace(mudar_voz=reject, voz_actual=lambda: 'turbo:default')
        try:
            response = await daemon._definir({'voice': 'invalid:voice', 'quiet': True})
            assert response['ok'] is False
            assert 'Invalid voice selection' in response['error']
            assert response['voice'] == 'turbo:default'
            assert not any(kind == 'settings' for kind, _ in emitted)
        finally:
            daemon._intencoes.pool.shutdown(wait=True)
    asyncio.run(scenario())


def test_user_overrides_layer_over_engine_defaults(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.voz').mkdir(parents=True)
    (home / '.voz' / 'config.toml').write_text('[vad]\nturn_gap_ms = 2500\n')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    engine = tmp_path / 'config.toml'
    engine.write_text('[vad]\nturn_gap_ms = 1200\nsilence_ms = 400\n')
    cfg = load_config(engine)
    assert cfg['vad']['turn_gap_ms'] == 2500
    assert cfg['vad']['silence_ms'] == 400
    assert cfg['_user_path'] == str(home / '.voz' / 'config.toml')


def test_engine_defaults_apply_without_user_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'empty-home'))
    engine = tmp_path / 'config.toml'
    engine.write_text('[vad]\nturn_gap_ms = 1300\n')
    cfg = load_config(engine)
    assert cfg['vad']['turn_gap_ms'] == 1300
    assert '_user_path' not in cfg


def test_user_relative_reference_resolves_beside_user_file(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.voz').mkdir(parents=True)
    (home / '.voz' / 'config.toml').write_text('[tts]\nmlx_ref_audio = "mine.wav"\n')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    assert load_config(tmp_path / 'missing.toml')['tts']['mlx_ref_audio'] == str(home / '.voz' / 'mine.wav')
