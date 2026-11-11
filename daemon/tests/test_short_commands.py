from voz.daemon import _is_short_command


def test_short_replies_count_as_commands():
    for text in ("Start it.", "send it", "Yes!", "go", "Do it."):
        assert _is_short_command(text)


def test_other_short_speech_is_not_a_command():
    for text in ("um", "thank you", "start the video please now"):
        assert not _is_short_command(text)
