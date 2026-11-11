export type VozState =
  | 'off'
  | 'starting'
  | 'listening'
  | 'recording'
  | 'transcribing'
  | 'speaking'
  | 'muted'
  | 'error'

export type VozVoice = { id: string; label: string; lang?: string; source?: string; installed?: boolean; size_mb?: number }

export type VozLanguage = { code: string; label: string }

export type VozDevice = { id: string; label: string }

export type VozDevices = { inputs: VozDevice[]; outputs: VozDevice[] }

export type VozSettings = {
  voice: string
  sensitivity: 'baixa' | 'media' | 'alta'
  turn_gap_ms: number
  mode: 'auto' | 'ptt'
  barge_in: boolean
  speed: number
  expression?: 'restrained' | 'conversational' | 'natural'
  language: string
  cleanup: 'off' | 'regras' | 'modelo'
  input_device: string
  output_device: string
}

export type VozStatus = {
  state: VozState
  mic: string
  output: string
  tts: string
  level: number
  ptt: boolean
  muted: boolean
  note: string
  saying: string
  showSettings: boolean
  settings: VozSettings
  voices: VozVoice[]
  languages: VozLanguage[]
  search: string
  results: VozVoice[]
  devices: VozDevices
  meeting: VozMeeting
}

export type MeetingEntry = { t: string; who: string; text: string }

export type VozMeeting = { on: boolean; title: string; path: string; speakers: string[]; entries: MeetingEntry[] }

export type RowKind = 'me' | 'claude' | 'tool' | 'note'

export type Row = {
  id: number
  kind: RowKind
  text: string
  live?: boolean
  delivery?: 'sending' | 'accepted' | 'blocked'
  deliveryReason?: string
}

declare module 'claude-code' {
  interface PluginState {
    voz: { status: VozStatus; rows: Row[] }
  }
}

