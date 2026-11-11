import { expect, mock, test } from 'claude-code/testing'

test('a real daemon utterance carries active runtime state into Claude', async ($, on) => {
  let finish!: () => void, deliver!: () => void, received!: () => void
  const exited=new Promise<void>(r=>{finish=r})
  const utterance=new Promise<void>(r=>{deliver=r})
  const submitted=new Promise<void>(r=>{received=r})
  const facts: string[]=[]
  const order: string[]=[]
  mock.store(on,{'voz.assistant':'voice-session'})
  on('session.id',()=>({value:'voice-session'}))
  on('command.register',()=>({value:{command:'voice-assistant'}}))
  on('session.start',(_,e)=>({cwd:e.cwd}))
  on('ui.open',()=>({value:{isPlaced:true}}))
  on('ui.scroll',()=>({value:undefined}))
  on('ui.toast',()=>({value:undefined}))
  on('fs.exists',()=>({value:true}))
  on('clock.sleep',()=>({value:undefined}))
  on('http.fetch',(_,e)=>{if(JSON.parse(e.init?.body??'{}').type==='quit')finish();return{value:{ok:true,status:200,headers:{},text:'{"ok":true}'}}})
  on('session.append',(_,e,next)=>{if(e.door==='note'){facts.push(...e.message.content.filter(c=>c.type==='text').map(c=>c.text));order.push('facts')}return next(e)})
  on('prompt.submit',(_,e)=>{order.push(e.text);received();return{drop:'Test sink'}})
  on('process.run',()=>({value:{exitCode:0,stdout:'',stderr:''}}))
  on('process.spawn',async function*(){
    yield{stream:'stdout',text:JSON.stringify({type:'ready',mic:'Test microphone',tts:'mlx/Ryan',mode:'auto',port:8766})+'\n'}
    await utterance
    yield{stream:'stdout',text:JSON.stringify({type:'utterance',text:'Hello there.',generation:0})+'\n'}
    await exited
    return{value:{code:0,signal:null}}
  })
  await $.session.start({cwd:'/',surface:'desktop',isInteractive:true})
  await $.command.run({command:'voice-assistant',args:''})
  deliver();await submitted
  await $.command.run({command:'voice-assistant',args:'off'})
  expect(facts.join(' ')).toContain('"microphoneCapture":"active"')
  expect(order.join('|')).toBe('facts|Hello there.')
})

for (const mode of ['drop', 'accepted'] as const) {
  test(`voice delivery shows ${mode} acknowledgement instead of silently losing speech`, async ($, on) => {
    let finish!: () => void, drained!: () => void
    const exited = new Promise<void>(r => { finish = r })
    const consumed = new Promise<void>(r => { drained = r })
    let submissions = 0
    let recovered = ''
    mock.store(on, { 'voz.assistant': 'voice-session' })
    on('session.id', () => ({ value: 'voice-session' }))
    on('command.register', () => ({ value: { command: 'voice-assistant' } }))
    on('session.start', (_, e) => ({ cwd: e.cwd }))
    if (mode === 'accepted') {
      let seed = true
      on('state.get', { plugin: 'voz', key: 'rows' }, (_, e, next) => {
        if (!seed) return next(e)
        seed = false
        return { value: { value: [{ id: 1, kind: 'me', text: 'Older unsent speech', delivery: 'blocked', deliveryReason: 'Older refusal' }], version: 0 } }
      })
    }
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.scroll', () => ({ value: undefined }))
    on('ui.toast', () => ({ value: undefined }))
    on('fs.exists', () => ({ value: true }))
    on('clock.sleep', () => ({ value: undefined }))
    on('http.fetch', (_, e) => { if (JSON.parse(e.init?.body ?? '{}').type === 'quit') finish(); return { value: { ok: true, status: 200, headers: {}, text: '{"ok":true}' } } })
    on('prompt.submit', (_, e) => { submissions++; return mode === 'drop' ? { drop: 'The session was stopped' } : { text: e.text } })
    on('prompt.read', () => ({ value: { text: 'Existing draft', cursor: 14 } }))
    on('prompt.fill', (_, e) => { recovered = e.text; return { isFilled: true, text: 'Existing draft' + e.text, cursor: 60 } })
    on('process.spawn', async function* () {
      yield { stream: 'stdout', text: JSON.stringify({ type: 'ready', mic: 'Test mic', tts: 'turbo', mode: 'auto', port: 8766 }) + '\n' }
      yield { stream: 'stdout', text: JSON.stringify({ type: 'utterance', text: 'Can you give me more details?', generation: 0 }) + '\n' }
      drained(); await exited
      return { value: { code: 0, signal: null } }
    })
    await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
    await $.command.run({ command: 'voice-assistant', args: '' })
    await consumed
    const pane = await $.ui.mount({ plugin: 'voz', component: 'Pane', requestId: 'voz', surface: 'desktop', props: { title: 'Voice', isFocused: true, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} } })
    expect(await pane.find({ type: 'Text', text: mode === 'drop' ? 'Not sent to Claude' : 'Accepted by Claude' })).toBeDefined()
    if (mode === 'drop') {
      expect(await pane.find({ type: 'Text', text: /The session was stopped/ })).toBeDefined()
      expect(await pane.find({ type: 'Button', label: 'Put in chat' })).toBeDefined()
      await pane.press({ key: 'recover-1' })
      expect(recovered).toBe('\nCan you give me more details?')
      expect(await pane.find({ type: 'Text', text: /Review them and press Send/ })).toBeDefined()
    }
    if (mode === 'accepted') {
      expect(await pane.find({ type: 'Text', text: 'Not sent to Claude' })).toBeDefined()
      expect(await pane.find({ type: 'Text', text: 'Older refusal' })).toBeDefined()
    }
    expect(submissions).toBe(1)
    await pane.unmount()
    await $.command.run({ command: 'voice-assistant', args: 'off' })
  })
}

test('completed recognition clears a stale Hearing preview', async ($, on) => {
  let finish!: () => void, drained!: () => void
  const exited = new Promise<void>(r => { finish = r })
  const consumed = new Promise<void>(r => { drained = r })
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
  on('process.spawn', async function* () {
    for (const event of [{ type: 'ready', mic: 'Test mic', tts: 'turbo', mode: 'auto', port: 8766 }, { type: 'live', text: 'Old partial transcription', generation: 0 }, { type: 'state', state: 'listening' }]) {
      yield { stream: 'stdout', text: JSON.stringify(event) + '\n' }
    }
    drained(); await exited
    return { value: { code: 0, signal: null } }
  })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'voice-assistant', args: '' })
  await consumed
  const pane = await $.ui.mount({ plugin: 'voz', component: 'Pane', requestId: 'voz', surface: 'desktop', props: { title: 'Voice', isFocused: true, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} } })
  expect(await pane.find({ type: 'Text', text: /Hearing: Old partial/ })).toBeUndefined()
  await pane.unmount()
  await $.command.run({ command: 'voice-assistant', args: 'off' })
})
