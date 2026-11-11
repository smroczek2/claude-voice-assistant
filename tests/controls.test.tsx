import { expect, mock, test } from 'claude-code/testing'

test('off reports failure when a running daemon cannot stop', async ($, on) => {
  let finish!: () => void
  let started!: () => void
  const launched = new Promise<void>(resolve => { started = resolve })
  const exited = new Promise<void>(resolve => { finish = resolve })
  mock.store(on, { 'voz.assistant': 'voice-session' })
  on('session.id', () => ({ value: 'voice-session' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.log', () => ({}))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('fs.exists', () => ({ value: true }))
  on('http.fetch', () => ({ value: { ok: false, status: 503, headers: {}, text: '{}' } }))
  on('clock.sleep', () => ({ value: undefined }))
  on('process.spawn', async function* () { started(); await exited; return { value: { code: 0, signal: null } } })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'voice-assistant', args: '' })
  await launched
  const result = await $.command.run({ command: 'voice-assistant', args: 'off' })
  finish()
  const stopped = await $.command.run({ command: 'voice-assistant', args: 'off' })
  expect(stopped.text).toBe('Voice assistant off.')
  expect(result.text).toContain('could not confirm')
})

test('buffered pre-pause speech is discarded even after resume', async ($, on) => {
  let started!: () => void, emit!: () => void, drained!: () => void, finish!: () => void
  const launched = new Promise<void>(r => { started = r })
  const deliver = new Promise<void>(r => { emit = r })
  const consumed = new Promise<void>(r => { drained = r })
  const exited = new Promise<void>(r => { finish = r })
  const prompts: string[] = []
  const contexts: string[] = []
  let generation = 0
  mock.store(on, { 'voz.assistant': 'voice-session' })
  on('session.id', () => ({ value: 'voice-session' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.scroll', () => ({ value: undefined }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('fs.exists', () => ({ value: true }))
  on('clock.sleep', () => ({ value: undefined }))
  on('prompt.submit', (_, e) => { prompts.push(e.text); contexts.push((e.context ?? []).join(' ')); return { drop: 'Test sink' } })
  on('http.fetch', (_, e) => {
    const body = JSON.parse(e.init?.body ?? '{}')
    if (body.type === 'mute') generation++
    if (body.type === 'quit') finish()
    return { value: { ok: true, status: 200, headers: {}, text: JSON.stringify({ ok: true, generation }) } }
  })
  on('process.spawn', async function* () {
    started(); await deliver
    yield { stream: 'stdout', text: JSON.stringify({ type: 'utterance', text: 'Stale speech from before pause.', generation: 0 }) + '\n' }
    drained(); await exited
    return { value: { code: 0, signal: null } }
  })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'voice-assistant', args: '' })
  await launched
  await $.command.run({ command: 'voice-assistant', args: 'pause' })
  const band = await $.ui.mount({ plugin: 'voz', component: 'AbovePrompt', surface: 'desktop', props: { hasSurvey: false } })
  expect(await band.find({ type: 'Text', text: /Paused/ })).toBeDefined()
  await band.unmount()
  await $.prompt.submit({ text: 'Pause status check' })
  expect(contexts.at(-1)).toContain('"microphoneCapture":"paused"')
  await $.command.run({ command: 'voice-assistant', args: 'resume' })
  await $.prompt.submit({ text: 'Resume status check' })
  expect(contexts.at(-1)).toContain('"microphoneCapture":"active"')
  emit(); await consumed
  await $.command.run({ command: 'voice-assistant', args: 'off' })
  expect(prompts.join('|')).toBe('Pause status check|Resume status check')
})
