import { expect, mock, test } from 'claude-code/testing'

// A voice turn does not reach the UserPromptSubmit hook, so the plugin adds the
// orchestrator state to the same note that carries the voice facts.
for (const outcome of ['digest', 'empty', 'error', 'thrown'] as const) {
  test(`voice turn carries the orchestrator state (${outcome})`, async ($, on) => {
    let finish!: () => void, deliver!: () => void, received!: () => void
    const exited = new Promise<void>(r => { finish = r })
    const utterance = new Promise<void>(r => { deliver = r })
    const submitted = new Promise<void>(r => { received = r })
    const notes: string[] = []
    const order: string[] = []
    let argv: string[] = []
    let limit = 0
    mock.store(on, { 'voz.assistant': 'voice-session' })
    on('session.id', () => ({ value: 'voice-session' }))
    on('command.register', () => ({ value: { command: 'voice-assistant' } }))
    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.scroll', () => ({ value: undefined }))
    on('ui.toast', () => ({ value: undefined }))
    on('fs.exists', () => ({ value: true }))
    on('clock.sleep', () => ({ value: undefined }))
    on('http.fetch', (_, e) => { if (JSON.parse(e.init?.body ?? '{}').type === 'quit') finish(); return { value: { ok: true, status: 200, headers: {}, text: '{"ok":true}' } } })
    on('process.run', (_, e) => {
      argv = e.argv; limit = e.init?.timeoutMs ?? 0
      if (outcome === 'thrown') throw new Error('python3 not found')
      if (outcome === 'error') return { value: { exitCode: 1, stdout: '', stderr: 'Traceback\nOSError: disk full' } }
      return { value: { exitCode: 0, stdout: outcome === 'digest' ? 'STATE as of 10:42\nALERTS\n- Orchestrator is archived.\n' : '', stderr: '' } }
    })
    on('session.append', (_, e, next) => { if (e.door === 'note') { notes.push(...e.message.content.filter(c => c.type === 'text').map(c => c.text)); order.push('note') } return next(e) })
    on('prompt.submit', (_, e) => { order.push(e.text); received(); return { drop: 'Test sink' } })
    on('process.spawn', async function* () {
      yield { stream: 'stdout', text: JSON.stringify({ type: 'ready', mic: 'Test microphone', tts: 'mlx/Ryan', mode: 'auto', port: 8766 }) + '\n' }
      await utterance
      yield { stream: 'stdout', text: JSON.stringify({ type: 'utterance', text: 'Merge it.', generation: 0 }) + '\n' }
      await exited
      return { value: { code: 0, signal: null } }
    })
    await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'voice-assistant', args: '' })
    deliver(); await submitted
    await $.command.run({ command: 'voice-assistant', args: 'off' })
    const note = notes.join('\n')
    expect(order.join('|')).toBe('note|Merge it.')
    expect(note).toContain('"source":"voz-runtime"')
    expect(argv.slice(-7)).toEqual(['state', '--session', 'voice-session', '--budget', 'turn', '--limit', '1.5'])
    expect(limit).toBe(2500)
    if (outcome === 'digest') expect(note).toContain('STATE as of 10:42\nALERTS')
    if (outcome === 'empty') expect(note).not.toContain('STATE')
    if (outcome === 'error') expect(note).toContain('STATE BUILD FAILED: state script exited 1: OSError: disk full')
    if (outcome === 'thrown') expect(note).toMatch(/\nSTATE BUILD FAILED: .+/)
  })
}

// The state command prints the digest only for a session in orchestrators.json and nothing for any
// other session (scripts/tests/test_orch_state.py runs the real command for both). The voice turn
// must pass that through: the digest for an orchestrator, and no state text at all for another session.
const ORCHESTRATORS = ['orch-session']
for (const session of ['orch-session', 'work-session'] as const) {
  test(`voice turn in ${session} appends ${session === 'orch-session' ? 'the digest' : 'no state'}`, async ($, on) => {
    let finish!: () => void, deliver!: () => void, received!: () => void
    const exited = new Promise<void>(r => { finish = r })
    const utterance = new Promise<void>(r => { deliver = r })
    const submitted = new Promise<void>(r => { received = r })
    const notes: string[] = []
    const runs: string[][] = []
    mock.store(on, { 'voz.assistant': session })
    on('session.id', () => ({ value: session }))
    on('command.register', () => ({ value: { command: 'voice-assistant' } }))
    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.scroll', () => ({ value: undefined }))
    on('ui.toast', () => ({ value: undefined }))
    on('fs.exists', () => ({ value: true }))
    on('clock.sleep', () => ({ value: undefined }))
    on('http.fetch', (_, e) => { if (JSON.parse(e.init?.body ?? '{}').type === 'quit') finish(); return { value: { ok: true, status: 200, headers: {}, text: '{"ok":true}' } } })
    on('process.run', (_, e) => {
      runs.push(e.argv)
      const id = e.argv[e.argv.indexOf('--session') + 1] ?? ''
      const stdout = ORCHESTRATORS.includes(id) ? 'STATE as of 09:15\nOPEN QUESTIONS\n- Review session asks: merge PR 6?\n' : ''
      return { value: { exitCode: 0, stdout, stderr: '' } }
    })
    on('session.append', (_, e, next) => { if (e.door === 'note') notes.push(...e.message.content.filter(c => c.type === 'text').map(c => c.text)); return next(e) })
    on('prompt.submit', () => { received(); return { drop: 'Test sink' } })
    on('process.spawn', async function* () {
      yield { stream: 'stdout', text: JSON.stringify({ type: 'ready', mic: 'Test microphone', tts: 'mlx/Ryan', mode: 'auto', port: 8766 }) + '\n' }
      await utterance
      yield { stream: 'stdout', text: JSON.stringify({ type: 'utterance', text: 'What is waiting?', generation: 0 }) + '\n' }
      await exited
      return { value: { code: 0, signal: null } }
    })
    await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'voice-assistant', args: '' })
    deliver(); await submitted
    await $.command.run({ command: 'voice-assistant', args: 'off' })
    const stateRuns = runs.filter(argv => argv.includes('state'))
    expect(stateRuns.length).toBe(1)
    expect(stateRuns[0]?.[0]).toBe('/usr/bin/python3')
    expect(stateRuns[0]?.[1]).toMatch(/\/scripts\/ledger\.py$/)
    expect(notes.length).toBe(1)
    const note = notes[0] ?? ''
    expect(JSON.parse(note.split('\n')[0] ?? '').source).toBe('voz-runtime')
    if (session === 'orch-session') expect(note).toContain('\nSTATE as of 09:15\nOPEN QUESTIONS\n- Review session asks: merge PR 6?')
    else expect(note).toBe(note.split('\n')[0] ?? '') // the voice facts line only
  })
}
