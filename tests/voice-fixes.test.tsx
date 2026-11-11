import { expect, mock, test } from 'claude-code/testing'

// Fixes ported from the voice-orchestrator branch: the stop fallback to the chat box, plain
// "ignored" notes that clear themselves, "Send now" for a queued message, and Skip only while
// speech is possible.

type On = Parameters<Parameters<typeof test>[1]>[1]

/** Starts the voice with a daemon that emits `events`, then waits until they are read. */
function daemon(on: On, events: object[], extra: { drop?: string; gate?: Promise<void> } = {}) {
  let finish!: () => void, drained!: () => void
  const exited = new Promise<void>(r => { finish = r })
  const consumed = new Promise<void>(r => { drained = r })
  const commands: string[] = []
  const filled: string[] = []
  let aborted = 0
  mock.store(on, { 'voz.assistant': 'voice-session' })
  on('session.id', () => ({ value: 'voice-session' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.scroll', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('fs.exists', () => ({ value: true }))
  on('clock.sleep', () => ({ value: undefined }))
  on('turn.abort', () => { aborted++; return { value: undefined } })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('prompt.submit', (_, e) => (extra.drop !== undefined ? { drop: extra.drop } : { text: e.text }))
  on('prompt.read', () => ({ value: { text: '', cursor: 0 } }))
  on('prompt.fill', (_, e) => { filled.push(e.text); return { isFilled: true, text: e.text, cursor: e.text.length } })
  on('http.fetch', (_, e) => {
    const body = JSON.parse(e.init?.body ?? '{}'); commands.push(body.type)
    if (body.type === 'quit') finish()
    return { value: { ok: true, status: 200, headers: {}, text: '{"ok":true}' } }
  })
  on('process.spawn', async function* () {
    yield { stream: 'stdout', text: JSON.stringify({ type: 'ready', mic: 'Test mic', tts: 'turbo', mode: 'auto', port: 8766 }) + '\n' }
    await extra.gate
    for (const event of events) yield { stream: 'stdout', text: JSON.stringify(event) + '\n' }
    drained(); await exited
    return { value: { code: 0, signal: null } }
  })
  return { consumed, commands, filled, aborted: () => aborted }
}

const BAND = { plugin: 'voz', component: 'AbovePrompt', surface: 'desktop', props: { hasSurvey: false } } as const
const settle = () => new Promise(r => setTimeout(r, 30))

async function start($: Parameters<Parameters<typeof test>[1]>[0]) {
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'voice-assistant', args: '' })
}

for (const [drop, toChat] of [['The session was stopped', true], ['Usage limit reached', false]] as const) {
  test(`a refused voice message ${toChat ? 'goes to the chat box after a stop' : 'stays in the pane'} (${drop})`, async ($, on) => {
    const d = daemon(on, [{ type: 'utterance', text: 'Merge the docs PR.', generation: 0 }], { drop })
    await start($)
    await d.consumed; await settle()
    expect(d.filled).toEqual(toChat ? ['Merge the docs PR.'] : [])
    const band = await $.ui.mount(BAND)
    if (toChat) expect(await band.find({ type: 'Text', text: /Your words are in the chat box/ })).toBeDefined()
    else expect(await band.find({ type: 'Text', text: /Not sent to Claude: Usage limit reached/ })).toBeDefined()
    await band.unmount()
    await $.command.run({ command: 'voice-assistant', args: 'off' })
  })
}

for (const [reason, note] of [
  ['Too short (0.2s)', 'Ignored a very short sound'],
  ['Low confidence for interruption (-1.20): hm what', 'Did not catch that clearly. Please say it again. ("hm what")'],
  ['Transcription failed: timeout', 'Could not turn that into text. Please say it again.'],
  ['Speaker echo: the build passed', 'Ignored background sound ("the build passed")'],
] as const) {
  test(`an ignored sound shows a plain note: ${reason}`, async ($, on) => {
    const d = daemon(on, [{ type: 'discarded', reason }])
    await start($)
    await d.consumed; await settle()
    const band = await $.ui.mount(BAND)
    expect(await band.find({ type: 'Text', text: note })).toBeDefined()
    await band.unmount()
    await $.command.run({ command: 'voice-assistant', args: 'off' })
  })
}

test('an ignored-sound note clears itself after 6 seconds', async ($, on) => {
  const delays: number[] = []
  let fire!: () => void
  const fired = new Promise<void>(r => { fire = r })
  // The engine runs the callback when clock.after answers; answer only when the test says so.
  on('clock.after', async (_, e) => { delays.push(e.ms); await fired; return { value: undefined } })
  const d = daemon(on, [{ type: 'discarded', reason: 'Too short (0.2s)' }])
  await start($)
  await d.consumed; await settle()
  expect(delays).toContain(6000)
  let band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: 'Ignored a very short sound' })).toBeDefined()
  await band.unmount()
  fire(); await settle()
  band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: 'Ignored a very short sound' })).toBeUndefined()
  await band.unmount()
  await $.command.run({ command: 'voice-assistant', args: 'off' })
})

test('a message spoken during a turn can be sent now', async ($, on) => {
  let speak!: () => void
  const gate = new Promise<void>(r => { speak = r })
  const d = daemon(on, [{ type: 'utterance', text: 'Also check the tests.', generation: 0 }], { gate })
  await start($)
  await $.turn.start({ turnId: 'long-task', text: 'Run the build.' })
  speak(); await d.consumed; await settle()
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: 'Your message waits until Claude finishes.' })).toBeDefined()
  await band.press({ key: 'send-now' })
  expect(d.commands).toContain('stop')
  expect(d.aborted()).toBe(1)
  await band.unmount()
  await $.command.run({ command: 'voice-assistant', args: 'off' })
})

test('Skip shows only while Claude answers or a reply plays', async ($, on) => {
  const d = daemon(on, [{ type: 'state', state: 'listening' }])
  await start($)
  await d.consumed; await settle()
  let band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Button', key: 'stop' })).toBeUndefined()
  await band.unmount()
  await $.turn.start({ turnId: 'answer', text: 'A question.' })
  band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Button', key: 'stop' })).toBeDefined()
  await band.unmount()
  await $.command.run({ command: 'voice-assistant', args: 'off' })
})
