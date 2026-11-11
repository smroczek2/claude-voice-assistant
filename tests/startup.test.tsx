import { expect, mock, test } from 'claude-code/testing'

test('remembered assistant stays off until explicitly started', async ($, on) => {
  let spawned = false
  mock.store(on, { 'voz.assistant': 'voice-session' })
  on('session.id', () => ({ value: 'voice-session' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('prompt.submit', (_, e) => ({ text: e.text, context: e.context }))
  on('process.spawn', async function* () { spawned = true; throw new Error('Unexpected daemon start') })
  on('ui.render', ($, e) => { const { Text } = $.ui.resolve(e); return <Text>Voice is off</Text> })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const band = await $.ui.mount({ plugin: 'voz', component: 'AbovePrompt', surface: 'desktop', props: { hasSurvey: false } })
  expect(spawned).toBe(false)
  expect(await band.find({ type: 'Text', text: 'Voice off — microphone closed' })).toBeDefined()
  expect(await band.find({ type: 'Button', key: 'power' })).toBeDefined()
  expect(await band.find({ type: 'Button', key: 'pause' })).toBeUndefined()
  const prompt = await $.prompt.submit({ text: 'Is the microphone on?' })
  expect(prompt.context?.join(' ')).toContain('"microphoneCapture":"closed"')
  await band.unmount()
})

test('reload clears stale listening status without reopening microphone', async ($, on) => {
  mock.store(on, { 'voz.assistant': 'voice-session' })
  on('session.id', () => ({ value: 'voice-session' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('process.spawn', async function* () { throw new Error('Microphone must remain closed') })
  let held: unknown = { state: 'muted', muted: true, note: 'stale capture' }
  on('state.get', () => ({ value: { value: held, version: 1 } }))
  on('state.set', (_, e) => { held = e.value; return { value: { isSet: true, version: 2 } } })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const band = await $.ui.mount({ plugin: 'voz', component: 'AbovePrompt', surface: 'desktop', props: { hasSurvey: false } })
  expect(await band.find({ type: 'Text', text: 'Voice off — microphone closed' })).toBeDefined()
  expect(await band.find({ type: 'Button', key: 'power' })).toBeDefined()
  expect(await band.find({ type: 'Button', key: 'pause' })).toBeUndefined()
  await band.unmount()
})

for (const state of ['listening', 'muted', 'speaking', 'recording', 'transcribing', 'starting']) {
  test(`hot reload reconciles persisted ${state} without a session.start event`, async ($, on) => {
    let spawned = false
    mock.store(on, { 'voz.assistant': 'voice-session' })
    on('session.id', () => ({ value: 'voice-session' }))
    on('state.get', () => ({ value: { value: { state, muted: true, level: .8, ptt: true, note: 'Hearing: stale words', saying: 'Old speech' }, version: 1 } }))
    on('process.spawn', async function* () { spawned = true; throw new Error('Microphone must remain closed') })
    on('prompt.submit', (_, e) => ({ text: e.text, context: e.context }))
    const band = await $.ui.mount({ plugin: 'voz', component: 'AbovePrompt', surface: 'desktop', props: { hasSurvey: false } })
    expect(await band.find({ type: 'Text', text: 'Voice off — microphone closed' })).toBeDefined()
    expect(await band.find({ type: 'Button', key: 'power' })).toBeDefined()
    expect(await band.find({ type: 'Button', key: 'pause' })).toBeUndefined()
    const prompt = await $.prompt.submit({ text: 'Local test sink only' })
    expect(prompt.context?.join(' ')).toContain('"microphoneCapture":"closed"')
    expect(prompt.context?.join(' ')).toContain('"voiceState":"off"')
    expect(spawned).toBe(false)
    await band.unmount()
  })
}

test('hot reload preserves a real failure explanation', async ($, on) => {
  mock.store(on, { 'voz.assistant': 'voice-session' })
  on('session.id', () => ({ value: 'voice-session' }))
  on('state.get', () => ({ value: { value: { state: 'error', note: 'Audio helper failed to start' }, version: 1 } }))
  const band = await $.ui.mount({ plugin: 'voz', component: 'AbovePrompt', surface: 'desktop', props: { hasSurvey: false } })
  expect(await band.find({ type: 'Text', text: 'Audio helper failed to start' })).toBeDefined()
  expect(await band.find({ type: 'Text', text: 'Voice off — microphone closed' })).toBeUndefined()
  await band.unmount()
})
