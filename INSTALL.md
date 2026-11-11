# Install the voice assistant

This takes about 30 minutes, most of it downloads. You need an Apple Silicon Mac (M1 or later), macOS 14 or later, Claude Desktop, and about 15 GB of free disk space.

This is a personal project by Spencer Mroczek, with no support. Pull requests are welcome, and you can fork it freely.

## 1. Get the tools

1. Open Terminal.
2. Install the Command Line Tools. Run `xcode-select --install`. Skip this if they are already installed.
3. Install uv. Run `curl -LsSf https://astral.sh/uv/install.sh | sh`. Then open a new Terminal window.

## 2. Get the code and run setup

1. Clone this repository:
   `git clone https://github.com/smroczek2/claude-voice-assistant.git ~/Projects/claude-voice-assistant`
2. If the clone fails, check the URL and your network.
3. Go to the folder: `cd ~/Projects/claude-voice-assistant`.
4. Run `./setup.sh`.

Setup does these steps:

- It checks the Mac, the tools, the disk space and the voice port.
- It installs the Python packages.
- It downloads the speech models (about 8 GB) and builds the audio helper. It never opens the microphone.
- It creates your profile in `~/voice-orchestrator`.

If a download stops, run `./setup.sh` again. It keeps what it already downloaded and continues.

Options:

- `--name Alex` puts your name in the task ledger's output.
- `--profile ~/somewhere` puts the profile in another folder. The folder must be empty, or a voice profile already.
- Your team can tell you to add `--flavor <name>`, for a profile with your workplace's rules.

## 3. Add the voice environment in Claude Desktop

Setup cannot change Desktop settings, so you do this once.

1. Open Claude Desktop and go to the Code tab.
2. Add a saved Local environment. Name it "Voice Orchestrator".
3. Set its folder to your profile folder, `~/voice-orchestrator`.
4. Turn Worktree off.
5. Add the environment variable `CLAUDE_CODE_PLUGIN_DIRS`. Set it to the full path of this repository folder. Setup prints the exact value.
6. Start a new session in that environment.

You do not need a plugin marketplace. This route works on most managed accounts too. If your account blocks local plugin folders, see "When something goes wrong" below.

## 4. Talk to it

1. In the new session, type `/voice-assistant`.
2. macOS asks for microphone access for Claude. Click Allow.
3. Say "What can you do?" You hear a short answer.

If you clicked Don't Allow, open System Settings, then Privacy & Security, then Microphone. Turn Claude on and restart Claude Desktop.

## Choose a voice

The default voice is Chatterbox Turbo. It needs no recording. Open Voice settings in the Voice panel to try Kokoro, or change the speed.

To use a clone of your own voice, record 10 to 20 seconds of clear speech. Save it as `~/.voz/ref.wav`, and its exact words as `~/.voz/ref.txt`. Run `./setup.sh install` to download the clone model (about 3 GB). Then choose "Your voice" in Voice settings. The recording stays on your Mac.

## Make it yours

Your profile is plain text. Edit it at any time. The session reads it at the start of each new session.

- `CLAUDE.md`: what the voice session does and how it speaks. Your own rules go in the list at the end of this file.
- `context/speaking.md`: examples of how it speaks. It has no rules of its own.
- `context/about-me.md`: optional. Your projects, the people you mention, and words the speech recognizer gets wrong.

Your name, orchestrator sessions and other settings for the task ledger go in `~/.claude/voice-orchestrator/config.json`. Your voice tuning, for example a longer pause before a message is sent, goes in `~/.voz/config.toml`:

```toml
[vad]
turn_gap_ms = 3000
```

## Update

1. Turn the voice off.
2. In this repository folder, run `git pull`, then `./setup.sh install`.
3. Run `./setup.sh profile`. It adds new template files that you do not have yet. It never changes your files. The newest template is in `~/voice-orchestrator/.voz/template/` if you want to compare.

## When something goes wrong

Run `./setup.sh check` first. It names the problem and the fix.

| Problem | What to do |
|---|---|
| Setup says the Mac is not Apple Silicon | The speech models need an M1 or later. There is no workaround. |
| Setup says uv or Swift is missing | Do step 1 again, then open a new Terminal window. |
| No sound after you change headphones or Bluetooth | Press Off, then On, in the Voice panel. |
| It hears its own voice | Use headphones, or lower the speaker volume. |
| `/voice-assistant` is "not found" | The plugin did not load. Check that the environment variable `CLAUDE_CODE_PLUGIN_DIRS` is set in the saved environment, that it holds the full path of this repository folder, and that you started a new session in that environment. If all of that is correct, your account may block local plugin folders. Ask your administrator to allow them, or use a personal Claude account. |
| Port 8766 is in use | Another app uses it. Change the `port` option ("Local port") in the voice plugin's settings in Claude Desktop. Then turn the voice off and on. |
| A model download stops or fails | Check your network and disk space. Then run `./setup.sh install` again. It resumes and keeps what it already downloaded. It checks every file against a pinned hash. |
| "Nothing waiting" when you know there is | The ledger reads Claude Desktop's session records. Check that Desktop is installed and that you used it at least once. |

## Uninstall or reset

Turn the voice off first. Then delete what you want to remove. Each item is separate:

- Your profile, `~/voice-orchestrator`. This holds your edited instructions.
- Your task ledger, name and orchestrators, `~/.claude/voice-orchestrator`.
- Your voice settings, clone recording and meeting notes, `~/.voz`.
- The downloaded speech models. They are the `models--mlx-community--*` folders in `~/.cache/huggingface/hub`. Other apps may use the same models, so keep any folder you still need.
- The installed Python packages and the audio helper, `daemon/.venv` and `daemon/.native` inside this repository folder.
- The saved "Voice Orchestrator" environment in Claude Desktop, with its `CLAUDE_CODE_PLUGIN_DIRS` variable.

To reset and start again, delete the items you want fresh, then run `./setup.sh`. Setup never overwrites your profile, so delete `~/voice-orchestrator` if you want a new copy of the template.
