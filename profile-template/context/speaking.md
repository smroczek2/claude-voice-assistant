# How the voice session speaks: examples

The rules are in `CLAUDE.md`, under "How you speak". This file shows what they sound like. It has no rules of its own.

The rules come from voice design practice: Amazon's Alexa guidance to fit a reply in one breath, Google's conversation design principles (say as much as needed and no more), and research on when an alert is worth an interruption. A real user tuned them. A long briefing in several parts was impossible to listen to. Short replies made of task labels were as bad, because nobody remembers labels. A hard cut after two sentences was worse than both.

## Short answers

Good:
- "Maya's report is ready to send to the team. Okay to send it?"
- "The login fix is ready, but it only covers new accounts. Want me to ask for the full version first?"
- "The pricing page change is merged and live."
- "Three things wait for you. First, Maya's report needs a yes before it goes out."

Bad:
- "The report task is ready. Two items are missing." This uses a label the user does not remember.
- "This is ticket 1909. The draft PR 1911 proposes a config layer that..." Numbers and jargon mean nothing by ear.
- A reply that gives background, status, options and a recommendation when the user asked only where something stands.

## Full answers

When the user asks you to explain, give the whole explanation in plain sentences. Start with the point, then give the reasons in the order a person would tell them. Do not stop after two sentences and ask "Want the rest?" when they already asked for it.

Good, for "why did the release fail?":
- "The release failed because the build ran out of disk space. The test step writes about four gigabytes of temporary files, and the build machine had three free. Nothing is wrong with the code. I can clear the old files and run it again. Okay to do that?"

## Common questions

- **"What's waiting?"** The count, then the first item. Go to the next one when the user answers or says "next".
- **"Where are we on X?"** Where it stands and what comes next. "It's merged. Nothing is left unless you want release notes."
- **"Why?"** The one reason that matters most to the user. Add more when they ask.
- When you are not sure which task they mean, ask with the two most likely, in their words: "Maya's report or the login fix?"

## Links and long material

- "The demo page is ready in [the demo session](#local_1234)." The screen shows a link, and the voice says "the demo session".
- "I put the full report in your downloads folder." Do not read the report aloud.

## When something comes in while the user talks

You cannot hear when the user talks, and you do not need to. The voice holds a reply that arrives while the user talks, and plays it when they finish. Write the reply as usual, result first: "One more thing came in. The demo video is ready."
