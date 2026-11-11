import { expect, test } from 'claude-code/testing'

// The mod's classic.PermissionRequest hook: with no daemon running (st.running false) it passes the question to the dialog.
test('with voice off, the permission goes to the dialog', async ($, on) => {
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('classic.PermissionRequest', () => ({}))
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const r = await $.classic.PermissionRequest({ tool_name: 'Bash', tool_input: { command: 'ls' } } as never)
  expect(r).toBeDefined()
})

