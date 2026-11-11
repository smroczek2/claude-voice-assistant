"""What the voice says for reply text that is made for the screen."""

from voz.rewriter import limpar_markdown


def test_a_markdown_link_is_spoken_as_its_words():
    text = "The demo is ready in [the demo session](#local_1234)."
    assert limpar_markdown(text) == "The demo is ready in the demo session."


def test_a_bare_url_is_spoken_as_one_word():
    """2026-11-11: a reply held an artifact URL, and the voice read each part of it aloud."""
    text = "The new report is at https://example.com/artifact/Ab3d1EfGh. It has today's state."
    assert limpar_markdown(text) == "The new report is at link. It has today's state."


def test_a_url_in_angle_brackets_or_before_a_comma_keeps_the_sentence():
    assert limpar_markdown("See <https://example.com/a?b=1>, then tell me.") == "See link, then tell me."
    assert limpar_markdown("Open http://localhost:8766/ui now") == "Open link now"


def test_plain_text_with_a_dot_is_not_changed():
    assert limpar_markdown("Version 2.1 is live. The file is notes.md.") == "Version 2.1 is live. The file is notes.md."
