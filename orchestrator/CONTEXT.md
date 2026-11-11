# The orchestrator folder

The state folder is `~/.claude/orchestrator/` (or `VOZ_ORCH_DIR`). It is outside every
repository and outside the vault sync, because it holds the user's own words.
This file says which file answers which question. Read only the file you need.

## Files

| File | Answers | Written by | Edit by hand? |
|---|---|---|---|
| `orchestrators.json` | Which sessions are orchestrators? Where is the vault? | the user, once | Yes |
| `rules.md` | How must the orchestrator behave? | `orch_state.py install-rules` from `orchestrator/rules.md` in the engine | No: edit the source, then install |
| `state.json` | What is open now: alerts, questions, cards, idle PRs, vault date, newest decisions | `orch_state.py build` | No |
| `digest.md` | The same state as text, cut to the start budget | `orch_state.py build` | No |
| `decisions.jsonl` | What did the user approve, reject or choose, and what was the question? | `orch_mine.py`, run by each build | No |
| `rule-candidates.jsonl` | Where did the user correct the orchestrator ("you forgot", "I told you", "stop")? | `orch_mine.py`, run by each build | No |
| `cache-v<N>.json`, `mine-cache-v<N>.json` | Nothing for a reader. Speed-ups: read offsets and slim session records | the builder and the miner | No; delete to rebuild |
| `printed.json` | Which digest each session saw last | `orch_hook.py` | No |
| `nagged.json` | When each session last named each waiting card (the Stop hook names a card again after 20 minutes) | `orch_hook.py` | No; delete to remind at once |
| `.lock`, `.printed.lock` | Nothing. One writer at a time | the builder, the miner, the hook | No |

## Rules for the files

- One writer: the builder holds `.lock` for the whole build, the miners run inside it,
  and each file is replaced with an atomic rename.
- `state.json` stays version 1. A new key is fine; older code ignores it. A new fact kind
  is not: older code fails on a kind it does not know.
- The two `.jsonl` logs only grow. Each line has an `id`, a time (`at`), the session, and
  the transcript it came from. A broken line stops the miners with an alert; fix the line.
- A decision is the user's own typed or spoken words, or an answer in a question tool.
  A relay from another session and a murmur ("Mm-hmm") are not decisions. A bare "yes"
  or "no" counts only when a yes-or-no question came just before it.

## Commands

```bash
python3 scripts/orch_state.py build
```

```bash
python3 scripts/orch_mine.py decisions --last 20
```

```bash
python3 scripts/orch_mine.py import-rules --dry-run
```

The import prints the dated lines that it would add to `rules.md` from the rule candidates,
and writes nothing. A person turns a candidate into a rule. Note: `install-rules --force`
replaces the folder copy of `rules.md` (the old copy goes to `rules.md.bak`).

## Not here yet

`references/` (the routing and people tables) and the merge of the user's memory files
into `rules.md` wait for the user's decision.
