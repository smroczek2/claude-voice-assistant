"""The user-facing voice catalog contains only local neural English voices."""
import pytest

from voz import voices


KOKORO = {'af_heart', 'af_bella', 'am_michael', 'bf_emma', 'bm_george'}
QWEN = {'Ryan', 'Aiden', 'Serena', 'Vivian', 'Dylan', 'Eric', 'Uncle_Fu', 'Ono_Anna', 'Sohee'}
EXPECTED = {'kokoro:bm_george', 'turbo:default'}


@pytest.fixture
def local_catalog(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(voices, 'KOKORO_CACHE', tmp_path / 'kokoro', raising=False)
    monkeypatch.setattr(voices, 'vozes_say', lambda: calls.append('apple') or [])
    def no_network(*args, **kwargs):
        pytest.fail('Opening the voice catalog must not access the network')
    import httpx
    monkeypatch.setattr(httpx, 'get', no_network)
    monkeypatch.setattr(httpx, 'stream', no_network)
    return calls


def test_catalog_exposes_only_turbo_and_george_without_legacy_discovery(local_catalog):
    result = voices.procurar('en', None, [])
    assert {v['id'] for v in result} == EXPECTED
    assert local_catalog == []
    assert all(v['source'] in {'kokoro', 'turbo'} for v in result)


def test_kokoro_availability_is_per_voice_and_local(local_catalog):
    packs = voices.KOKORO_CACHE / 'snapshots' / 'test-revision' / 'voices'
    packs.mkdir(parents=True)
    # Another voice's pack does not make George usable.
    (packs / 'af_heart.safetensors').write_bytes(b'fixture')
    assert {v['id']: v for v in voices.procurar('en', None, [])}['kokoro:bm_george']['installed'] is False
    # A broken cache link does not either.
    (packs / 'bm_george.safetensors').symlink_to(packs / 'missing')
    assert {v['id']: v for v in voices.procurar('en', None, [])}['kokoro:bm_george']['installed'] is False
    (packs / 'bm_george.safetensors').unlink()
    (packs / 'bm_george.safetensors').write_bytes(b'fixture')
    assert {v['id']: v for v in voices.procurar('en', None, [])}['kokoro:bm_george']['installed'] is True


def test_neural_search_and_english_regions(local_catalog):
    assert {v['id'] for v in voices.procurar('en-US', 'kokoro', [])} == {'kokoro:bm_george'}
    assert voices.procurar('en', 'Ryan', []) == []
    assert {v['id'] for v in voices.procurar(None, None, [])} == EXPECTED
    assert voices.procurar('en', 'Karen', []) == []
    assert voices.procurar('fr', None, []) == []


def test_reference_clones_remain_selectable(local_catalog, tmp_path):
    ref = tmp_path / 'My voice.wav'
    ref.write_bytes(b'fixture')
    result = voices.procurar('en', 'My voice', [ref])
    assert len(result) == 1
    assert result[0]['id'] == f'mlx:ref:{ref}'
    assert result[0]['source'] == 'qwen-clone'
    assert result[0]['installed'] is True
