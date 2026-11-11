import { expect, mock, test } from 'claude-code/testing'

const PANE = {
  plugin: 'voz',
  component: 'Pane',
  requestId: 'voz',
  props: {
    title: 'Voz',
    isFocused: false,
    bodyColumns: 60,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

const BAND = {
  plugin: 'voz',
  component: 'AbovePrompt',
  props: { hasSurvey: false },
} as const

const LISTENING = {
  state: 'listening', mic: 'AirPods', tts: 'mlx/joana-pt', level: 0.3, ptt: false, muted: false, note: 'listening: hello', saying: 'Hello.',
  showSettings: true,
  settings: { voice: 'mlx:ref:x.wav', sensitivity: 'media', turn_gap_ms: 1200, mode: 'auto', barge_in: true, speed: 1, language: 'pt_PT', cleanup: 'regras' },
  voices: [{ id: 'mlx:ref:x.wav', label: 'joana-pt (clone)' }],
  languages: [{ code: 'pt_PT', label: 'Português (Portugal)' }],
  search: '',
  results: [{ id: 'piper:pt_PT-tugão-medium', label: 'tugão (Piper)', lang: 'pt_PT', installed: false, size_mb: 63 }],
  meeting: { on: true, title: 'Test', path: '/tmp/acta.md', speakers: ['Orador 1'], entries: [{ t: '10:00:00', who: 'Orador 1', text: 'Hello everyone.' }] },
}

test('with no designated session the voice stays passive: the band is not drawn', async ($, on) => {
  mock.store(on, {})
  on('session.id', () => ({ value: 'sessao-a' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  // nothing draws beneath the plugin: this hook acts as the engine
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const band = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await band.find({ type: 'Button', key: 'gear' })).toBeUndefined()
  expect(await band.find({ type: 'Text', text: 'engine' })).toBeDefined()
  await band.unmount()
})

test('explicit autoStart renders the designated assistant', { options: { autoStart: true } }, async ($, on) => {
  mock.store(on, { 'voz.assistant': 'sessao-voz' })
  on('session.id', () => ({ value: 'sessao-voz' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  // startup tries to launch the daemon; without fs/process in the kit it falls into the "error" state, which the band draws
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })

  for (const surface of ['desktop', 'terminal'] as const) {
    const pane = await $.ui.mount({ ...PANE, surface })
    expect(await pane.find({ type: 'Text', text: /Fala|Voice|voz/ })).toBeDefined()
    await pane.unmount()
    const band = await $.ui.mount({ ...BAND, surface })
    expect(await band.find({ type: 'Button', key: 'gear' })).toBeDefined()
    await band.press({ key: 'gear' })
    await band.unmount()
  }
})


for (const runtimeState of ['listening', 'starting'] as const) {
test(`stored duplicate voice options preserve controls while ${runtimeState}`, { options: { autoStart: true } }, async ($, on) => {
  let finish!: () => void, started!: () => void
  const exited = new Promise<void>(resolve => { finish = resolve })
  const launched = new Promise<void>(resolve => { started = resolve })
  mock.store(on, { 'voz.assistant': 'voice-session' })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('fs.exists', () => ({ value: true }))
  on('clock.sleep', () => ({ value: undefined }))
  on('http.fetch', (_, e) => { if (JSON.parse(e.init?.body ?? '{}').type === 'quit') finish(); return { value: { ok: true, status: 200, headers: {}, text: '{"ok":true}' } } })
  on('process.spawn', async function* () { started(); await exited; return { value: { code: 0, signal: null } } })
  on('session.id', () => ({ value: 'voice-session' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('state.set', () => ({ value: { isSet: true, version: 2 } }))
  on('state.get', () => ({ value: { value: { ...LISTENING, state: runtimeState, meeting: { ...LISTENING.meeting, on: false }, voices: [
    { id: 'mlx:voice:Ryan', label: 'Ryan' }, { id: 'mlx:voice:Ryan', label: 'Ryan duplicate' },
    { id: 'say:Aman (English (India))', label: 'Aman' }, { id: 'say:Aman (English (India))', label: 'Aman duplicate' },
  ] }, version: 1 } }))
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await launched
  const band = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await band.find({ type: 'Text', text: runtimeState === 'listening' ? /Listening/ : /Starting voice/ })).toBeDefined()
  expect(await band.find({ type: 'Select', key: 'voice-band' })).toBeDefined()
  expect(await band.find({ type: 'Button', key: 'power' })).toBeDefined()
  await band.unmount()
  await $.command.run({ command: 'voice-assistant', args: 'off' })
})
}
