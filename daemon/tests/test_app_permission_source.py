import asyncio
from types import SimpleNamespace

from voz.app import App


def _app():
    app = object.__new__(App)
    notes = []
    app.ui = SimpleNamespace(emit=lambda *a, **k: None)
    app._note = lambda text, warn=False: notes.append(text)
    return app, notes


def test_typed_text_never_answers_a_permission_question():
    async def run():
        app, notes = _app()
        app._pergunta = asyncio.get_running_loop().create_future()
        await app._on_user_text("yes", spoken=False)
        assert not app._pergunta.done()
        assert notes and "by voice" in notes[0]
        await app._on_user_text("yes", spoken=True)
        assert app._pergunta.result() == "yes"

    asyncio.run(run())
