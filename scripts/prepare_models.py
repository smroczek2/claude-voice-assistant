"""Download the pinned models, check their hashes, and warm them without microphone access."""
import asyncio
import time
from pathlib import Path

import mlx_whisper

from voz.config import load_config
from voz.speech import Fala
from voz.intent import Intencao
from voz.models import check_configured, go_offline, load_pins, prepare
from voz.listener import Ouvido


def main():
    cfg = load_config()
    pins = load_pins()
    check_configured(cfg, pins)
    reference = Path(cfg['tts'].get('mlx_ref_audio') or '~/.voz/ref.wav').expanduser()
    wanted = [repo for repo, pin in pins.items() if pin['required'] or (reference.is_file() and repo == cfg['tts']['mlx_model'])]
    prepare(wanted, pins)
    print(f'Pinned models downloaded and hash-checked: {len(wanted)}.', flush=True)
    go_offline()
    if cfg['audio'].get('backend') == 'native':
        import subprocess
        from voz.native_audio import prepare_binary
        helper = prepare_binary()
        subprocess.run([str(helper), '--self-test'], check=True)
        print('Native echo-cancelling audio helper built; microphone not opened.', flush=True)
    started = time.monotonic()
    loop = asyncio.new_event_loop()
    ear = Ouvido(cfg, loop, asyncio.Queue())
    ear._mlx = mlx_whisper
    try:
        ear.warmup()
        print('Speech recognition ready.', flush=True)
    finally:
        ear.stop()
        loop.close()
    voice = Fala(cfg)
    try:
        voice._carregar_turbo()
        print('Chatterbox Turbo bundled voice ready.', flush=True)
        if reference.is_file():
            cfg['tts']['mlx_ref_audio'] = str(reference)
            voice._carregar_mlx()
            print('Qwen reference clone ready.', flush=True)
        else:
            print('No clone reference installed; skipping Qwen preset downloads.', flush=True)
        # Fala loads the configured repository ID, not its hash-named snapshot
        # directory (which MLX cannot use to infer this model's type).
        cfg['tts']['kokoro_voice'] = 'bm_george'
        voice._carregar_kokoro()
        print('Kokoro George ready.', flush=True)
    finally:
        voice._mlx_pool.shutdown(wait=True)
    kw = cfg['keywords']
    if kw['dynamic']:
        classifier = Intencao(kw['model'], True, kw['max_words'])
        try:
            classifier.carregar()
            print('Local short-command classifier ready.', flush=True)
        finally:
            classifier.pool.shutdown(wait=True)
    print(f'All neural speech models and enabled recognition models prepared in {time.monotonic()-started:.1f}s. Microphone was not opened.', flush=True)


if __name__ == '__main__':
    main()
