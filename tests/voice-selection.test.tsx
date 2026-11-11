import { expect, mock, test } from 'claude-code/testing'

for (const migrated of [false, true]) {
  test(migrated ? 'restart preserves a later neural voice choice and user speed' : 'approved Turbo migration preserves user speed and warms Turbo first', async ($, on) => {
    let finish!: () => void, restored!: () => void
    const ready = new Promise<void>(r => { restored = r })
    const exited = new Promise<void>(r => { finish = r })
    let argv: readonly string[] = []
    let input = ''
    const tokens = new Set<string>()
    let selected: Record<string, unknown> = {}
    mock.store(on, {
      'voz.assistant': 'voice-session',
      'voz.settings': { voice: 'kokoro:af_heart', speed: 1.3, language: 'en' },
      'voz.turboChoiceApplied': migrated,
    })
    on('session.id', () => ({ value: 'voice-session' }))
    on('command.register', () => ({ value: { command: 'voice-assistant' } }))
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.log', () => ({}))
    on('ui.toast', () => ({ value: undefined }))
    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('fs.exists', () => ({ value: true }))
    on('clock.sleep', () => ({ value: undefined }))
    on('http.fetch', (_, e) => {
      const cmd = JSON.parse(e.init?.body ?? '{}')
      tokens.add(String((e.init?.headers as Record<string, string> | undefined)?.['X-Voz-Token'] ?? ''))
      if (cmd.type === 'set') { selected = cmd; restored() }
      if (cmd.type === 'quit') finish()
      return { value: { ok: true, status: 200, headers: {}, text: JSON.stringify({ ok: true, voice: cmd.voice }) } }
    })
    on('process.spawn', async function* (_, e) {
      argv = e.argv
      input = e.input ?? ''
      yield { stream: 'stdout', text: JSON.stringify({ type: 'ready', mic: 'Test mic', tts: 'turbo/Chatterbox Turbo' }) + '\n' }
      await exited
      return { value: { code: 0, signal: null } }
    })
    await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'voice-assistant', args: '' })
    await ready
    expect(argv.slice(-3)).toEqual(['--tts', migrated ? 'kokoro' : 'turbo', '--token-stdin'])
    // the per-start secret goes to the daemon on stdin and on every HTTP request
    expect(input).toMatch(/^[0-9a-f]{64}\n$/)
    expect([...tokens]).toEqual([input.trim()])
    expect(selected.voice).toBe(migrated ? 'kokoro:af_heart' : 'turbo:default')
    expect(selected.speed).toBe(1.3)
    await $.command.run({ command: 'voice-assistant', args: 'off' })
  })
}

for (const failedResponse of [false, true]) {
  test(failedResponse ? 'rejected restore keeps the actual running voice visible' : 'unacknowledged restore keeps the actual running voice visible', async ($, on) => {
    let finish!: () => void, drained!: () => void
    const ready = new Promise<void>(r => { drained = r })
    const exited = new Promise<void>(r => { finish = r })
    mock.store(on, {
      'voz.assistant': 'voice-session', 'voz.turboChoiceApplied': true,
      'voz.settings': { voice: 'kokoro:af_heart', speed: 1.3, language: 'en' },
    })
    on('session.id', () => ({ value: 'voice-session' }))
    on('command.register', () => ({ value: { command: 'voice-assistant' } }))
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.log', () => ({}))
    on('ui.toast', () => ({ value: undefined }))
    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('fs.exists', () => ({ value: true }))
    on('clock.sleep', () => ({ value: undefined }))
    on('http.fetch', (_, e) => {
      const cmd = JSON.parse(e.init?.body ?? '{}')
      if (cmd.type === 'quit') finish()
      if (cmd.type === 'set') return { value: { ok: failedResponse, status: failedResponse ? 200 : 503, headers: {}, text: '{"ok":false,"error":"Saved voice could not be restored."}' } }
      return { value: { ok: true, status: 200, headers: {}, text: '{}' } }
    })
    on('process.spawn', async function* () {
      yield { stream: 'stdout', text: JSON.stringify({ type: 'ready', mic: 'Test mic', tts: 'turbo/Chatterbox Turbo', settings: { voice: 'turbo:default', speed: 1 } }) + '\n' }
      drained(); await exited
      return { value: { code: 0, signal: null } }
    })
    await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'voice-assistant', args: '' })
    await ready
    const pane = await $.ui.mount({ plugin: 'voz', component: 'Pane', requestId: 'voz', surface: 'desktop', props: { title: 'Voice', isFocused: true, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} } })
    expect(await pane.find({ type: 'Text', text: 'Voice: turbo/Chatterbox Turbo' })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: /Saved voice could not be restored/ })).toBeDefined()
    await pane.unmount()
    await $.command.run({ command: 'voice-assistant', args: 'off' })
  })
}
