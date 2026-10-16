// What a card's decision sends to POST /api/actions/<id>/review, the body the review flow has always
// sent: {status, text, note, reasons}. `text` is null unless the words to post differ from the draft.
// A person's own take is stored the same way as an edit, with a note that says it was a take, so
// `learn` and the hand-off read it with no new field.

export type ReviewBody = { status: 'approved' | 'killed' | 'pending'; text: string | null; note: string; reasons: string[] }

/** Where the reply box's words came from: typed fresh (a take), or the draft copied in by Edit. */
export type Origin = 'take' | 'edit'

export const TAKE_NOTE = "A person's own take, posted in place of the draft."

export type Decision =
  | { kind: 'approve'; draft: string | null; composer: string; origin: Origin }
  | { kind: 'kill'; reasons: string[]; note: string }
  | { kind: 'undo' }

export class VerdictError extends Error {}

export function reviewBody(d: Decision): ReviewBody {
  if (d.kind === 'undo') return { status: 'pending', text: null, note: '', reasons: [] }
  if (d.kind === 'kill') {
    if (d.reasons.length === 0) throw new VerdictError('Pick at least one reason, so the kill teaches the next round something.')
    return { status: 'killed', text: null, note: d.note.trim(), reasons: [...new Set(d.reasons)] }
  }
  const words = d.composer.trim()
  if (!words) {
    if (!d.draft?.trim()) throw new VerdictError('Write the reply first; there is no draft to approve.')
    return { status: 'approved', text: null, note: '', reasons: [] }
  }
  if (d.origin === 'edit') {
    return { status: 'approved', text: words === d.draft?.trim() ? null : words, note: '', reasons: [] }
  }
  return { status: 'approved', text: words, note: TAKE_NOTE, reasons: [] }
}

export type Lane = 'reply' | 'quote' | 'post' | 'mention'

/** The one primary action's label for a card in its current state. */
export function primaryLabel(type: Lane, draft: string | null, composer: string, origin: Origin): string {
  const noun = type === 'reply' ? 'reply' : type === 'quote' ? 'quote' : 'post'
  if (!composer.trim()) return draft ? 'Approve draft' : `Approve your ${noun}`
  if (origin === 'edit') return composer.trim() === draft?.trim() ? 'Approve draft' : 'Approve edit'
  return `Approve your ${noun}`
}
