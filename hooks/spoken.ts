// Which sentences of a reply the voice speaks. The pane always shows the full reply; the voice
// speaks only the first `limit` sentences of each text block, plus a final question that the cut
// would hide. A block ends at a tool call or at the end of the turn. limit 0 speaks everything.
//
// A "sentence" here is one spoken unit:
// - a run of prose that ends in . ! ? or … followed by white space;
// - one line (a list item, a heading, a line that ends with ":"), because a line break ends a unit;
// - abbreviations such as "e.g." or "Dr." and numbers such as "v1.2" do not end a unit.
// Fenced code blocks are not spoken and do not count. Lines with no letter or digit (rules, table
// borders) are skipped.
//
// The final question: when the last unit of a block ends with "?", sits in plain prose (not in a
// list item, heading, quote or table row) and the limit kept it silent, it is spoken at the end of
// the block. A question that was already spoken is not said again. `finalQuestion: false` turns
// this rule off.

const FENCE = /^\s*(```|~~~)/
// Abbreviations that never end a sentence, lowercase and without the final period.
const NO_BREAK = new Set(['e.g', 'i.e', 'vs', 'cf', 'mr', 'mrs', 'ms', 'dr', 'prof', 'st', 'jr', 'sr', 'no', 'approx', 'fig', 'a.m', 'p.m'])
// Abbreviations that end a sentence only when the next word starts with a capital letter.
const SOFT_BREAK = new Set(['etc', 'inc', 'ltd', 'co'])
// Marks that may close a sentence after its final . ! ? or …: quotes, brackets, and the markdown
// emphasis and code markers (`**Done.**`, `_ok._`, `` `v1`. ``).
const CLOSERS = "[\"'”’)\\]*_`]*"
const OPENERS = /^[("'“‘[*_`]+/
const END = new RegExp(`[.!?…]${CLOSERS}$`)
const PERIOD_END = new RegExp(`\\.${CLOSERS}$`)
const QUESTION_END = new RegExp(`\\?${CLOSERS}$`)
const SPEAKABLE = /[\p{L}\p{N}]/u
// A line that is not plain prose: a list item, a heading, a quote or a table row.
const NOT_PROSE = /^\s*(?:[-*+]\s|\d+[.)]\s|#|>|\|)/
// Enough of a line to tell whether it is prose: its first word and the space after it.
const KNOWN_START = /^\s*\S+\s/

interface Unit { text: string; prose: boolean; spoken: boolean }

/** Splits one line of prose into units. The last unit is open unless `closed` is true. */
function splitLine(line: string, closed: boolean): { done: string[]; open: string } {
  const done: string[] = []
  const words = line.split(/(\s+)/)
  let current = ''
  for (let i = 0; i < words.length; i++) {
    const word = words[i] ?? ''
    current += word
    if (i % 2 === 1 || i + 2 >= words.length) continue // white space, or no next word yet
    if (!END.test(word) || !endsSentence(word, words[i + 2] ?? '')) continue
    if (/^\d+[.)]$/.test(word) && current.trim() === word) continue // an ordered-list marker
    done.push(current.trim())
    current = ''
  }
  if (closed) {
    if (current.trim()) done.push(current.trim())
    return { done, open: '' }
  }
  return { done, open: current }
}

function endsSentence(word: string, next: string): boolean {
  if (!PERIOD_END.test(word)) return true // ! ? and … always end a sentence
  const stem = word.replace(OPENERS, '').replace(PERIOD_END, '').replace(/[*_`]+$/, '').toLowerCase()
  if (NO_BREAK.has(stem)) return false
  if (/^\p{L}$/u.test(stem)) return false // an initial, as in "J. Smith"
  if (SOFT_BREAK.has(stem)) return /^\p{Lu}/u.test(next)
  return true
}

export class SpokenLimit {
  private buf = '' // text of the current block that is not yet split into units
  private inFence = false
  private units = 0 // speakable units seen in this block
  private last: Unit | undefined // the last speakable unit of this block
  private prose: boolean | undefined // the current line is plain prose; undefined until known

  constructor(private readonly limit: number, private readonly finalQuestion = true) {}

  /** Adds streamed text and returns the units to speak now. */
  push(text: string): string[] {
    this.buf += text
    return this.drain(false)
  }

  /** Ends the block (tool call or end of turn) and returns the units still to speak. */
  end(): string[] {
    const out = this.drain(true)
    const last = this.last
    if (this.finalQuestion && last && last.prose && !last.spoken && QUESTION_END.test(last.text)) out.push(last.text)
    this.buf = ''
    this.inFence = false
    this.units = 0
    this.last = undefined
    this.prose = undefined
    return out
  }

  private drain(closed: boolean): string[] {
    const out: string[] = []
    const lines = this.buf.split('\n')
    const open = closed ? undefined : lines.pop() ?? ''
    for (const line of lines) this.take(line, true, out)
    if (open === undefined) {
      this.buf = ''
      return out
    }
    // An open line can still become a fence, and its first word decides whether it is prose:
    // wait for more text before deciding.
    const unknown = this.prose === undefined && !KNOWN_START.test(open)
    if (unknown || FENCE.test(open) || this.inFence || /^\s*`{1,2}$|^\s*~{1,2}$/.test(open)) {
      this.buf = open
      return out
    }
    this.buf = this.take(open, false, out)
    return out
  }

  /** Handles one line; returns the open rest of an unfinished line. */
  private take(line: string, closed: boolean, out: string[]): string {
    if (this.prose === undefined && FENCE.test(line)) {
      this.inFence = !this.inFence
      return ''
    }
    if (this.inFence) return ''
    if (this.prose === undefined) this.prose = !NOT_PROSE.test(line)
    const { done, open } = splitLine(line, closed)
    for (const unit of done) this.add(unit, out)
    if (closed) this.prose = undefined
    return open
  }

  private add(unit: string, out: string[]) {
    if (!SPEAKABLE.test(unit)) return
    this.units++
    const spoken = this.limit <= 0 || this.units <= this.limit
    if (spoken) out.push(unit)
    this.last = { text: unit, prose: this.prose ?? true, spoken }
  }
}
