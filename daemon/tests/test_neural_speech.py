import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from voz.config import load_config
from voz.speech import Fala

@pytest.mark.parametrize('choice', ['say:Karen', 'piper:en_US-example'])
def test_legacy_voices_are_rejected(choice):
    async def run():
        voice=Fala(load_config())
        with pytest.raises(ValueError, match='neural'):
            await voice.mudar_voz(choice)
        await voice.close()
    asyncio.run(run())


def test_kokoro_keeps_every_generated_segment():
    voice=Fala(load_config())
    voice.cfg['backend']='kokoro'
    voice.cfg['kokoro_voice']='af_heart'
    seen={}
    def generate(**kwargs):
        seen.update(kwargs)
        return iter([SimpleNamespace(audio=np.full(240,0.2),sample_rate=24000),SimpleNamespace(audio=np.full(240,0.3),sample_rate=24000)])
    voice._kokoro=SimpleNamespace(generate=generate)
    audio,sr=voice._gerar_audio_mlx('The first segment. The second segment.')
    assert len(audio)==480
    assert sr==24000
    assert seen['voice']=='af_heart'
    assert seen['lang_code']=='a'
    assert seen['speed']==1.0
    voice._mlx_pool.shutdown(wait=True)


def test_neural_failure_is_visible_and_never_reads_with_system_voice():
    async def run():
        errors=[]; legacy=[]
        voice=Fala(load_config(),on_error=errors.append)
        voice.cfg['backend']='mlx'
        voice._mlx=object()
        def fail(text): raise RuntimeError('Generation failed')
        async def old_voice(*args): legacy.append(args)
        voice._gerar_audio_mlx=fail
        voice._speak_say=old_voice
        voice.start()
        voice.dizer('Please use only the selected neural voice.')
        await asyncio.wait_for(voice.esperar(),1)
        assert errors and 'Generation failed' in errors[0]
        assert legacy==[]
        await voice.close()
    asyncio.run(run())


@pytest.mark.parametrize('expression,has_style',[('restrained',True),('conversational',True),('natural',False)])
def test_qwen_expression_is_separate_from_spoken_text(expression,has_style):
    voice=Fala(load_config()); seen={}
    voice.cfg['expression']=expression
    voice._ref_audio=None
    def generate(**kwargs): seen.update(kwargs); return iter(())
    voice._mlx=SimpleNamespace(generate=generate)
    list(voice._gerar_mlx('The notebook is blue.'))
    assert seen['text']=='The notebook is blue.'
    assert bool(seen.get('instruct')) is has_style
    if expression == 'conversational':
        assert 'attentive everyday conversation' in seen['instruct']
    voice._mlx_pool.shutdown(wait=True)


@pytest.mark.parametrize('clone', [False, True])
def test_qwen_uses_checkpoint_matching_explicit_voice_selection(monkeypatch, tmp_path, clone):
    voice = Fala(load_config())
    voice.cfg['mlx_model'] = 'test/Qwen-Base-8bit' if not clone else 'test/Qwen-CustomVoice-8bit'
    voice.cfg['mlx_ref_audio'] = str(tmp_path / 'selected.wav') if clone else ''
    seen = {}
    def generate(**kwargs):
        seen.update(kwargs)
        return iter(())
    def load(repository):
        seen['repository'] = repository
        return SimpleNamespace(config=SimpleNamespace(tts_model_type='base' if clone else 'custom_voice'), generate=generate)
    monkeypatch.setattr('voz.speech._load_neural_model', load)
    voice._carregar_mlx()
    assert seen['repository'] == ('test/Qwen-Base-8bit' if clone else 'test/Qwen-CustomVoice-8bit')
    assert ('ref_audio' in seen) is clone
    assert ('voice' in seen) is not clone
    if not clone:
        assert seen['voice'] == 'Ryan'
    voice._mlx_pool.shutdown(wait=True)


def test_turbo_uses_bundled_voice_keeps_segments_and_preserves_user_rate():
    voice = Fala(load_config())
    voice.cfg.update(backend='turbo', mlx_ref_audio='unused.wav', speed=1.3)
    seen = {}
    def generate(**kwargs):
        seen.update(kwargs)
        return iter([SimpleNamespace(audio=np.full(240, .2), sample_rate=24000), SimpleNamespace(audio=np.full(240, .3), sample_rate=24000)])
    voice._turbo = SimpleNamespace(generate=generate)
    audio, rate = voice._gerar_audio_mlx('The notebook is blue.')
    assert len(audio) == 480 and rate == 24000
    assert seen == dict(text='The notebook is blue.', verbose=False, exaggeration=0.0, cfg_weight=0.0)
    assert voice.voz_actual() == 'turbo:default'
    assert voice.cfg['speed'] == 1.3
    voice._mlx_pool.shutdown(wait=True)


def test_failed_selection_preserves_working_voice_and_reference_state(monkeypatch):
    async def run():
        voice = Fala(load_config())
        voice.cfg.update(backend='mlx', mlx_ref_audio='', mlx_voice='Ryan')
        working = SimpleNamespace(generate=lambda **kwargs: iter(()))
        voice._mlx = working
        voice._mlx_modelo = 'working'
        voice._ref_audio = None
        voice._ref_text = ''
        def fail(repository):
            raise RuntimeError('Model unavailable')
        monkeypatch.setattr('voz.speech._load_neural_model', fail)
        with pytest.raises(RuntimeError, match='Model unavailable'):
            await voice.mudar_voz('mlx:ref:/nonexistent.wav')
        assert voice.voz_actual() == 'mlx:voice:Ryan'
        assert voice._mlx is working and voice._ref_audio is None
        await voice.close()
    asyncio.run(run())


def test_failed_switch_cannot_overwrite_a_later_choice():
    async def run():
        voice = Fala(load_config())
        started, release = asyncio.Event(), asyncio.Event()
        visited = []
        async def select(choice):
            visited.append(choice)
            voice.cfg['backend'] = choice
            if choice == 'first':
                started.set()
                await release.wait()
                raise RuntimeError('First selection failed')
            return choice
        voice._select_voice = select
        first = asyncio.create_task(voice.mudar_voz('first'))
        await started.wait()
        second = asyncio.create_task(voice.mudar_voz('second'))
        await asyncio.sleep(0)
        queued = visited == ['first']
        voice.cfg['speed'] = 1.3
        voice.cfg['expression'] = 'natural'
        release.set()
        result = await asyncio.gather(first, second, return_exceptions=True)
        assert queued, 'Voice selection must serialize configuration and model preparation'
        assert isinstance(result[0], RuntimeError)
        assert voice.cfg['backend'] == 'second'
        assert voice.cfg['speed'] == 1.3
        assert voice.cfg['expression'] == 'natural'
        await voice.close()
    asyncio.run(run())


def test_kokoro_loading_cannot_redownload_removed_voice_packs(monkeypatch):
    import fnmatch, sys
    from voz.speech import _load_neural_model
    requested = {}
    def load(repository, **kwargs):
        requested.update(repository=repository, **kwargs)
        return 'loaded'
    monkeypatch.setitem(sys.modules, 'mlx_audio.tts.utils', SimpleNamespace(load_model=load))
    assert _load_neural_model('mlx-community/Kokoro-82M-bf16') == 'loaded'
    patterns = requested.get('allow_patterns', ['*'])
    allowed = lambda name: any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
    assert allowed('config.json') and allowed('kokoro-v1_0.safetensors')
    assert allowed('voices/bm_george.safetensors')
    assert not allowed('voices/af_heart.safetensors')
    assert not allowed('voices/bf_emma.safetensors')
