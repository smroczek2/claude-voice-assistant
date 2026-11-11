// voz: voice conversation in this Claude desktop session.
//
// Launches the voice daemon (microphone → VAD → Whisper; local TTS) as a child process,
// submits each dictated sentence here as a prompt, picks up the streamed reply text
// (turn.step) and sends it to the daemon sentence by sentence to be read aloud, and draws a pane
// next to the chat with the state, the transcript, what is being said and the settings.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import { SpokenLimit } from './spoken'
import type { MeetingEntry, Row, VozLanguage, VozSettings, VozStatus, VozVoice, VozDevice } from '../types'

const PANE = 'voz'
const MAX_ROWS = 40
const STORE_KEY = 'voz.settings'
const OWNER_KEY = 'voz.assistant' // id of the session that is the voice assistant (the last one where /voice-assistant ran)

// The voice session is a hands-free assistant over the other sessions, not the one that does the work.
const ASSISTENTE = `You are the user's hands-free voice assistant for the Claude Code desktop app. This session exists to talk: keep every turn short and never start long work here. The user may have other orchestrator sessions. Relay requests to the chosen orchestrator and let it coordinate its own children. If the target is ambiguous, ask which session. Never claim delivery without a successful tool result. Never change another session's permission mode.
The real work happens in the user's other sessions. To know what is going on, use the session tools (load them with ToolSearch first if they are deferred): mcp__ccd_session_mgmt__list_sessions lists the other sessions with title, state, branch and PR; mcp__ccd_session_mgmt__list_events reads what a session has been doing or asking; mcp__ccd_session_mgmt__search_session_transcripts finds where something was discussed; mcp__ccd_session_mgmt__send_message (or SendMessage with the session id) delivers an instruction or an answer into a session.
When asked how things are going, read the sessions and summarise each active one in a sentence: what it is doing and whether it waits for the user. Answer any question about another session (what it did, decided, changed, asked, or where it got stuck) by reading its recent transcript with list_events, paging back if needed, or by searching the transcripts when the user does not name the session; answer from what you read, in a few spoken sentences, and say so when the transcript does not tell. When a session is waiting on the user, say its question in your own words, take the user's spoken answer and forward it with send_message, then confirm. Send a message into a session only when the user asks you to. When the user asks for new work, hand it to the right session or propose a new one; do not do it here.
Normal permission dialogs remain the user's approval surface. Refer to sessions by title and projects by folder name; never read ids, paths or URLs aloud.`

// Spoken-style instructions in the chosen language (other languages use the English version + "answer in X").
const ESTILOS: Record<string, string> = {
  pt_PT: 'Estás numa conversa por voz: o utilizador dita as mensagens e cada frase tua é dita em voz alta assim que termina. Responde em prosa falada, português de Portugal e dá primeiro o resultado, em frases curtas e completas. O tamanho segue a pergunta: uma confirmação leva uma ou duas frases, uma explicação leva o que for preciso. Não uses listas, tabelas, cabeçalhos, negrito nem blocos de código na resposta; se tiveres de mostrar código ou um comando, escreve-o num ficheiro e diz onde está. Não leias caminhos nem URLs; descreve-os. Quando precisas de uma decisão, termina com uma única pergunta clara. Não afirmes decisões que o utilizador ainda não tomou. Antes de uma acção demorada diz numa frase curta o que vais fazer.',
  pt_BR: 'Você está numa conversa por voz: o usuário dita as mensagens e cada frase sua é falada em voz alta assim que termina. Responda em prosa falada, português do Brasil e dê primeiro o resultado, em frases curtas e completas. O tamanho segue a pergunta: uma confirmação leva uma ou duas frases, uma explicação leva o que for preciso. Não use listas, tabelas, títulos, negrito nem blocos de código; se precisar mostrar código ou um comando, escreva em um arquivo e diga onde está. Não leia caminhos nem URLs; descreva-os. Quando precisar de uma decisão, termine com uma única pergunta clara. Não afirme decisões que o usuário ainda não tomou. Antes de uma ação demorada, diga em uma frase curta o que vai fazer.',
  en: 'You are in a voice conversation: the user dictates messages and each sentence you write is spoken aloud as soon as it ends. Answer in spoken prose, the result first, in short complete sentences. Let the question set the length: a confirmation takes one or two sentences, and an explanation takes what it needs. No lists, tables, headings, bold or code blocks in the reply; if you must show code or a command, write it to a file and say where it is. Do not read paths or URLs aloud; describe them. When you need a decision, end with a single clear question. Do not state decisions the user has not made. Before a long action, say in one short sentence what you are about to do.',
  es: 'Estás en una conversación por voz: el usuario dicta los mensajes y cada frase tuya se lee en voz alta en cuanto termina. Responde en prosa hablada, primero el resultado, en frases cortas y completas. La pregunta decide la longitud: una confirmación ocupa una o dos frases, una explicación ocupa lo necesario. Sin listas, tablas, títulos, negrita ni bloques de código; si debes mostrar código o un comando, escríbelo en un archivo y di dónde está. No leas rutas ni URLs; descríbelas. Cuando necesites una decisión, termina con una única pregunta clara. No afirmes decisiones que el usuario aún no ha tomado. Antes de una acción larga, di en una frase corta lo que vas a hacer.',
  fr: 'Tu es dans une conversation vocale : l’utilisateur dicte ses messages et chaque phrase que tu écris est lue à voix haute dès qu’elle se termine. Réponds en prose parlée, le résultat d’abord, en phrases courtes et complètes. La question fixe la longueur : une confirmation prend une ou deux phrases, une explication prend ce qu’il faut. Pas de listes, tableaux, titres, gras ni blocs de code ; si tu dois montrer du code ou une commande, écris-le dans un fichier et dis où il est. Ne lis pas les chemins ni les URL ; décris-les. Quand tu as besoin d’une décision, termine par une seule question claire. N’affirme pas de décisions que l’utilisateur n’a pas prises. Avant une action longue, dis en une phrase courte ce que tu vas faire.',
  de: 'Du bist in einem Sprachgespräch: der Nutzer diktiert Nachrichten und jeder Satz von dir wird vorgelesen, sobald er endet. Antworte in gesprochener Prosa, das Ergebnis zuerst, in kurzen vollständigen Sätzen. Die Frage bestimmt die Länge: eine Bestätigung braucht ein oder zwei Sätze, eine Erklärung so viele wie nötig. Keine Listen, Tabellen, Überschriften, Fettdruck oder Codeblöcke; wenn du Code oder einen Befehl zeigen musst, schreibe ihn in eine Datei und sage, wo sie liegt. Lies keine Pfade oder URLs vor; beschreibe sie. Wenn du eine Entscheidung brauchst, ende mit einer einzigen klaren Frage. Behaupte keine Entscheidungen, die der Nutzer nicht getroffen hat. Vor einer längeren Aktion sage in einem kurzen Satz, was du tun wirst.',
  it: 'Sei in una conversazione vocale: l’utente detta i messaggi e ogni tua frase viene letta ad alta voce appena termina. Rispondi in prosa parlata, prima il risultato, in frasi brevi e complete. La domanda decide la lunghezza: una conferma richiede una o due frasi, una spiegazione quanto serve. Niente elenchi, tabelle, titoli, grassetto o blocchi di codice; se devi mostrare codice o un comando, scrivilo in un file e di’ dove si trova. Non leggere percorsi o URL; descrivili. Quando ti serve una decisione, termina con una sola domanda chiara. Non affermare decisioni che l’utente non ha preso. Prima di un’azione lunga, di’ in una frase breve cosa stai per fare.',
}
function estiloPara(lang: string, label: string): string {
  return ESTILOS[lang] ?? `${ESTILOS['en']} Always answer in ${label || lang}.`
}

// Short acknowledgements spoken while Claude works: they are not a new message.
const BACKCHANNEL = new Set(['ok', 'okay', 'yes', 'yeah', 'right', 'got it', 'go on', 'sure', 'sim', 'hum', 'hmm', 'certo', 'está bem', 'continua', 'boa', 'exacto', 'exato', 'pois'])
const SENT_END = /(?<=[.!?…])\s+/
// After the spoken-sentence cut, still speak a final question in plain prose (hooks/spoken.ts).
// Set to false to speak only the first sentences.
const SPEAK_FINAL_QUESTION = true
// Commands that interrupt the work in progress (besides the Stop button).
const STOP_WORDS = new Set(['pára', 'para', 'parar', 'pára aí', 'stop', 'cancel', 'forget it', 'claude stop', 'cancela', 'cancelar', 'esquece', 'claude pára', 'claude para'])

const DEFAULT_SETTINGS: VozSettings = { voice: 'turbo:default', sensitivity: 'media', turn_gap_ms: 2500, mode: 'auto', barge_in: true, speed: 1, expression: 'restrained', language: 'en', cleanup: 'regras', input_device: '', output_device: '' }
const SPEEDS = [
  { value: '0.85', label: '0.85×' },
  { value: '1', label: '1.0×' },
  { value: '1.15', label: '1.15×' },
  { value: '1.3', label: '1.3×' },
  { value: '1.5', label: '1.5×' },
]
const neuralVoice = (id: string) => id.startsWith('mlx:') || id.startsWith('kokoro:') || id === 'turbo:default'
function voiceOptions(list: VozVoice[]): VozVoice[] {
  return [...new Map(list.filter(v => neuralVoice(v.id)).map(v => [v.id, v])).values()]
}

const STATUS_DEFAULT: VozStatus = {
  state: 'off', mic: '', output: '', tts: '', level: 0, ptt: false, muted: false, note: '', saying: '',
  showSettings: false, settings: DEFAULT_SETTINGS, voices: [] as VozVoice[], languages: [] as VozLanguage[], search: '', results: [] as VozVoice[],
  devices: { inputs: [] as VozDevice[], outputs: [] as VozDevice[] },
  meeting: { on: false, title: '', path: '', speakers: [] as string[], entries: [] as MeetingEntry[] },
}
const status = atom({ plugin: 'voz', key: 'status' } as const, STATUS_DEFAULT)

// Session state survives mod reloads: state saved by an earlier version may lack
// new fields. Always read through here, with defaults filling in whatever is missing.
async function readStatus($: EngineInterface): Promise<VozStatus> {
  const s = ((await read($, status)) ?? {}) as Partial<VozStatus>
  // A hot reload can preserve atoms without replaying session.start. Only this
  // module owns its daemon; saved capture state cannot prove that it is alive.
  const staleRuntime = !st.running && ['starting', 'listening', 'recording', 'transcribing', 'speaking', 'muted'].includes(s.state ?? '')
  return {
    ...STATUS_DEFAULT,
    ...s,
    ...(staleRuntime ? { state: 'off' as const, muted: false, ptt: false, level: 0, saying: '', note: '' } : {}),
    settings: { ...DEFAULT_SETTINGS, ...(s.settings ?? {}) },
    meeting: { ...STATUS_DEFAULT.meeting, ...(s.meeting ?? {}) },
    voices: voiceOptions(s.voices ?? []),
    languages: s.languages ?? [],
    results: voiceOptions(s.results ?? []),
  }
}
const rows = atom({ plugin: 'voz', key: 'rows' } as const, [] as Row[])

type DaemonEvent =
  | { type: 'ready'; mic: string; output?: string; muted?: boolean; tts: string; mode: 'auto' | 'ptt'; port: number; settings?: VozSettings; voices?: VozVoice[] }
  | { type: 'mute'; on: boolean }
  | { type: 'keyword'; action: 'skip' | 'stop_voice' | 'abort'; generation?: number }
  | { type: 'abort'; generation?: number }
  | { type: 'state'; state: string }
  | { type: 'level'; v: number }
  | { type: 'utterance'; text: string; generation?: number }
  | { type: 'partial'; text: string; generation?: number }
  | { type: 'live'; text: string; generation?: number }
  | { type: 'discarded'; reason: string }
  | { type: 'spoken'; text: string }
  | { type: 'mode'; mode: 'auto' | 'ptt' }
  | { type: 'note'; text: string }
  | { type: 'fatal'; error: string }
  | { type: 'devices'; mic: string; output: string }
  | { type: 'bye'; reason: string }
  | ({ type: 'settings' } & Partial<VozSettings>)
  | { type: 'meeting'; on: boolean; title: string; path: string; speakers: string[]; entries?: number }
  | { type: 'meeting_entry'; t: string; who: string; text: string; similarity: number; new: boolean; speakers: string[] }
  | { type: 'meeting_speakers'; speakers: string[]; relabeled?: number; entries?: MeetingEntry[] }

// Module state: resets on every reload (the daemon is killed along with the module).
const st = {
  vozDir: '',
  port: 8766,
  tts: 'turbo',
  token: '', // per-start secret: sent to the daemon on stdin and on every HTTP request
  running: false,
  wanted: true,
  captureVersion: 0,
  captureAllowed: true,
  minDaemonGeneration: 0,
  child: undefined as ReturnType<EngineInterface['process']['spawn']> | undefined,
  nextId: 1,
  activeTurn: undefined as string | undefined,
  lastLevelAt: 0,
  lastBars: -1,
  lastScrollAt: 0,
  lastLiveAt: 0,
  streamBuf: '',
  mergeBuf: '',
  spokenInTurn: 0,
  spokenSentences: 0, // sentences spoken per text block; 0 (the default) speaks everything
  spoken: new SpokenLimit(0, SPEAK_FINAL_QUESTION),
  liveRowId: undefined as number | undefined,
  summaryMinutes: 15,
  lastErr: '', // last stderr line from the daemon, to explain an unexpected exit
  fatal: '', // fatal error reported by the daemon (`fatal` event)
  restarts: 0, // consecutive automatic restarts (avoids a loop if the daemon exits cleanly without stopping)
  silencedTurn: undefined as string | undefined, // turn whose reading was silenced by a keyword
  meetingTimer: undefined as undefined | { cancel: () => void },
  meetingCount: 0,
  summarizedCount: 0,
  summaryFrom: '',
  liveText: '',
  // all row writes go through this queue, in order: prevents concurrent
  // deltas from creating several boxes or overwriting one another
  chain: Promise.resolve() as Promise<unknown>,
}

function enqueue(fn: () => Promise<unknown>): Promise<unknown> {
  st.chain = st.chain.then(fn, fn)
  return st.chain
}

// ---------- daemon: comandos e eventos ----------

async function cmd($: EngineInterface, body: Record<string, unknown>): Promise<Record<string, unknown> | undefined> {
  try {
    const r = await $.http.fetch(`http://127.0.0.1:${st.port}/cmd`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Voz-Token': st.token },
      body: JSON.stringify(body),
    })
    return r.ok ? (JSON.parse(r.text) as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

async function listening($: EngineInterface, on: boolean) {
  const version = ++st.captureVersion
  st.captureAllowed = false
  const r = await cmd($, { type: 'mute', on: !on })
  if (version !== st.captureVersion) return undefined
  if (r?.['ok'] === true) {
    if (typeof r['generation'] === 'number') st.minDaemonGeneration = Math.max(st.minDaemonGeneration, r['generation'])
    st.captureAllowed = on
    // The acknowledged control must update its own label, even if the event
    // stream is busy with recognition or conversation rendering.
    await setStatus($, { muted: !on, state: on ? 'listening' : 'muted', note: '', level: 0 })
  }
  return r
}

function captureCurrent(version: number): boolean {
  return st.wanted && st.captureAllowed && version === st.captureVersion
}

async function voiceFacts($: EngineInterface): Promise<string> {
  const s = await readStatus($)
  const microphoneCapture = !st.running && s.state === 'off' ? 'closed'
    : st.running && (s.muted || !st.captureAllowed) ? 'paused'
    : st.running && ['listening', 'speaking', 'recording', 'transcribing'].includes(s.state) ? 'active'
    : 'unknown'
  return JSON.stringify({ source: 'voz-runtime', observedAt: new Date().toISOString(), voiceState: s.state, daemonRunning: st.running, microphoneCapture, voice: s.settings.voice, speechRate: s.settings.speed, expression: s.settings.expression })
}

// The orchestrator state for a voice turn: the same text the UserPromptSubmit hook gives a typed
// turn, which voice turns do not reach. The script waits at most 1.5 s for a rebuild, then serves
// the saved state. A session that is not an orchestrator gets an empty string. A fault becomes a
// STATE BUILD FAILED line and never stops the turn.
async function orchestratorState($: EngineInterface): Promise<string> {
  try {
    const id = await $.session.id()
    const argv = ['/usr/bin/python3', `${$.plugin.root}/scripts/ledger.py`, 'state', '--session', id, '--budget', 'turn', '--limit', '1.5']
    const r = await $.process.run(argv, { timeoutMs: 2500 })
    if (r.exitCode !== 0) return `STATE BUILD FAILED: state script exited ${r.exitCode}: ${r.stderr.trim().split('\n').pop() ?? ''}`
    return r.stdout.trim()
  } catch (error) {
    return `STATE BUILD FAILED: ${String(error)}`
  }
}

function setStatus($: EngineInterface, patch: Partial<VozStatus>) {
  return update($, status, s => ({ ...s, ...patch }))
}

// Keeps the pane aligned with the last bubble (at most ~4 times per second).
function scrollToEnd($: EngineInterface, force = false) {
  const now = Date.now()
  if (!force && now - st.lastScrollAt < 250) return
  st.lastScrollAt = now
  void $.ui.scroll({ in: PANE, to: 'end' }).catch(() => undefined)
}

function nextRowId(list: Row[]): number {
  st.nextId = Math.max(st.nextId, ...list.map(r => r.id + 1))
  return st.nextId++
}

async function addRow($: EngineInterface, kind: Row['kind'], text: string, live = false) {
  let id = 0
  await enqueue(() => update($, rows, list => {
    id = nextRowId(list)
    return [...list, { id, kind, text, live }].slice(-MAX_ROWS)
  }))
  scrollToEnd($, true)
  return id
}

async function setDelivery($: EngineInterface, id: number, delivery: Row['delivery'], deliveryReason = '') {
  await enqueue(() => update($, rows, list => list.map(r => r.id === id ? { ...r, delivery, deliveryReason } : r)))
  scrollToEnd($, true)
}

function interruptSpeech($: EngineInterface) {
  st.silencedTurn = st.activeTurn
  void cmd($, { type: 'stop' })
}

async function deliverSpeech($: EngineInterface, id: number, text: string, version: number) {
  const blocked = async (reason: string) => {
    await setDelivery($, id, 'blocked', reason)
    await setStatus($, { note: `Not sent to Claude: ${reason}` })
  }
  try {
    await setDelivery($, id, 'sending')
    if (!captureCurrent(version)) return await blocked('Listening changed before this message was sent.')
    // The plugin's own submission skips its enrichment hook.
    const facts = await voiceFacts($)
    if (!captureCurrent(version)) return await blocked('Listening changed before this message was sent.')
    const state = await orchestratorState($)
    if (!captureCurrent(version)) return await blocked('Listening changed before this message was sent.')
    const note = state ? `${facts}\n${state}` : facts
    const appended = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: note }] } })
    if (appended.deny) return await blocked(`Runtime context was refused (${appended.deny}).`)
    if (!captureCurrent(version)) return await blocked('Listening changed before this message was sent.')
    const result = await $.prompt.submit({ text, asUser: true })
    if (result.drop !== undefined) {
      // After a stop, Claude takes no plugin input until the user types. Put the words in the
      // chat box so one press of Send delivers them, instead of losing them silently.
      if (/stopped/i.test(result.drop)) {
        await setDelivery($, id, 'blocked', result.drop)
        return await putSpeechInChat($, text)
      }
      return await blocked(result.drop)
    }
    await setDelivery($, id, 'accepted')
  } catch (error) {
    await blocked(String(error))
  }
}

async function putSpeechInChat($: EngineInterface, text: string) {
  const draft = await $.prompt.read()
  const result = await $.prompt.fill({ text: `${draft.text ? '\n' : ''}${text}`, mode: 'append' })
  await setStatus($, { note: result.isFilled ? 'Your words are in the chat box. Review them and press Send to continue.' : 'Claude could not put the words in the chat box. They are still saved below.' })
}

async function appendLive($: EngineInterface, text: string) {
  await enqueue(async () => {
    st.liveText += text
    const snapshot = st.liveText
    await update($, rows, list => {
      if (st.liveRowId === undefined) {
        const id = nextRowId(list)
        st.liveRowId = id
        return [...list, { id, kind: 'claude' as const, text: snapshot, live: true }].slice(-MAX_ROWS)
      }
      return list.map(r => r.id === st.liveRowId ? { ...r, text: snapshot } : r)
    })
  })
  scrollToEnd($)
}

async function endLive($: EngineInterface) {
  await enqueue(async () => {
    if (st.liveRowId === undefined) return
    const id = st.liveRowId
    const finalText = st.liveText
    st.liveRowId = undefined
    st.liveText = ''
    await update($, rows, list => list.map(r => r.id === id ? { ...r, text: finalText, live: false } : r))
  })
}

async function abortTurn($: EngineInterface) {
  if (st.activeTurn === undefined) return
  const turnId = st.activeTurn
  st.activeTurn = undefined
  await $.turn.abort({ turnId }).catch(() => undefined)
}

function say($: EngineInterface, text: string) {
  if (!st.wanted) return
  const t = text.trim()
  if (!t) return
  if (st.silencedTurn !== undefined && st.silencedTurn === st.activeTurn) return
  void cmd($, { type: 'say', text: t })
}

// The first sentence of each reply goes out immediately (latency). The following ones are merged into
// growing blocks (60, 120, 200… characters): each block plays long enough for the
// next to be generated, and there are fewer and fewer cuts at full stops.
const MERGE_STEPS = [0, 60, 120, 200, 280]
function sayMerged($: EngineInterface, sentence: string, force = false) {
  const limit = MERGE_STEPS[Math.min(st.spokenInTurn, MERGE_STEPS.length - 1)] ?? 280
  st.mergeBuf = (st.mergeBuf + ' ' + sentence).trim()
  if (force || st.mergeBuf.length >= limit) {
    if (st.mergeBuf) {
      say($, st.mergeBuf)
      st.spokenInTurn++
    }
    st.mergeBuf = ''
  }
}

function flushStream($: EngineInterface) {
  if (st.spokenSentences > 0) {
    for (const s of st.spoken.end()) sayMerged($, s)
    sayMerged($, '', true)
  } else if (st.streamBuf.trim()) sayMerged($, st.streamBuf, true)
  else if (st.mergeBuf) sayMerged($, '', true)
  st.streamBuf = ''
}

// settings: applied to the daemon and saved between sessions
async function applySettings($: EngineInterface, patch: Partial<VozSettings>) {
  const current = (await readStatus($)).settings
  const next = { ...current, ...patch }
  const live = st.running
  const r = await cmd($, { type: 'set', ...patch })
  if (live && r?.['ok'] !== true) {
    await setStatus($, { note: String(r?.['error'] ?? 'Voice settings were not acknowledged. The saved choice is unchanged.') })
    return
  }
  await setStatus($, { settings: next })
  await $.store.set(STORE_KEY, next)
  if (r && typeof r['voice'] === 'string') {
    await setStatus($, { settings: { ...next, voice: r['voice'] as string, language: typeof r['language'] === 'string' ? (r['language'] as string) : next.language } })
  }
  if ('language' in patch || 'voice' in patch) {
    const v = await cmd($, { type: 'voices' })
    if (v && Array.isArray(v['voices'])) await setStatus($, { voices: v['voices'] as VozVoice[], results: [] })
  }
}

async function searchVoices($: EngineInterface, query: string) {
  await setStatus($, { search: query, note: 'Finding voices…' })
  const lang = (await readStatus($)).settings.language
  const r = await cmd($, { type: 'voices_search', query, lang, all: query.trim().length > 0 })
  const results = r && Array.isArray(r['voices']) ? (r['voices'] as VozVoice[]) : []
  await setStatus($, { results, note: results.length ? `${results.length} voices found` : 'No voices found' })
}

async function installVoice($: EngineInterface, id: string) {
  await setStatus($, { note: 'Preparing the selected voice…' })
  const r = await cmd($, { type: 'voice_install', id })
  if (r?.['ok'] !== true) {
    await setStatus($, { note: String(r?.['error'] ?? 'Voice installation was not acknowledged.') })
    return
  }
  const current = (await readStatus($)).settings
  const voice = r && typeof r['voice'] === 'string' ? (r['voice'] as string) : id
  const next = { ...current, voice }
  await setStatus($, { settings: next, voices: r && Array.isArray(r['voices']) ? (r['voices'] as VozVoice[]) : (await readStatus($)).voices })
  await $.store.set(STORE_KEY, next)
}

// available microphones and audio outputs (list requested from the daemon; changes when headphones are connected)
async function refreshDevices($: EngineInterface) {
  const r = await cmd($, { type: 'devices' })
  if (!r) return
  const lista = (k: string) => (Array.isArray(r[k]) ? (r[k] as VozDevice[]) : [])
  await setStatus($, {
    devices: { inputs: lista('inputs'), outputs: lista('outputs') },
    ...(typeof r['input'] === 'string' ? { mic: r['input'] as string } : {}),
    ...(typeof r['output'] === 'string' ? { output: r['output'] as string } : {}),
  })
}

async function desiredSettings($: EngineInterface): Promise<Partial<VozSettings>> {
  const saved = (await $.store.get(STORE_KEY).catch(() => undefined)) as Partial<VozSettings> | undefined
  // One-time application of the user's approved Turbo choice, preserving rate.
  // Future menu choices are restored normally.
  if (await $.store.get('voz.turboChoiceApplied').catch(() => undefined) !== true) {
    const selected = { ...saved, voice: 'turbo:default' }
    await $.store.set(STORE_KEY, selected)
    await $.store.set('voz.turboChoiceApplied', true)
    return selected
  }
  return saved ?? {}
}

async function restoreSettings($: EngineInterface) {
  const saved = await desiredSettings($)
  const restored = { ...saved, voice: saved.voice && neuralVoice(saved.voice) ? saved.voice : DEFAULT_SETTINGS.voice, expression: saved.expression ?? 'restrained' }
  const r = await cmd($, { type: 'set', ...restored, quiet: true }) // no greeting: only restores what was there
  if (r?.['ok'] !== true) {
    await setStatus($, { note: String(r?.['error'] ?? 'Saved voice could not be restored. Select a voice to retry.') })
    return
  }
  const current = (await readStatus($)).settings
  const settings = { ...current, ...restored, ...(r && typeof r['voice'] === 'string' ? { voice: r['voice'] as string } : {}) }
  await setStatus($, { settings })
  await $.store.set(STORE_KEY, settings)
}

async function handleEvent($: EngineInterface, ev: DaemonEvent) {
  if (!st.wanted && ev.type !== 'bye' && ev.type !== 'fatal') return
  const captureVersion = st.captureVersion
  if (ev.type === 'utterance' || ev.type === 'partial' || ev.type === 'live' || ev.type === 'keyword' || ev.type === 'abort') {
    if (!captureCurrent(captureVersion) || (ev.generation !== undefined && ev.generation < st.minDaemonGeneration)) return
  }
  switch (ev.type) {
    case 'ready': {
      st.restarts = 0
      const settings = ev.settings ?? DEFAULT_SETTINGS
      await setStatus($, { state: 'listening', mic: ev.mic, output: ev.output ?? '', muted: ev.muted ?? false, ptt: false, tts: ev.tts, note: '', settings, voices: ev.voices ?? [] })
      await restoreSettings($)
      await refreshDevices($)
      const langs = await cmd($, { type: 'languages' })
      if (langs && Array.isArray(langs['languages'])) await setStatus($, { languages: langs['languages'] as VozLanguage[] })
      $.ui.toast(`Listening (${ev.mic})`)
      break
    }
    case 'state': {
      const current = await readStatus($)
      await setStatus($, ev.state === 'speaking' ? { state: 'speaking' } : {
        state: ev.state as VozStatus['state'], saying: '',
        ...(['listening', 'muted', 'off'].includes(ev.state) && current.note.startsWith('Hearing:') ? { note: '' } : {}),
      })
      break
    }
    case 'mute':
      // the daemon says what the microphone is actually doing; the button follows it
      await setStatus($, { muted: ev.on, state: ev.on ? 'muted' : 'listening', note: '', level: 0 })
      break
    case 'keyword':
      if (ev.action === 'stop_voice' || ev.action === 'abort') st.silencedTurn = st.activeTurn // the rest of this reply is not read aloud
      await addRow($, 'note', ev.action === 'skip' ? 'Sentence skipped' : ev.action === 'stop_voice' ? 'Speech stopped; Claude continues' : 'Stopped at your request')
      break
    case 'abort':
      await abortTurn($)
      break
    case 'level': {
      // Only redraws when the number of bars changes (8 levels) and at most 5 times per second:
      // each write redraws the whole band, and constant redraws make the buttons flicker.
      const bars = Math.round(Math.min(1, Math.max(0, ev.v)) * 8)
      const now = Date.now()
      if (bars === st.lastBars || now - st.lastLevelAt < 200) return
      st.lastBars = bars
      st.lastLevelAt = now
      await setStatus($, { level: bars / 8 })
      break
    }
    case 'partial':
      await setStatus($, { note: `Hearing: ${ev.text}` })
      break
    case 'live': {
      const now = Date.now()
      if (now - st.lastLiveAt < 250) return
      st.lastLiveAt = now
      await setStatus($, { note: `Hearing: ${ev.text}` })
      break
    }
    case 'utterance': {
      const text = ev.text.trim()
      await setStatus($, { note: '' })
      if (!text) return
      const clean = text.replace(/[.!?,\s]+$/g, '').toLowerCase()
      if (st.activeTurn !== undefined && BACKCHANNEL.has(clean)) {
        await addRow($, 'note', `"${text}" (acknowledged; Claude continues)`)
        return
      }
      if (STOP_WORDS.has(clean)) {
        if (!captureCurrent(captureVersion)) return
        // only an explicit command interrupts the work in progress
        void cmd($, { type: 'stop' })
        await abortTurn($)
        await addRow($, 'note', 'Stopped at your request')
        return
      }
      const row = await addRow($, 'me', text)
      // Speaking over the voice silences it (the daemon already did that), but does not abort the work in progress:
      // the sentence is queued and goes in when the turn ends.
      if (st.activeTurn !== undefined) await setStatus($, { note: QUEUED_NOTE })
      // Keep reading capture/control events while the host acknowledges delivery.
      // A stopped or limited host can return {drop}; never silently call that sent,
      // and never retry automatically (which could duplicate or restart work).
      void deliverSpeech($, row, text, captureVersion)
      break
    }
    case 'discarded': {
      const note = ignoredNote(ev.reason)
      await setStatus($, { note })
      // A stale "ignored" note reads like a current problem; clear it after a few seconds.
      $.clock.after(IGNORED_NOTE_MS, () => {
        void readStatus($).then(cur => (cur.note === note ? setStatus($, { note: '' }) : undefined)).catch(() => undefined)
      })
      break
    }
    case 'spoken':
      await setStatus($, { saying: ev.text })
      break
    case 'mode': {
      const current = (await readStatus($)).settings
      await setStatus($, { settings: { ...current, mode: ev.mode } })
      break
    }
    case 'note':
      await addRow($, 'note', ev.text)
      break
    case 'devices':
      await setStatus($, { mic: ev.mic, output: ev.output })
      break
    case 'settings': {
      // settings changed outside the pane (by voice or by Claude): reflect and save them
      const { type: _t, ...defs } = ev
      const current = (await readStatus($)).settings
      const next = { ...current, ...defs }
      await setStatus($, { settings: next })
      await $.store.set(STORE_KEY, next).catch(() => undefined)
      if ('voice' in defs || 'language' in defs) {
        const v = await cmd($, { type: 'voices' })
        if (v && Array.isArray(v['voices'])) await setStatus($, { voices: v['voices'] as VozVoice[] })
      }
      break
    }
    case 'bye':
      if (ev.reason === 'handover' && !(await isOwner($))) {
        st.wanted = false // another session took over the voice: this one switches off without error
        await addRow($, 'note', 'Voice moved to another session')
      }
      break
    case 'fatal':
      st.fatal = ev.error
      await addRow($, 'note', `daemon error: ${ev.error}`)
      break
    case 'meeting': {
      const m = (await readStatus($)).meeting
      await setStatus($, { meeting: { ...m, on: ev.on, title: ev.title, path: ev.path, speakers: ev.speakers, entries: ev.on ? [] : m.entries } })
      if (ev.on) {
        $.ui.toast(`Meeting "${ev.title}" is recording: ${ev.path}`)
        st.meetingCount = 0
        st.summarizedCount = 0
        st.summaryFrom = horaAgora()
        st.meetingTimer?.cancel()
        if (st.summaryMinutes > 0) st.meetingTimer = $.clock.every(st.summaryMinutes * 60 * 1000, () => void partialSummary($))
      } else {
        st.meetingTimer?.cancel()
        st.meetingTimer = undefined
      }
      break
    }
    case 'meeting_entry': {
      const m = (await readStatus($)).meeting
      const entry: MeetingEntry = { t: ev.t, who: ev.who, text: ev.text }
      st.meetingCount++
      await setStatus($, { meeting: { ...m, speakers: ev.speakers, entries: [...m.entries, entry].slice(-200) }, note: ev.new ? `New speaker: ${ev.who}` : '' })
      scrollToEnd($, true)
      break
    }
    case 'meeting_speakers': {
      const m = (await readStatus($)).meeting
      await setStatus($, {
        meeting: { ...m, speakers: ev.speakers, entries: ev.entries ?? m.entries },
        note: ev.relabeled ? `Speakers regrouped: ${ev.relabeled} contributions reassigned` : m.on ? '' : '',
      })
      break
    }
  }
}

function horaAgora(): string {
  const d = new Date()
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// Interim summary: every N minutes, Claude summarises the new contributions in a file next to the minutes.
async function partialSummary($: EngineInterface) {
  const m = (await readStatus($)).meeting
  if (!m.on || !m.path || st.meetingCount <= st.summarizedCount) return
  const desde = st.summaryFrom
  const ate = horaAgora()
  const resumos = m.path.replace(/\.md$/, '') + '-summaries.md'
  st.summarizedCount = st.meetingCount
  st.summaryFrom = ate
  await addRow($, 'note', `interim summary ${desde}–${ate} requested from Claude`)
  void $.prompt.submit({
    text:
      `Meeting "${m.title}" in progress. Read the contributions between ${desde} and ${ate} in the file ${m.path} and add to the file ${resumos} ` +
      `a section "## ${desde}–${ate}" with a short prose summary, the decisions made and the tasks with their owners, based only on what was said. ` +
      'Create the file if it does not exist. Do not say anything aloud: reply only "Summary saved."',
  })
}

// End of the meeting: asks Claude for the formal minutes based on the contributions and the interim summaries.
async function endMeeting($: EngineInterface) {
  st.meetingTimer?.cancel()
  st.meetingTimer = undefined
  const r = await cmd($, { type: 'meeting', on: false })
  const path = r && typeof r['path'] === 'string' ? (r['path'] as string) : ''
  const entries = r && typeof r['entries'] === 'number' ? (r['entries'] as number) : 0
  if (!path || entries === 0) return path
  const acta = path.replace(/\.md$/, '') + '-minutes.md'
  const resumos = path.replace(/\.md$/, '') + '-summaries.md'
  void $.prompt.submit({
    text:
      `The meeting has ended. Read the file ${path} (contributions with time and speaker) and, if it exists, ${resumos} (interim summaries), ` +
      `and write the formal minutes to ${acta}, in English, with: title, date and time, participants, prose summary, ` +
      'decisions made, tasks with owner and deadline when stated, and open points. Do not invent anything that is not in the contributions. ' +
      'Then tell me the essentials in two or three sentences and where the minutes were saved.',
  })
  return path
}

function newToken(): string {
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(32))
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
}

async function pumpDaemon($: EngineInterface) {
  st.token = newToken()
  const child = $.process.spawn({
    argv: [`${st.vozDir}/.venv/bin/voz-daemon`, '--port', String(st.port), '--tts', st.tts, '--token-stdin'],
    cwd: st.vozDir,
    input: `${st.token}\n`,
  })
  st.child = child
  let buf = ''
  try {
    for await (const chunk of child) {
      if (chunk.stream === 'stderr') {
        $.ui.log(chunk.text, { to: 'debug' })
        const util = chunk.text.split('\n').map(l => l.trim()).filter(l => l && !/Warning|WARNING|transformers|resource_tracker|codebooks|tokenizer/.test(l)).pop()
        if (util) st.lastErr = util.slice(-200)
        continue
      }
      buf += chunk.text
      let i: number
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim()
        buf = buf.slice(i + 1)
        if (!line) continue
        try {
          await handleEvent($, JSON.parse(line) as DaemonEvent)
        } catch (err) {
          $.ui.log(`voz: invalid event: ${String(err)}`, { to: 'debug' })
        }
      }
    }
  } catch (err) {
    await setStatus($, { state: 'error', note: `daemon: ${String(err)}` })
  } finally {
    if (st.child === child) st.child = undefined
    st.running = false
    if (st.wanted && st.fatal === '' && st.lastErr === '' && st.restarts < 2 && (await isOwner($))) {
      st.restarts++
      // clean exit requested by us (handover to this same session, reload): start again
      await setStatus($, { state: 'off', level: 0, saying: '', note: '' })
      startDaemon($)
    } else if (st.wanted) {
      // nobody asked to switch off: the daemon died; keep the band with the reason instead of hiding it
      const motivo = st.fatal || st.lastErr || 'it stopped without giving a reason'
      await setStatus($, { state: 'error', level: 0, saying: '', note: `Voice stopped: ${motivo}. Use /voice-assistant to restart.` })
    } else {
      await setStatus($, { state: 'off', level: 0, saying: '', note: '' })
    }
    st.fatal = ''
  }
}

// Where the daemon lives: the `vozDir` option, or the daemon/ folder inside the plugin itself.
// The first time without a .venv, runs `uv sync --frozen` there: it installs exactly the locked packages.
// Models are not downloaded here; setup downloads and hash-checks the pinned models.
async function prepareDaemon($: EngineInterface): Promise<boolean> {
  if (!st.vozDir) st.vozDir = `${$.plugin.root}/daemon`
  const bin = `${st.vozDir}/.venv/bin/voz-daemon`
  if (await $.fs.exists(bin)) return true
  await setStatus($, { state: 'starting', note: 'Installing voice dependencies for the first time. This may take a few minutes…' })
  const home = (await $.env.get('HOME')) ?? ''
  for (const uv of ['uv', '/opt/homebrew/bin/uv', `${home}/.local/bin/uv`]) {
    try {
      const r = await $.process.run([uv, 'sync', '--frozen'], { cwd: st.vozDir, timeoutMs: 600000 })
      if (r.exitCode === 0 && (await $.fs.exists(bin))) return true
      $.ui.log(`voz: ${uv} sync: ${r.stderr.slice(-400)}`, { to: 'debug' })
    } catch (err) {
      $.ui.log(`voz: ${uv} unavailable: ${String(err)}`, { to: 'debug' })
    }
  }
  await setStatus($, { state: 'error', note: 'Voice dependencies could not be installed. Install uv, then run ./setup.sh install in the engine folder.' })
  return false
}

// ---------- which session has the voice ----------
async function isOwner($: EngineInterface): Promise<boolean> {
  try {
    const me = await $.session.id()
    const owner = await $.store.get(OWNER_KEY)
    return typeof owner === 'string' && owner === me
  } catch {
    return false
  }
}

// This session becomes the assistant: the daemon of another session (same port) is told to quit
// with reason "handover", so its mod switches off without error. Returns true if anything changed.
async function takeOver($: EngineInterface): Promise<boolean> {
  if (await isOwner($)) return false
  await cmd($, { type: 'quit', reason: 'handover' })
  try {
    await $.store.set(OWNER_KEY, await $.session.id())
  } catch {
    return false
  }
  return true
}

function startDaemon($: EngineInterface) {
  if (st.running) {
    st.wanted = true // if it was exiting, the end of the pump starts it again
    return
  }
  st.running = true
  st.wanted = true
  st.captureVersion++
  st.captureAllowed = true
  st.minDaemonGeneration = 0
  st.lastErr = ''
  st.fatal = ''
  void setStatus($, { state: 'starting', note: 'Preparing speech recognition and the selected voice…' })
  void (async () => {
    try {
      const settings = await desiredSettings($)
      const selected = settings.voice ?? DEFAULT_SETTINGS.voice
      st.tts = selected.startsWith('kokoro:') ? 'kokoro' : selected.startsWith('mlx:') ? 'mlx' : 'turbo'
      if (await prepareDaemon($) && st.wanted) await pumpDaemon($)
      else st.running = false
    } catch (err) {
      st.running = false
      await setStatus($, { state: 'error', note: `daemon: ${String(err)}` })
    }
  })()
}

async function stopDaemon($: EngineInterface) {
  st.wanted = false
  st.captureVersion++
  st.captureAllowed = false
  if (!st.running) {
    await setStatus($, { state: 'off', note: '', level: 0, saying: '' })
    return true
  }
  const child = st.child
  // Core Audio's first permission prompt can block the daemon's HTTP loop.
  // Keep Off bounded and terminate only the process stream this session owns.
  await Promise.race([cmd($, { type: 'quit' }), $.clock.sleep(1000)])
  for (let i = 0; i < 10 && st.running; i++) await $.clock.sleep(100)
  if (st.running && child) {
    await Promise.race([child.return(undefined as never).catch(() => undefined), $.clock.sleep(1000)])
  }
  // A quit response is only an acknowledgement. The process loop must finish.
  for (let i = 0; i < 30 && st.running; i++) await $.clock.sleep(100)
  if (st.running) {
    await setStatus($, { state: 'error', note: 'Voice could not confirm microphone shutdown. Try Off again.' })
    return false
  }
  await setStatus($, { state: 'off', note: '', level: 0, saying: '' })
  return true
}

// ---------- pane ----------

const IGNORED_NOTE_MS = 6000
const QUEUED_NOTE = 'Queued until Claude finishes the current task'

/** "Send now": stop Claude's current work so the queued spoken message runs at once. */
async function sendQueuedNow($: EngineInterface) {
  void cmd($, { type: 'stop' })
  await abortTurn($)
  await setStatus($, { note: '' })
}
/** The daemon's discard reasons are diagnostics. Say what happened in plain words, and keep the heard text for checking. */
function ignoredNote(reason: string): string {
  const [kind = '', ...rest] = reason.split(': ')
  const heard = rest.join(': ').trim()
  const quote = heard ? ` ("${heard.length > 50 ? heard.slice(0, 49) + '…' : heard}")` : ''
  const k = kind.toLowerCase()
  if (k.startsWith('too short')) return 'Ignored a very short sound'
  if (k.startsWith('transcription failed')) return 'Could not turn that into text. Please say it again.'
  if (k.startsWith('low confidence') || k.startsWith('repetitive')) return `Did not catch that clearly. Please say it again.${quote}`
  return `Ignored background sound${quote}`
}

const LABEL: Record<string, string> = {
  off: 'Voice off', starting: 'Starting voice', listening: 'Listening', recording: 'Hearing you',
  transcribing: 'Transcribing', speaking: 'Speaking', muted: 'Paused', error: 'Voice error',
}
const LABEL_CURTO: Record<string, string> = {
  off: 'Off', starting: 'Starting', listening: 'Listening', recording: 'Hearing',
  transcribing: 'Transcribing', speaking: 'Speaking', muted: 'Paused', error: 'Error',
}
// One line under the state in the Voice pane: what it means right now.
const HINT: Record<string, string> = {
  off: 'The microphone is closed.', starting: 'Loading the speech models.', listening: 'Say something. I am ready.',
  recording: 'I send your message after a short pause.', transcribing: 'Turning your speech into text.',
  muted: 'The microphone is closed until you resume.', error: '',
}
const COLOR: Record<string, string> = {
  listening: 'green', recording: 'red', transcribing: 'yellow', speaking: 'cyan',
  error: 'red', muted: 'gray', starting: 'yellow', off: 'gray',
}
const GAPS = [
  { value: '800', label: 'Short (0.8 s)' },
  { value: '1200', label: 'Medium (1.2 s)' },
  { value: '2000', label: 'Long (2 s)' },
  { value: '2500', label: 'Longer (2.5 s), default' },
  { value: '3000', label: 'Very long (3 s)' },
]

// The spokenSentences option: a whole number from 0 (speak everything, the default) to 20. A missing or bad
// value falls back to 0, so the voice speaks the whole reply.
function spokenSentencesOption(value: unknown): number {
  const n = Number(value ?? 0)
  return Number.isInteger(n) && n >= 0 && n <= 20 ? n : 0
}

export const register: Register = (on, options) => {
  st.vozDir = String(options['vozDir'] ?? '')
  st.port = Number(options['port'] ?? 8766)
  st.tts = options['tts'] === 'mlx' || options['tts'] === 'kokoro' ? String(options['tts']) : 'turbo'
  st.wanted = options['autoStart'] === true
  st.summaryMinutes = Number(options['summaryMinutes'] ?? 15)
  st.spokenSentences = spokenSentencesOption(options['spokenSentences'])
  st.spoken = new SpokenLimit(st.spokenSentences, SPEAK_FINAL_QUESTION)

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'voice-assistant',
      description: 'Voice assistant: talk to this session by voice, hands-free over your other sessions, meeting minutes (/voice-assistant [off|pause|resume|auto|manual|meeting <title>[, N speakers]|end])',
      argumentHint: 'off|pause|resume|auto|manual|meeting <title>[, N speakers]|end',
    })
    const started = await next(e)
    // Only one session has the microphone and the voice: the one designated with /voice-assistant (saved between sessions).
    // The others stay passive: they neither speak nor listen.
    if (st.wanted && (await isOwner($))) {
      void $.ui.open({ id: PANE, title: 'Voice' })
      startDaemon($)
    } else if (!st.wanted && (await isOwner($))) {
      await setStatus($, { state: 'off', note: '', level: 0, saying: '' })
    }
    return started
  })

  on('command.run', { command: 'voice-assistant' }, async ($, e) => {
    const arg = (e.args ?? '').trim().toLowerCase()
    if (arg === 'off') {
      const stopped = await stopDaemon($)
      if (!stopped) return { text: 'Voice could not confirm microphone shutdown. Try /voice-assistant off again.' }
      return { text: 'Voice assistant off.' }
    }
    if (arg === 'pause' || arg === 'resume') {
      if (!st.running) return { text: 'Voice is off. Run /voice-assistant to start it.' }
      const r = await listening($, arg === 'resume')
      return { text: r?.['ok'] === true ? (arg === 'pause' ? 'Listening paused.' : 'Listening resumed.') : 'The microphone did not acknowledge the change.' }
    }
    if (arg === 'auto' || arg === 'manual') {
      await applySettings($, { mode: arg === 'manual' ? 'ptt' : 'auto' })
      return { text: arg === 'manual' ? 'Manual mode: record with the button.' : 'Automatic mode: speech is detected.' }
    }
    if (arg.startsWith('meeting')) {
      // "/voice-assistant meeting Title" or "/voice-assistant meeting Title, 3 speakers"
      let rest = (e.args ?? '').trim().replace(/^\S+\s*/, '')
      let speakers: number | undefined
      const m = rest.match(/[,;]?\s*(\d+)\s*(speakers?|people|participants?|oradores?|pessoas?)\s*$/i)
      if (m) {
        speakers = Number(m[1])
        rest = rest.slice(0, m.index).trim()
      }
      const title = rest || 'Meeting'
      await $.ui.open({ id: PANE, title: 'Voice', focus: true })
      const r = await cmd($, { type: 'meeting', on: true, title, speakers })
      return { text: r ? `Meeting mode on: "${title}"${speakers ? `, ${speakers} speakers` : ''}. Run "/voice-assistant end" to finish and get the minutes.` : 'The voice assistant is not running.' }
    }
    if (arg === 'end') {
      const path = await endMeeting($)
      return { text: path ? `Meeting ended; the timed transcript is in ${path}. The formal minutes follow.` : 'No meeting was running.' }
    }
    await $.ui.open({ id: PANE, title: 'Voice', focus: true })
    const was = st.running
    const tomou = await takeOver($)
    startDaemon($)
    return { text: was ? 'Voice pane opened.' : tomou ? 'This session is now the voice assistant; the voice is starting.' : 'Voice assistant starting.' }
  })

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    if (!st.running) return composed
    const s = await readStatus($)
    const label = s.languages.find(l => l.code === s.settings.language)?.label ?? ''
    return {
      ...composed,
      sections: [
        ...composed.sections,
        { id: 'voz:assistente', text: ASSISTENTE, scope: 'session' as const },
        { id: 'voz:estilo', text: estiloPara(s.settings.language, label), scope: 'session' as const },
      ],
    }
  })

  on('prompt.submit', async ($, e, next) => {
    // Background updates are not user interruptions. The engine stamps provenance;
    // queued human input also explicitly asks to wait. Spoken barge-in is handled
    // by the daemon, while direct unqueued typed/remote input stops audio here.
    const userInterruption = !e.wait && (e.origin?.kind === 'composer' || e.origin?.kind === 'bridge')
    if (st.running && userInterruption) void cmd($, { type: 'stop' })
    if (await isOwner($)) {
      // Controls change outside the chat transcript. Supply the same current
      // facts used by the UI without rewriting the user's spoken/typed text.
      const facts = await voiceFacts($)
      return next({ ...e, context: [...(e.context ?? []), facts] })
    }
    return next(e)
  }).catch(($, e, next) => next(e)) // never refuses a prompt: if the hook fails, the prompt goes through

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      st.activeTurn = e.turnId
      st.silencedTurn = undefined
      st.streamBuf = ''
      st.mergeBuf = ''
      st.spokenInTurn = 0
      st.spoken = new SpokenLimit(st.spokenSentences, SPEAK_FINAL_QUESTION)
      await endLive($)
    }
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    if (e.agentId !== undefined || !st.running || (await readStatus($)).meeting.on) return yield* next(e)
    for await (const chunk of next(e)) {
      if (chunk.kind === 'text') {
        void appendLive($, chunk.text)
        if (st.spokenSentences > 0) {
          for (const s of st.spoken.push(chunk.text)) sayMerged($, s)
          yield chunk
          continue
        }
        st.streamBuf += chunk.text
        const parts = st.streamBuf.split(SENT_END)
        if (parts.length > 1) {
          for (const s of parts.slice(0, -1)) sayMerged($, s)
          st.streamBuf = parts[parts.length - 1] ?? ''
        }
      } else if (chunk.kind === 'tool') {
        // Claude is about to use a tool: close the sentence in progress; the calls themselves do not appear in the pane
        flushStream($)
        void endLive($)
      } else if (chunk.kind === 'stop') {
        flushStream($)
        void endLive($)
      }
      yield chunk
    }
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {
      st.activeTurn = undefined
      flushStream($)
      await endLive($)
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    await stopDaemon($)
    return next(e)
  })

  // ---------- band above the text box: state and controls, always visible ----------
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const s = await readStatus($)
    if (!st.running && s.state === 'off' && !(await isOwner($))) return next(e)
    if (e.props.hasSurvey) return next(e)
    const els = $.ui.resolve(e)
    const { Box, Text, Button } = els
    if (!st.running && s.state === 'off') {
      return <Box gap={2} alignItems="center">
        <Text dimColor>Voice off — microphone closed</Text>
        <Button key="power" label="Turn listening on" onPress={() => startDaemon($)} />
      </Box>
    }
    const Select = 'Select' in els ? els.Select : undefined
    // three widths: full (≥ 96 columns), compact (≥ 64) and icons only (< 64)
    const cols = e.viewport?.columns ?? 100
    const wide = cols >= 96
    const compact = cols < 64
    const bar = '▮'.repeat(Math.round(s.level * 8)).padEnd(8, '▯')
    const manual = s.settings.mode === 'ptt'
    // Interrupt only matters while Claude answers or a reply plays; hide it at rest.
    const speechPossible = s.state === 'speaking' || st.activeTurn !== undefined || Boolean(s.saying)
    const L = (icon: string, full: string, short: string) => (compact ? icon : wide ? `${icon} ${full}` : `${icon} ${short}`)
    const MODOS = [
      { value: 'auto', label: 'automatic' },
      { value: 'ptt', label: 'manual' },
    ]
    const vozesInstaladas = s.voices.filter(v => v.installed !== false)
    const nomeCurto = (label: string) => label.replace(/\s*\(.*$/, '')

    return (
      <Box flexDirection="column">
        <Box justifyContent="space-between" alignItems="center" flexWrap="wrap" gap={2}>
          {/* left: recording state, voice detection, device */}
          <Box gap={2} alignItems="center">
            <Text color={COLOR[s.state] ?? 'white'} weight="bold">● {compact ? LABEL_CURTO[s.state] ?? s.state : LABEL[s.state] ?? s.state}</Text>
            <Text color={s.level > 0.05 ? 'green' : undefined} dimColor={s.level <= 0.05} mono>{bar}</Text>
            {wide ? <Text dimColor size="footnote">🎙 {s.mic || '—'}{s.output ? ` · 🔈 ${s.output}` : ''}</Text> : null}
          </Box>
          {/* right: actions */}
          <Box gap={1} alignItems="center" flexWrap="wrap">
            {Select && vozesInstaladas.length ? (
              <Select
                key="voice-band"
                label={compact ? undefined : 'Voice'}
                options={vozesInstaladas.map(v => ({ value: v.id, label: nomeCurto(v.label) }))}
                value={vozesInstaladas.some(v => v.id === s.settings.voice) ? s.settings.voice : vozesInstaladas[0]?.id ?? ''}
                onSelect={value => void applySettings($, { voice: value })}
              />
            ) : null}
            {Select ? (
              <Select
                key="mode-band"
                label={compact ? undefined : 'Mode'}
                options={MODOS}
                value={manual ? 'ptt' : 'auto'}
                onSelect={value => void applySettings($, { mode: value as VozSettings['mode'] })}
              />
            ) : (
              <Button
                key="mode-toggle"
                variant="secondary"
                label={manual ? L('✋', 'Mode: manual', 'Manual') : L('〰', 'Mode: automatic', 'Auto')}
                onPress={() => void applySettings($, { mode: manual ? 'auto' : 'ptt' })}
              />
            )}
            {manual ? (
              <Button
                key="ptt"
                variant={s.ptt ? 'primary' : 'secondary'}
                label={s.ptt ? L('⏺', 'Send', 'Send') : L('🎙', 'Record', 'Record')}
                onPress={async () => {
                  const down = !s.ptt
                  await setStatus($, { ptt: down })
                  void cmd($, { type: 'ptt', down })
                }}
              />
            ) : null}
            <Button
              key="pause"
              variant={s.muted ? 'primary' : 'secondary'}
              label={s.muted ? L('▶', 'Resume listening', 'Resume') : L('⏸', 'Pause listening', 'Pause')}
              onPress={async () => {
                const r = await listening($, s.muted)
                if (!r) await setStatus($, { note: 'Voice did not acknowledge the pause request' })
              }}
            />
            {speechPossible ? (
              <Button
                key="stop"
                variant="secondary"
                label={L('⏭', 'Skip this reply', 'Skip')}
                onPress={() => interruptSpeech($)}
              />
            ) : null}
            <Button
              key="pane"
              variant="secondary"
              label={L('☰', 'Conversation', 'Conversation')}
              onPress={async () => {
                await $.ui.open({ id: PANE, title: 'Voice', focus: true })
              }}
            />
            <Button
              key="gear"
              variant={s.showSettings ? 'primary' : 'secondary'}
              label={wide ? '⚙ Settings' : '⚙'}
              onPress={async () => {
                const show = !s.showSettings
                await setStatus($, { showSettings: show })
                if (show) void refreshDevices($)
                if (show) await $.ui.open({ id: PANE, title: 'Voice', focus: true })
              }}
            />
            <Button
              key="power"
              variant="secondary"
              dimColor
              label={st.running ? L('⏻', 'Turn voice off', 'Off') : L('⏻', 'Turn voice on', 'On')}
              onPress={async () => {
                if (st.running) await stopDaemon($)
                else startDaemon($)
              }}
            />
          </Box>
        </Box>
        {s.meeting.on ? (
          <Box gap={2} alignItems="center" flexWrap="wrap">
            <Text color="red" weight="bold">● Meeting "{s.meeting.title}"</Text>
            <Text dimColor size="footnote">{s.meeting.speakers.length} speakers · {s.meeting.entries.length} contributions</Text>
            <Button key="meeting-end" variant="primary" label={compact ? 'End' : 'End and write minutes'} onPress={() => void endMeeting($)} />
          </Box>
        ) : null}
        {s.saying ? <Text color="cyan" wrap="truncate">♪ {s.saying}</Text> : null}
        {s.note === QUEUED_NOTE && st.activeTurn !== undefined ? (
          <Box gap={2} alignItems="center">
            <Text dimColor wrap="truncate">Your message waits until Claude finishes.</Text>
            <Button key="send-now" variant="primary" label="Send now" onPress={() => void sendQueuedNow($)} />
          </Box>
        ) : s.note ? <Text dimColor wrap="truncate">{s.note}</Text> : null}
      </Box>
    )
  })

  // ---------- pane: conversation and settings ----------
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Text, Button } = els
    const Select = 'Select' in els ? els.Select : undefined // not every surface has Select
    const Input = 'Input' in els ? els.Input : undefined
    const s = await readStatus($)
    const list = await read($, rows)
    const voices = s.voices.length ? s.voices : [{ id: s.settings.voice || 'mlx', label: s.tts || 'voice' }]
    const voiceValue = voices.some(v => v.id === s.settings.voice) ? s.settings.voice : voices[0]?.id ?? ''
    const gapValue = GAPS.some(g => g.value === String(s.settings.turn_gap_ms)) ? String(s.settings.turn_gap_ms) : String(DEFAULT_SETTINGS.turn_gap_ms)
    const speedValue = SPEEDS.reduce((best, o) => (Math.abs(Number(o.value) - s.settings.speed) < Math.abs(Number(best) - s.settings.speed) ? o.value : best), '1')
    const cut = (t: string, n: number) => (t.length <= n ? t : '…' + t.slice(t.length - n + 1))
    // your transcript is shown in full; only a long Claude reply is cut from the start
    const visible = list.filter(r => r.kind !== 'tool').slice(-8).map(r => (r.kind === 'claude' ? { ...r, text: cut(r.text, 1500) } : r))

    return (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="column" gap={1} borderStyle="round" borderDimColor paddingX={1}>
          <Text weight="bold" color={COLOR[s.state]}>● {LABEL[s.state] ?? s.state}</Text>
          {s.state === 'speaking' ? (
            <Text dimColor size="footnote">{s.settings.barge_in ? 'Talk or say "stop" to interrupt me.' : 'Press Skip to stop me talking.'}</Text>
          ) : HINT[s.state] ? <Text dimColor size="footnote">{HINT[s.state]}</Text> : null}
          <Text dimColor size="footnote">Voice: {voices.find(v => v.id === s.settings.voice)?.label || s.settings.voice || s.tts || 'Not selected'}</Text>
          <Box gap={1} flexWrap="wrap">
            <Button
              key="power"
              label={st.running ? 'Turn voice off' : 'Turn listening on'}
              onPress={async () => {
                if (st.running) await stopDaemon($)
                else { await takeOver($); startDaemon($) }
              }}
            />
            {st.running ? <Button
              key="pause"
              label={s.muted ? 'Resume listening' : 'Pause listening'}
              onPress={async () => {
                const r = await listening($, s.muted)
                if (!r) await setStatus($, { note: 'The microphone did not acknowledge the change.' })
              }}
            /> : null}
            {st.running && (s.state === 'speaking' || st.activeTurn !== undefined || Boolean(s.saying)) ? <Button key="interrupt" label="Skip this reply" onPress={() => interruptSpeech($)} /> : null}
            <Button key="gear" label={s.showSettings ? 'Hide settings' : 'Voice settings'} onPress={() => setStatus($, { showSettings: !s.showSettings })} />
          </Box>
          {s.note ? <Text dimColor wrap="truncate">{s.note}</Text> : null}
        </Box>
        {s.showSettings && !Select ? (
          <Text dimColor>This view does not support selectors; use /voice-assistant manual or /voice-assistant auto.</Text>
        ) : null}
        {s.showSettings && Select ? (
          <Box flexDirection="column" gap={1} borderStyle="round" borderDimColor paddingX={1}>
            <Select
              key="language"
              label="Language"
              options={(s.languages.length ? s.languages : [{ code: s.settings.language, label: s.settings.language }]).map(l => ({ value: l.code, label: l.label }))}
              value={s.settings.language}
              onSelect={value => void applySettings($, { language: value })}
            />
            <Select
              key="voice"
              label="Voice"
              options={voices.map(v => ({ value: v.id, label: v.label }))}
              value={voiceValue}
              onSelect={value => void applySettings($, { voice: value })}
            />
            <Select
              key="input-device"
              label="Microphone"
              options={[{ value: '', label: `System default${s.settings.input_device ? '' : ` (${s.mic || '?'})`}` }, ...s.devices.inputs.map(d => ({ value: d.id, label: d.label }))]}
              value={s.devices.inputs.some(d => d.id === s.settings.input_device) ? s.settings.input_device : ''}
              onSelect={value => void applySettings($, { input_device: value })}
            />
            <Select
              key="output-device"
              label="Audio output"
              options={[{ value: '', label: `System default${s.settings.output_device ? '' : ` (${s.output || '?'})`}` }, ...s.devices.outputs.map(d => ({ value: d.id, label: d.label }))]}
              value={s.devices.outputs.some(d => d.id === s.settings.output_device) ? s.settings.output_device : ''}
              onSelect={value => void applySettings($, { output_device: value })}
            />
            <Select
              key="speed"
              label="Speech speed"
              options={SPEEDS}
              value={speedValue}
              onSelect={value => void applySettings($, { speed: Number(value) })}
            />
            <Select
              key="expression"
              label="Voice style"
              options={[{ value: 'restrained', label: 'Calm' }, { value: 'conversational', label: 'Balanced' }, { value: 'natural', label: 'Expressive' }]}
              value={s.settings.expression ?? 'restrained'}
              onSelect={value => void applySettings($, { expression: value as 'restrained' | 'conversational' | 'natural' })}
            />
            {s.settings.voice === 'turbo:default' ? <Text dimColor size="footnote">This voice has one fixed style, so Voice style does not change it.</Text> : null}
            {s.settings.voice.startsWith('kokoro:') ? <Text dimColor size="footnote">This voice has one fixed style, so Voice style does not change it.</Text> : null}
            <Select
              key="sensitivity"
              label="Microphone sensitivity"
              options={[
                { value: 'baixa', label: 'Low: ignores background sound' },
                { value: 'media', label: 'Medium' },
                { value: 'alta', label: 'High: hears quiet speech, and more noise' },
              ]}
              value={s.settings.sensitivity}
              onSelect={value => void applySettings($, { sensitivity: value as VozSettings['sensitivity'] })}
            />
            <Select
              key="gap"
              label="Pause before sending"
              options={GAPS}
              value={gapValue}
              onSelect={value => void applySettings($, { turn_gap_ms: Number(value) })}
            />
            <Text dimColor size="footnote">How long I wait after you stop talking before I send your message.</Text>
            <Select
              key="mode"
              label="Listening mode"
              options={[
                { value: 'auto', label: 'Automatic (detect speech)' },
                { value: 'ptt', label: 'Manual (record with button)' },
              ]}
              value={s.settings.mode}
              onSelect={value => void applySettings($, { mode: value as VozSettings['mode'] })}
            />
            <Select
              key="cleanup"
              label="Tidy my speech"
              options={[
                { value: 'off', label: 'Off: send exactly what I said' },
                { value: 'regras', label: 'Remove "um" and repeats' },
                { value: 'modelo', label: 'Smart cleanup (on this Mac)' },
              ]}
              value={s.settings.cleanup}
              onSelect={value => void applySettings($, { cleanup: value as VozSettings['cleanup'] })}
            />
            <Select
              key="barge"
              label="Let me interrupt by talking"
              options={[
                { value: 'on', label: 'Yes' },
                { value: 'off', label: 'No' },
              ]}
              value={s.settings.barge_in ? 'on' : 'off'}
              onSelect={value => void applySettings($, { barge_in: value === 'on' })}
            />
            {Input ? (
              <Input
                key="voice-search"
                label="Find more voices"
                placeholder="Voice name (blank shows all)"
                value={s.search}
                submitLabel="Search"
                onSubmit={value => void searchVoices($, value)}
              />
            ) : null}
            {s.results.slice(0, 12).map(v => (
              <Box key={`r:${v.id}`} gap={1} alignItems="center">
                <Button
                  key={`use:${v.id}`}
                  variant={v.installed ? 'secondary' : 'primary'}
                  label={v.installed ? 'Use voice' : `Download${v.size_mb ? ` (${v.size_mb} MB)` : ''}`}
                  onPress={() => void installVoice($, v.id)}
                />
                <Text wrap="truncate">{v.label}</Text>
                <Text dimColor size="footnote">{v.lang}</Text>
              </Box>
            ))}
            <Text dimColor size="footnote">All voices run on this Mac. Speech speed does not change pitch.</Text>
          </Box>
        ) : null}

        {s.meeting.on && Input ? (
          <Box flexDirection="column" gap={1} borderStyle="round" borderColor="red" paddingX={1}>
            <Text weight="bold">Meeting "{s.meeting.title}"</Text>
            <Text dimColor size="footnote">{s.meeting.path}</Text>
            <Text dimColor size="footnote">Speakers: {s.meeting.speakers.join(', ') || '—'}. To name a speaker, type "Orador 2 = Sam" below. "Orador 2 = Orador 1" merges two speakers.</Text>
            <Input
              key="rename"
              label="Rename a speaker"
              placeholder="Orador 2 = Sam"
              submitLabel="Rename"
              onSubmit={value => {
                const m = value.match(/^\s*(.+?)\s*=\s*(.+?)\s*$/)
                if (m) void cmd($, { type: 'meeting_rename', speaker: m[1], name: m[2] })
              }}
            />
            {s.meeting.entries.slice(-14).map((e2, i) => (
              <Text key={`m:${i}:${e2.t}`} wrap="wrap">
                <Text dimColor>{e2.t} </Text>
                <Text color="blue" weight="semibold">{e2.who}: </Text>
                {e2.text}
              </Text>
            ))}
          </Box>
        ) : null}
        <Box flexDirection="column" gap={1}>
          {visible.length === 0 && !s.meeting.on && <Text dimColor>Speak or type in the chat. Each reply is read aloud.</Text>}
          {visible.map(r =>
            r.kind === 'me' || r.kind === 'claude' ? (
              <Box
                key={String(r.id)}
                flexDirection="column"
                borderStyle="round"
                borderColor={r.kind === 'me' ? 'blue' : 'gray'}
                borderDimColor={r.kind !== 'me'}
                paddingX={1}
                alignSelf={r.kind === 'me' ? 'flex-end' : 'flex-start'}
              >
                <Text dimColor size="footnote">{r.kind === 'me' ? 'You' : 'Claude'}</Text>
                <Text wrap="wrap">{r.text}{r.live ? ' ▍' : ''}</Text>
                {r.delivery ? <Text dimColor size="footnote">{r.delivery === 'sending' ? 'Waiting for Claude to accept this message' : r.delivery === 'accepted' ? 'Accepted by Claude' : 'Not sent to Claude'}</Text> : null}
                {r.deliveryReason ? <Text size="footnote">{r.deliveryReason}</Text> : null}
                {r.delivery === 'blocked' ? <Button key={`recover-${r.id}`} label="Put in chat" onPress={() => putSpeechInChat($, r.text)} /> : null}
              </Box>
            ) : (
              <Text key={String(r.id)} dimColor size="footnote">{r.kind === 'tool' ? `⚙ ${r.text}` : r.text}</Text>
            ),
          )}
        </Box>
      </Box>
    )
  })
}

