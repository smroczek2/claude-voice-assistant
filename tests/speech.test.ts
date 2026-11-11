import { expect, mock, test } from 'claude-code/testing'

test('speech receives visible answer text and excludes thinking and tool arguments', async ($, on) => {
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
  on('turn.step', async function* (_, e) {
    yield { kind: 'thinking', index: 0, text: 'I should not say these private instructions.' }
    yield { kind: 'text', index: 1, text: 'The weather is clear. ' }
    yield { kind: 'input', index: 2, json: '{"instructions":"Never speak these tool arguments"}' }
    yield { kind: 'text', index: 3, text: 'The test is ready.' }
    yield { kind: 'stop', stopReason: 'end_turn', usage: null }
    return { turnId: e.turnId, index: e.index, answer: 'The weather is clear. The test is ready.', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'voice-assistant', args: '' })
  await launched
  for await (const _ of $.turn.step({ turnId: 'voice-test', index: 0, model: 'test', messageCount: 1 })) {}
  await $.turn.complete({ turnId: 'voice-test', answer: 'The weather is clear. The test is ready.', durationMs: 1, isAborted: false, reason: 'completed' } as never)
  await $.command.run({ command: 'voice-assistant', args: 'off' })
  expect(speech.join(' ')).toBe('The weather is clear. The test is ready.')
})

test('Interrupt speech does not stop the Claude session from receiving the next voice message', async ($, on) => {
  let started!: () => void, finish!: () => void
  const launched = new Promise<void>(r => { started = r })
  const exited = new Promise<void>(r => { finish = r })
  let aborted = 0
  const commands: string[] = []
  mock.store(on, { 'voz.assistant': 'voice-session' })
  on('session.id', () => ({ value: 'voice-session' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.scroll', () => ({ value: undefined }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('fs.exists', () => ({ value: true }))
  on('clock.sleep', () => ({ value: undefined }))
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.abort', () => { aborted++; return { value: undefined } })
  on('http.fetch', (_, e) => {
    const body = JSON.parse(e.init?.body ?? '{}'); commands.push(body.type)
    if (body.type === 'quit') finish()
    return { value: { ok: true, status: 200, headers: {}, text: '{"ok":true}' } }
  })
  on('process.spawn', async function* () { started(); await exited; return { value: { code: 0, signal: null } } })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'voice-assistant', args: '' })
  await launched
  await $.turn.start({ turnId: 'ongoing-voice-reply', text: 'A spoken question.' })
  const band = await $.ui.mount({ plugin: 'voz', component: 'AbovePrompt', surface: 'desktop', props: { hasSurvey: false } })
  await band.press({ key: 'stop' })
  expect(commands.includes('stop')).toBe(true)
  expect(aborted).toBe(0)
  await band.unmount()
  await $.command.run({ command: 'voice-assistant', args: 'off' })
})

for (const [kind, wait, stops] of [
  ['task-notification', false, false], ['peer', false, false],
  ['scheduled-trigger', false, false], ['observer', false, false],
  ['unclassified', false, false], ['composer', true, false],
  ['composer', false, true], ['bridge', false, true],
] as const) {
  test(`${kind} input (wait=${wait}) ${stops ? 'interrupts' : 'preserves'} current speech`, async ($, on) => {
    let finish!: () => void, started!: () => void
    const exited = new Promise<void>(r => { finish = r })
    const launched = new Promise<void>(r => { started = r })
    const commands: string[] = []
    let submitted = ''
    mock.store(on, { 'voz.assistant': 'voice-session' })
    on('session.id', () => ({ value: 'voice-session' }))
    on('command.register', () => ({ value: { command: 'voice-assistant' } }))
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('fs.exists', () => ({ value: true }))
    on('clock.sleep', () => ({ value: undefined }))
    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('prompt.submit', (_, e) => { submitted = e.text; return { text: e.text, context: e.context } })
    on('http.fetch', (_, e) => {
      const cmd = JSON.parse(e.init?.body ?? '{}'); commands.push(cmd.type)
      if (cmd.type === 'quit') finish()
      return { value: { ok: true, status: 200, headers: {}, text: '{"ok":true}' } }
    })
    on('process.spawn', async function* () { started(); await exited; return { value: { code: 0, signal: null } } })
    await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'voice-assistant', args: '' })
    await launched
    const before = commands.filter(c => c === 'stop').length
    await $.prompt.submit({ text: 'An incoming update.', origin: { kind }, wait })
    expect(commands.filter(c => c === 'stop').length - before).toBe(stops ? 1 : 0)
    expect(submitted).toBe('An incoming update.')
    await $.command.run({ command: 'voice-assistant', args: 'off' })
  })
}
