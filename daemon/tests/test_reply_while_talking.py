"""A reply that arrives while the user talks must play when the user's turn ends.

2026-11-11: the user said that a reply which arrived while he talked was never heard, and that
nothing told him a message came. The daemon held the sentences, then deleted them when it
recognized his words."""
import asyncio
from types import SimpleNamespace

from voz.config import load_config
from voz.daemon import Daemon

REAL_WORDS = {"text": "Please send the report to the team today.", "dur": 2.5, "logprob": -0.2}


def daemon_fixture(turn_gap_ms: int = 0):
    """A daemon with a fake voice. `log` holds, in order, each sent turn and each sentence given to the voice."""
    cfg = load_config()
    cfg["vad"]["turn_gap_ms"] = turn_gap_ms
    cfg["ui"]["ack_sound"] = False
    cfg["keywords"]["dynamic"] = False
    daemon = Daemon(cfg, 0)
    daemon.loop = asyncio.get_running_loop()
    daemon.events = asyncio.Queue()
    daemon.ouvido = SimpleNamespace(sr=16000, set_muted=lambda on: None, stop=lambda: None)
    log: list[tuple[str, str]] = []
    voice = SimpleNamespace(paused=False, busy=False, stopped=0)

    async def stop():
        voice.stopped += 1
        voice.busy = False

    async def pause():
        voice.paused = True

    def resume():
        voice.paused = False

    voice.stop, voice.skip, voice.pause, voice.resume = stop, stop, pause, resume
    voice.dizer = lambda sentence: log.append(("voice", sentence))
    daemon.fala = voice
    daemon.out = lambda kind, **payload: log.append(("sent", payload["text"])) if kind == "utterance" else None
    daemon._limpar = lambda text: asyncio.sleep(0, result=text)
    return daemon, log


async def run_events(daemon, *events):
    """Feed capture events to the main loop one at a time, so a test can act between them."""
    for event in events:
        daemon.events.put_nowait(event)
    daemon.events.put_nowait(("quit", None))
    await daemon._main_loop()


async def settle(daemon):
    """Let the turn-gap task send the turn."""
    while daemon._gap_task:
        await asyncio.sleep(0)


def test_a_reply_that_arrives_while_the_user_talks_plays_after_the_turn_is_sent():
    async def scenario():
        daemon, log = daemon_fixture()
        await run_events(daemon, ("speech_start", None))
        await daemon._on_cmd({"type": "say", "text": "The demo video is ready. It runs two minutes."})
        assert log == [], "the voice must not start while the user talks"
        await run_events(daemon, ("rec_end", 2.5), ("utterance", REAL_WORDS))
        await settle(daemon)
        assert log == [("sent", REAL_WORDS["text"]),
                       ("voice", "The demo video is ready."), ("voice", "It runs two minutes.")]
    asyncio.run(scenario())


def test_a_reply_does_not_start_in_the_pause_between_two_sentences_of_one_turn():
    async def scenario():
        daemon, log = daemon_fixture(turn_gap_ms=60_000)
        await run_events(daemon, ("speech_start", None), ("rec_end", 2.5), ("utterance", REAL_WORDS))
        await daemon._on_cmd({"type": "say", "text": "The build passed."})
        assert log == [], "words that are not sent yet mean the turn is open"
        await run_events(daemon, ("speech_start", None), ("rec_end", 1.5),
                         ("utterance", {"text": "And copy the manager too.", "dur": 1.5, "logprob": -0.2}))
        assert log == []
        daemon.cfg["vad"]["turn_gap_ms"] = 0
        daemon._schedule_gap(daemon._capture_generation)
        await settle(daemon)
        assert log == [("sent", "Please send the report to the team today. And copy the manager too."),
                       ("voice", "The build passed.")]
    asyncio.run(scenario())


def test_a_noise_inside_an_open_turn_keeps_the_reply_for_the_end_of_the_turn():
    async def scenario():
        daemon, log = daemon_fixture(turn_gap_ms=60_000)
        await run_events(daemon, ("speech_start", None), ("rec_end", 2.5), ("utterance", REAL_WORDS))
        await daemon._on_cmd({"type": "say", "text": "The build passed."})
        await run_events(daemon, ("speech_start", None), ("rec_end", 0.2), ("speech_discarded", "Noise"))
        assert log == [], "a noise does not close a turn that still has words to send"
        daemon.cfg["vad"]["turn_gap_ms"] = 0
        daemon._schedule_gap(daemon._capture_generation)
        await settle(daemon)
        assert log == [("sent", REAL_WORDS["text"]), ("voice", "The build passed.")]
    asyncio.run(scenario())


def test_a_noise_with_no_words_lets_the_reply_play_at_once():
    async def scenario():
        daemon, log = daemon_fixture()
        await run_events(daemon, ("speech_start", None))
        await daemon._on_cmd({"type": "say", "text": "The build passed."})
        await run_events(daemon, ("rec_end", 0.2), ("speech_discarded", "Noise"))
        assert log == [("voice", "The build passed.")]
    asyncio.run(scenario())


def test_sentences_held_while_the_voice_hears_its_own_echo_play_in_order():
    async def scenario():
        daemon, log = daemon_fixture()
        await daemon._on_cmd({"type": "say", "text": "The pricing page change is merged and live."})
        daemon.fala.busy = True  # the voice is audible
        await run_events(daemon, ("speech_start", None))
        await daemon._on_cmd({"type": "say", "text": "Nothing is left to do."})
        await run_events(daemon, ("rec_end", 2.0),
                         ("utterance", {"text": "the pricing page change is merged and live", "dur": 2.0, "logprob": -0.2}))
        await daemon._on_cmd({"type": "say", "text": "Okay to close it?"})
        assert log == [("voice", "The pricing page change is merged and live."),
                       ("voice", "Nothing is left to do."), ("voice", "Okay to close it?")]
        assert daemon.fala.paused is False and daemon.fala.stopped == 0
    asyncio.run(scenario())


def test_talking_over_the_voice_still_stops_what_it_had_to_say():
    async def scenario():
        daemon, log = daemon_fixture()
        await daemon._on_cmd({"type": "say", "text": "Here is the long explanation."})
        daemon.fala.busy = True  # the voice is audible, and the user talks over it
        await run_events(daemon, ("speech_start", None))
        await daemon._on_cmd({"type": "say", "text": "This is the next part of it."})
        await run_events(daemon, ("rec_end", 2.5), ("utterance", REAL_WORDS))
        await settle(daemon)
        assert log == [("voice", "Here is the long explanation."), ("sent", REAL_WORDS["text"])]
        assert daemon.fala.stopped >= 1
    asyncio.run(scenario())


def test_a_reply_that_arrives_after_the_user_talked_over_the_voice_plays_when_the_turn_ends():
    async def scenario():
        daemon, log = daemon_fixture(turn_gap_ms=60_000)
        await daemon._on_cmd({"type": "say", "text": "Here is the long explanation."})
        daemon.fala.busy = True
        await run_events(daemon, ("speech_start", None), ("rec_end", 2.5), ("utterance", REAL_WORDS))
        await daemon._on_cmd({"type": "say", "text": "A new message came from the plans session."})
        daemon.cfg["vad"]["turn_gap_ms"] = 0
        daemon._schedule_gap(daemon._capture_generation)
        await settle(daemon)
        assert log == [("voice", "Here is the long explanation."), ("sent", REAL_WORDS["text"]),
                       ("voice", "A new message came from the plans session.")]
    asyncio.run(scenario())


def test_a_stop_command_drops_the_held_reply():
    async def scenario():
        daemon, log = daemon_fixture()
        await run_events(daemon, ("speech_start", None))
        await daemon._on_cmd({"type": "say", "text": "The build passed."})
        await daemon._on_cmd({"type": "stop"})
        await run_events(daemon, ("rec_end", 0.2), ("speech_discarded", "Noise"))
        assert log == []
    asyncio.run(scenario())
