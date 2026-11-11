# Claude Voice Assistant

Talk to Claude Desktop and hear the answer. Your Mac turns your speech into text and reads Claude's replies aloud. Claude does the thinking. The speech never leaves your Mac.

- **Listen:** local Whisper and Parakeet models turn your speech into text.
- **Think:** one Claude Desktop Code session answers you. It can also check on and steer your other sessions.
- **Speak:** a local voice reads each reply aloud, one sentence at a time.

The best part is how it speaks. Claude normally writes long replies made for reading. The starter profile teaches the voice session to give the result first, in your own words, and to end with the next step. You can read the rules in [profile-template/CLAUDE.md](profile-template/CLAUDE.md). [profile-template/context/speaking.md](profile-template/context/speaking.md) has examples.

## What you need

- A Mac with Apple Silicon (M1 or later) and macOS 14 or later.
- Claude Desktop, with the Code tab.
- About 15 GB of free disk space for the speech models.
- The Xcode Command Line Tools and [uv](https://docs.astral.sh/uv/). [INSTALL.md](INSTALL.md) shows how to get them.
- Headphones help. Without them, the microphone can hear the voice.

## Install

The full steps are in [INSTALL.md](INSTALL.md). In short:

1. Clone this repository and run setup:

   ```sh
   git clone https://github.com/smroczek2/claude-voice-assistant.git ~/Projects/claude-voice-assistant
   cd ~/Projects/claude-voice-assistant
   ./setup.sh
   ```

   Setup checks your Mac, installs the packages, downloads the speech models (about 8 GB), and creates your profile folder `~/voice-orchestrator`. It never opens the microphone. If a download stops, run `./setup.sh` again. It continues where it stopped.

2. In Claude Desktop, open the Code tab and add a saved Local environment:
   - Name: Voice Orchestrator.
   - Folder: `~/voice-orchestrator`.
   - Worktree: off.
   - Environment variable: `CLAUDE_CODE_PLUGIN_DIRS`, set to the full path of this repository folder. Setup prints the exact value.

3. Start a new session in that environment.

## Start and stop the voice

Type these in the voice session:

- `/voice-assistant` starts the voice and listens. The first time, macOS asks for microphone access. Click Allow.
- `/voice-assistant pause` stops listening. The microphone stays open.
- `/voice-assistant resume` listens again.
- `/voice-assistant off` stops the voice and releases the microphone.

To stop a reply, start to talk or press Interrupt in the Voice panel. The Voice panel also has a button for each control. Nothing listens until you turn the voice on.

A paused voice cannot hear "resume". Use the button or the slash command.

## Pick a voice

Open Voice settings in the Voice panel. You can choose the voice, the speed and the delivery style.

- **Chatterbox Turbo** is the default. It needs no setup.
- **Kokoro** is a small model with several American and British English voices.
- **Your voice** is a clone of your own voice. Record 10 to 20 seconds of clear speech and save it as `~/.voz/ref.wav`. Save its exact words in `~/.voz/ref.txt`. Then run `./setup.sh install` to download the clone model (about 3 GB). The recording stays on your Mac.

## Make it yours

Your profile in `~/voice-orchestrator` is plain text, and it belongs to you. Setup and updates never overwrite a file in it.

- `CLAUDE.md` says what the voice session does and how it speaks. Add your own rules to the list at the end.
- `context/about-me.md` holds your projects, the people you mention, and words the speech recognizer gets wrong.

Your private state stays on your Mac, in `~/.claude/voice-orchestrator` and `~/.voz`. None of it goes into a repository.

## When something goes wrong

Run this first:

```sh
./setup.sh check
```

It reports each part and the exact fix for each problem. It changes nothing. [INSTALL.md](INSTALL.md) has a table of common problems, and the steps to update or uninstall.

## Credits and license

By Spencer Mroczek.

Released under the MIT License. See [LICENSE](LICENSE).

This is a personal project with no support. Pull requests are welcome, and you can fork it freely.

To run the tests: `cd daemon && uv run pytest tests ../scripts/tests`.
