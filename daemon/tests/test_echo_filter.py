"""Speaker-echo screening: only an in-order copy of recent speech counts as echo."""

from voz.daemon import _looks_like_echo

SAID = ["The only thing left for you now is the Notion yes in the UAT orchestrator's session."]


def test_reply_that_reuses_topic_words_is_not_echo():
    assert not _looks_like_echo("Yes, on the two UAT Notion edits.", SAID)


def test_exact_echo_is_echo():
    assert _looks_like_echo("the only thing left for you now", SAID)


def test_echo_with_punctuation_and_case_changes_is_echo():
    assert _looks_like_echo("Notion, yes in the UAT orchestrator's session", SAID)


def test_echo_plus_short_tail_is_echo():
    assert _looks_like_echo("the only thing left for you now is the notion okay", SAID)


def test_short_fragment_that_matches_is_echo():
    assert _looks_like_echo("the Notion", SAID)


def test_short_reply_not_in_speech_is_not_echo():
    assert not _looks_like_echo("Notion first", SAID)


def test_empty_transcript_is_echo():
    assert _looks_like_echo("  ...  ", SAID)


def test_no_recent_speech_means_no_echo():
    assert not _looks_like_echo("the only thing left", [])


def test_only_last_six_lines_count():
    old = ["the only thing left for you now"] + ["Other line."] * 6
    assert not _looks_like_echo("the only thing left for you now", old)
