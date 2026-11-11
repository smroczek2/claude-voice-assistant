import { expect, mock, test } from 'claude-code/testing'

const PANE = {
  plugin: 'voz', component: 'Pane', requestId: 'voz', surface: 'desktop',
  props: { title: 'Voice', isFocused: true, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
} as const

test('Voice pane recovers and controls listening without the shared prompt band', async ($, on) => {
  let launches = 0
  let finish: (() => void) | undefined
  let started!: () => void
  const launched = new Promise<void>(r => { started = r })
  mock.store(on, { 'voz.assistant': 'voice-session' })
  on('session.id', () => ({ value: 'voice-session' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.log', () => ({}))
  on('ui.toast', () => ({ value: undefined }))
  on('fs.exists', () => ({ value: true }))
  on('clock.sleep', () => ({ value: undefined }))
  on('http.fetch', (_, e) => {
    const command = JSON.parse(e.init?.body ?? '{}')
    if (command.type === 'quit') finish?.()
    return { value: { ok: true, status: 200, headers: {}, text: '{"ok":true,"generation":1}' } }
  })
  on('process.spawn', async function* () {
    launches++
    const exited = new Promise<void>(r => { finish = r })
    started()
    yield { stream: 'stdout', text: JSON.stringify({ type: 'ready', mic: 'Test mic', tts: 'mlx/Ryan', settings: { voice: 'mlx:voice:Ryan', language: 'en', mode: 'auto' } }) + '\n' }
    await exited
    return { value: { code: 0, signal: null } }
  })
  // The host may reserve/collapse AbovePrompt; the pane is the only mounted UI.
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const pane = await $.ui.mount(PANE)
  expect(await pane.find({ type: 'Button', key: 'power' })).toBeDefined()
  await pane.press({ key: 'power' })
  await launched
  expect(await pane.find({ type: 'Button', key: 'pause' })).toBeDefined()
  await pane.press({ key: 'pause' })
  expect(await pane.find({ type: 'Text', text: /Paused/ })).toBeDefined()
  await pane.press({ key: 'pause' })
  expect(await pane.find({ type: 'Text', text: /Listening/ })).toBeDefined()
  await $.command.run({ command: 'voice-assistant', args: '' })
  expect(launches).toBe(1)
  await pane.press({ key: 'gear' })
  expect(await pane.find({ type: 'Select', key: 'voice' })).toBeDefined()
  // The default pause before sending is 2.5 s, and the list offers it.
  const gap = await pane.find({ type: 'Select', key: 'gap' })
  expect(gap?.props.value).toBe('2500')
  expect(gap?.props.options).toContainEqual({ value: '2500', label: 'Longer (2.5 s), default' })
  await pane.press({ key: 'power' })
  expect(await pane.find({ type: 'Text', text: /Voice off/ })).toBeDefined()
  expect(await pane.find({ type: 'Button', key: 'pause' })).toBeUndefined()
  expect(await pane.find({ type: 'Button', key: 'power' })).toBeDefined()
  await pane.unmount()
})
