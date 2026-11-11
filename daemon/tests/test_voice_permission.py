"""A spoken permission answer allows a tool only when it is a short, clear yes."""
import pytest

from voz.brain import voice_approves


@pytest.mark.parametrize("answer", ["Yes.", "yes please", "Go ahead", "sure", "ok do it", "Sim, pode.", "Yes, go ahead, thanks."])
def test_clear_yes_allows(answer):
    assert voice_approves(answer)


@pytest.mark.parametrize("answer", [
    "don't allow it", "No, don't", "not now", "yes, wait, no", "never", "Não.", "cancel that",
    "I said the token was fine", "allow me to think about whether this is okay first", "", "hmm",
    "maybe later", "please do not approve",
    "don\u2019t allow it", "yes, don\u2019t", "yes, but delete the other file", "yes delete everything",
    "ok?", "do it?", "is it safe? yes", "go-ahead and wipe", "sure, wait",
])
def test_anything_else_refuses(answer):
    assert not voice_approves(answer)
