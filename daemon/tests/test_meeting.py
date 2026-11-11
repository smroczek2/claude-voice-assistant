"""RE_NOME: someone who introduces themselves gets a name; a normal meeting sentence does not change the speaker."""

import pytest

from voz.meeting import RE_NOME


def nome(frase: str) -> str | None:
    m = RE_NOME.search(frase)
    return m.group(1) if m else None


@pytest.mark.parametrize(
    "frase, esperado",
    [
        ("Eu sou o João.", "João"),
        ("eu sou o João Silva", "João Silva"),
        ("Eu sou a Inês Costa.", "Inês Costa"),
        ("Sou a Maria.", "Maria"),
        ("Sou Maria.", "Maria"),
        ("Chamo-me Rui.", "Rui"),
        ("O meu nome é Olga.", "Olga"),
        ("Este é o Pedro.", "Pedro"),
        ("Esta é a Marta.", "Marta"),
        ("Aqui fala o Tiago.", "Tiago"),
        ("Aqui é a Ana.", "Ana"),
        ("Fala o Rui.", "Rui"),
        # mid-sentence: this is how people introduce themselves
        ("Bom dia, eu sou o João Silva.", "João Silva"),
        ("Olá, sou a Marta.", "Marta"),
        # the introduction phrase is case-insensitive
        ("EU SOU o João", "João"),
        ("Sou O Pedro", "Pedro"),
    ],
)
def test_introductions_yield_a_name(frase, esperado):
    assert nome(frase) == esperado


@pytest.mark.parametrize(
    "frase, esperado",
    [
        ("Chamo-me Ana", "Ana"),
        ("Sou Olga", "Olga"),
        ("Eu sou Ana Silva", "Ana Silva"),
        ("Sou Óscar", "Óscar"),
    ],
)
def test_the_article_does_not_eat_the_name_initial(frase, esperado):
    assert nome(frase) == esperado


@pytest.mark.parametrize(
    "frase",
    [
        "eu sou contra isso",
        "Eu sou contra isso.",
        "quem fala primeiro",
        "Quem fala primeiro?",
        "Este é o problema.",
        "Esta é a questão.",
        "aqui é a minha opinião",
        "Sou de opinião que não.",
        "Este é eu, Pedro.",
        "assou o frango",
    ],
)
def test_normal_sentences_yield_no_name(frase):
    assert nome(frase) is None


def test_lowercase_name_does_not_count():
    # Accepted cost: Whisper writes names with a capital letter; the panel still allows renaming.
    assert nome("eu sou o joão") is None

