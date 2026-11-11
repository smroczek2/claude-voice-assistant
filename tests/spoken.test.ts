import { expect, mock, test } from 'claude-code/testing'
import { SpokenLimit } from '../hooks/spoken'

// Feeds a reply in chunks of `size` characters, as a stream does, and returns what is spoken.
function speakIn(reply: string, limit: number, size: number): string[] {
  const s = new SpokenLimit(limit)
  const out: string[] = []
  for (let i = 0; i < reply.length; i += size) out.push(...s.push(reply.slice(i, i + size)))
  return [...out, ...s.end()]
}

// The result must not depend on how the stream cuts the text: 1 character, 3 characters, or whole.
function speak(reply: string, limit = 2): string[] {
  const results = [1, 3, Math.max(reply.length, 1)].map(size => speakIn(reply, limit, size))
  for (const other of results.slice(1)) expect(other).toEqual(results[0])
  return results[0] ?? []
}

test('a long reply speaks only the first two sentences', () => {
  expect(speak('The build passed. Tests are green. I pushed the branch. The PR is open.'))
    .toEqual(['The build passed.', 'Tests are green.'])
})

test('a final question is spoken even after the cut', () => {
  expect(speak('The build passed. Tests are green. I pushed the branch. Should I merge it?'))
    .toEqual(['The build passed.', 'Tests are green.', 'Should I merge it?'])
})

test('a question in the middle stays silent after the cut', () => {
  expect(speak('One. Two. Is three hidden? Four.')).toEqual(['One.', 'Two.'])
})

test('a final question among the first two sentences is spoken one time only', () => {
  expect(speak('The PR is open. Should I merge it?')).toEqual(['The PR is open.', 'Should I merge it?'])
  expect(speak('Should I merge it?')).toEqual(['Should I merge it?'])
})

test('a final question in a list item, heading, quote or table row is not spoken after the cut', () => {
  expect(speak('Two choices. Pick one.\n- Merge now?\n- Wait for review?')).toEqual(['Two choices.', 'Pick one.'])
  expect(speak('One. Two. Three.\n1. Should I merge?')).toEqual(['One.', 'Two.'])
  expect(speak('One. Two. Three.\n## Next steps?')).toEqual(['One.', 'Two.'])
  expect(speak('One. Two. Three.\n> Is this right?')).toEqual(['One.', 'Two.'])
  expect(speak('One. Two. Three.\n| Ready? |')).toEqual(['One.', 'Two.'])
})

test('a final question in a code block is not spoken', () => {
  expect(speak('One. Two. Three.\n```\nwhy is this here?\n```')).toEqual(['One.', 'Two.'])
})

test('a prose question before a final code block is still the final question', () => {
  expect(speak('One. Two. Should I run this?\n```bash\nuv run pytest\n```')).toEqual(['One.', 'Two.', 'Should I run this?'])
})

test('a prose question after a list is the final question', () => {
  expect(speak('One. Two.\n- Three?\nAnd now?')).toEqual(['One.', 'Two.', 'And now?'])
})

test('the final-question rule can be turned off', () => {
  const s = new SpokenLimit(2, false)
  expect([...s.push('One. Two. Three. Should I merge it?'), ...s.end()]).toEqual(['One.', 'Two.'])
})

test('a reply that is only a question is spoken whole', () => {
  expect(speak('Do you want the short version or the full report?')).toEqual(['Do you want the short version or the full report?'])
})

test('a reply of one sentence is spoken whole, with or without a final period', () => {
  expect(speak('Done.')).toEqual(['Done.'])
  expect(speak('Done')).toEqual(['Done'])
})

test('an empty reply speaks nothing', () => {
  expect(speak('')).toEqual([])
  expect(speak('   \n\n ')).toEqual([])
  expect(new SpokenLimit(2).end()).toEqual([])
})

test('abbreviations, initials and version numbers do not end a sentence', () => {
  expect(speak('Use a short name, e.g. voz, for v1.2 of the app. Dr. Lee and J. Smith agree, i.e. ship it. Third one.'))
    .toEqual(['Use a short name, e.g. voz, for v1.2 of the app.', 'Dr. Lee and J. Smith agree, i.e. ship it.'])
})

test('emphasis and code markers after the end mark still end a sentence', () => {
  expect(speak('**Bold start.** Then text. Third. Fourth?'))
    .toEqual(['**Bold start.**', 'Then text.', 'Fourth?'])
  expect(speak('_ok._ Next one. Hidden.')).toEqual(['_ok._', 'Next one.'])
  expect(speak('Use `v1`. Then stop. Hidden.')).toEqual(['Use `v1`.', 'Then stop.'])
  expect(speak('Is it **done?** Yes. Hidden.')).toEqual(['Is it **done?**', 'Yes.'])
  expect(speak('One. Two. Three. **Should I merge?**')).toEqual(['One.', 'Two.', '**Should I merge?**'])
})

test('emphasis does not make v1.2 or e.g. end a sentence', () => {
  expect(speak('Ship **v1.2** today, **e.g.** after lunch. Hidden one. Hidden two.'))
    .toEqual(['Ship **v1.2** today, **e.g.** after lunch.', 'Hidden one.'])
  expect(speak('Pick one, _e.g._ voz. Then `v1.2` ships. Hidden.')).toEqual(['Pick one, _e.g._ voz.', 'Then `v1.2` ships.'])
})

test('etc. ends a sentence only before a capital letter', () => {
  expect(speak('Fix the tests, docs, etc. and push. Then stop. Hidden.')).toEqual(['Fix the tests, docs, etc. and push.', 'Then stop.'])
  expect(speak('Fix the tests, docs, etc. Then push. Hidden.')).toEqual(['Fix the tests, docs, etc.', 'Then push.'])
})

test('code blocks are not spoken and do not count', () => {
  expect(speak('Run this:\n```bash\ncd daemon. Then this.\nuv run pytest\n```\nIt prints the count. Hidden.'))
    .toEqual(['Run this:', 'It prints the count.'])
  expect(speak('```\nonly code. Here.\n```')).toEqual([])
})

test('an unclosed code block is not spoken', () => {
  expect(speak('First.\n```\nprint("never spoken.")\nmore code. ', 5)).toEqual(['First.'])
})

test('each list item counts as one sentence', () => {
  expect(speak('Three things:\n- the daemon\n- the plugin\n- the ledger\n'))
    .toEqual(['Three things:', '- the daemon'])
  expect(speak('1. First step. Still the first item.\n2. Second step.', 3))
    .toEqual(['1. First step.', 'Still the first item.', '2. Second step.'])
})

test('lines with no letters or digits are skipped', () => {
  expect(speak('Results:\n| a | b |\n|---|---|\n---\nAll good.', 3)).toEqual(['Results:', '| a | b |', 'All good.'])
})

test('limit 0 speaks every sentence', () => {
  expect(speak('One. Two. Three. Four.', 0)).toEqual(['One.', 'Two.', 'Three.', 'Four.'])
})

test('the count starts again after end()', () => {
  const s = new SpokenLimit(1)
  expect([...s.push('Checking now. Wait.'), ...s.end()]).toEqual(['Checking now.'])
  expect([...s.push('Found it. More.'), ...s.end()]).toEqual(['Found it.'])
})

test('the first sentence is spoken while the reply still streams', () => {
  const s = new SpokenLimit(2)
  expect(s.push('First sentence. Sec')).toEqual(['First sentence.'])
  expect(s.push('ond one')).toEqual([])
  expect(s.end()).toEqual(['Second one'])
})

// The plugin path: a streamed turn with a tool call between two text blocks.
for (const option of [undefined, 0, -1, 2.5, 2] as const) {
  const options = option === undefined ? {} : { spokenSentences: option }
  test(`the voice speaks the limited reply (spokenSentences=${String(option)})`, { options }, async ($, on) => {
    let started!: () => void, finish!: () => void
    const launched = new Promise<void>(r => { started = r })
    const exited = new Promise<void>(r => { finish = r })
    const speech: string[] = []
    mock.store(on, { 'voz.assistant': 'voice-session' })
    on('session.id', () => ({ value: 'voice-session' }))
    on('command.register', () => ({ value: { command: 'voice-assistant' } }))
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.scroll', () => ({ value: undefined }))
    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('fs.exists', () => ({ value: true }))
    on('clock.sleep', () => ({ value: undefined }))
    on('turn.complete', () => ({ text: '' }))
    on('http.fetch', (_, e) => {
      const body = JSON.parse(e.init?.body ?? '{}')
      if (body.type === 'say') speech.push(body.text)
      if (body.type === 'quit') finish()
      return { value: { ok: true, status: 200, headers: {}, text: '{"ok":true}' } }
    })
    on('process.spawn', async function* () { started(); await exited; return { value: { code: 0, signal: null } } })
    const answer = 'All four suites pass. The release check is clean. Nothing is merged. Should I open the PR?'
    on('turn.step', async function* (_, e) {
      yield { kind: 'text', index: 0, text: 'Let me check. ' }
      yield { kind: 'tool', index: 1, name: 'Bash', id: 't1' }
      yield { kind: 'text', index: 2, text: answer }
      yield { kind: 'stop', stopReason: 'end_turn', usage: null }
      return { turnId: e.turnId, index: e.index, answer, toolUses: [], stopReason: 'end_turn', usage: null }
    })
    await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'voice-assistant', args: '' })
    await launched
    for await (const _ of $.turn.step({ turnId: 'voice-test', index: 0, model: 'test', messageCount: 1 } as never)) {}
    await $.turn.complete({ turnId: 'voice-test', answer, durationMs: 1, isAborted: false, reason: 'completed' } as never)
    await $.command.run({ command: 'voice-assistant', args: 'off' })
    const said = speech.join(' ')
    // The default, 0 and a bad value speak the whole reply; 2 speaks two sentences per block.
    if (option !== 2) expect(said).toBe(`Let me check. ${answer}`)
    else expect(said).toBe('Let me check. All four suites pass. The release check is clean. Should I open the PR?')
  })
}
