// One decision, laid out as the thread it would join: the post being answered, the person's own reply
// box, then our draft rendered as X would render the reply. The box comes first because a person's own
// words post verbatim and read as human. Edit copies the draft into the box. Once the box has words, they
// are what Approve sends, and the draft below dims rather than moving, so nothing shifts while typing.

import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { api, ApiError, type Card } from '../lib/api'
import { ago, freshness } from '../lib/time'
import { draftTweet, userOf, type Account } from '../lib/tweet'
import { primaryLabel, reviewBody, VerdictError, type Decision, type Origin, type ReviewBody } from '../lib/verdict'
import { MAX_WEIGHTED_CHARS, weightedLength } from '../lib/xtext'
import { missingNote, Post, PostSkeleton, usePost } from './Post'

const ARM_LABELS: Record<string, string> = {
  continuation: 'Continuation',
  styled: 'Styled fallback',
  caption: 'Caption',
  banter: 'Banter',
}
// A one-liner with no draft can only be skipped for reasons about the post, not about a draft's words.
const SKIP_REASONS = ['off_message', 'stale_story', 'wrong_target', 'other']

export type Verdict = { card: Card; body: ReviewBody; text: string }

type Props = {
  card: Card
  account: Account
  reasons: Record<string, string>
  onDecided: (v: Verdict) => void
  /** Set by the queue's keyboard handler: "approve", "edit", "kill" or "reply". */
  command?: { name: string; seq: number } | null
}

export function QueueCard({ card, account, reasons, onDecided, command }: Props) {
  const [composer, setComposer] = useState('')
  const [origin, setOrigin] = useState<Origin>('take')
  const [killing, setKilling] = useState(false)
  const [picked, setPicked] = useState<string[]>([])
  const [killNote, setKillNote] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const box = useRef<HTMLTextAreaElement>(null)
  const killButton = useRef<HTMLButtonElement>(null)
  const firstChip = useRef<HTMLButtonElement>(null)
  const id = useId()
  const target = usePost(card.target?.status_id)
  const noun = card.type === 'reply' ? 'reply' : card.type === 'quote' ? 'quote' : 'post'
  const words = composer.trim()
  const unchecked = !words && card.verify.length > 0
  const weight = weightedLength(composer)
  const label = primaryLabel(card.type, card.draft, composer, origin)

  const preview = useMemo(
    () =>
      card.draft
        ? draftTweet({
            account,
            text: card.draft,
            replyTo: card.type === 'reply' && card.target ? { status_id: card.target.status_id, author: card.target.author } : null,
            quoted: card.type === 'quote' ? target.tweet : null,
            createdAt: new Date().toISOString(),
          })
        : null,
    [account, card.draft, card.type, card.target, target.tweet],
  )

  async function send(d: Decision) {
    setError(null)
    let body: ReviewBody
    try {
      body = reviewBody(d)
    } catch (e) {
      if (e instanceof VerdictError) return setError(e.message)
      throw e
    }
    setBusy(true)
    try {
      await api.review(card.id, body)
      onDecided({ card, body, text: body.text ?? card.draft ?? '' })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'The server did not answer. Is `harness review` still running?')
      setBusy(false)
    }
  }

  const approve = () => send({ kind: 'approve', draft: card.draft, composer, origin })
  const openKill = () => {
    setKilling(true)
    requestAnimationFrame(() => firstChip.current?.focus())
  }
  const edit = () => {
    setOrigin('edit')
    setComposer(card.draft ?? '')
    requestAnimationFrame(() => {
      box.current?.focus()
      box.current?.setSelectionRange(box.current.value.length, box.current.value.length)
    })
  }

  // A command acts once, on the card that was showing when it was given. Undo remounts a card while the
  // queue still holds the command that decided it; replaying that would approve the card again.
  const handled = useRef(command?.seq)
  useEffect(() => {
    if (!command || command.seq === handled.current) return
    handled.current = command.seq
    if (command.name === 'approve' && !killing) (unchecked ? edit : approve)()
    else if (command.name === 'edit' && card.draft && !words) edit()
    else if (command.name === 'kill') openKill()
    else if (command.name === 'reply') box.current?.focus()
    // Only a new command should act, not a re-render with the same one.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [command?.seq])

  useAutosize(box, composer)

  const fresh = card.source === 'hot' ? freshness(target.tweet?.created_at ?? card.at) : null
  const replies = target.tweet?.conversation_count
  const note = missingNote(target.missing)
  const footer = useMemo(
    () =>
      target.tweet ? (
        <>
          <a className="age" data-fresh={fresh ?? undefined} href={card.target?.url ?? undefined} target="_blank" rel="noreferrer">
            {ago(target.tweet.created_at || card.at)}
          </a>
          {card.type === 'reply' && typeof replies === 'number' ? <span>{replies === 1 ? '1 reply' : `${replies.toLocaleString()} replies`}</span> : null}
          {note ? <span className="missing">{note}</span> : null}
        </>
      ) : null,
    [target.tweet, fresh, card.target?.url, card.at, card.type, replies, note],
  )
  const killReasons = card.draft ? Object.keys(reasons) : SKIP_REASONS
  const over = weight > MAX_WEIGHTED_CHARS

  return (
    <article className="card" tabIndex={-1} data-card={card.id} aria-labelledby={`${id}-title`} data-hot={card.source === 'hot' ? '' : undefined}>
      <h2 id={`${id}-title`} className="sr-only">
        {card.target ? `${noun} to @${card.target.author ?? 'a post'}` : `A ${noun}`}
      </h2>

      {card.target ? (
        <div className="node node-target" data-down="">
          {target.tweet ? (
            <Post tweet={target.tweet} footer={footer} />
          ) : target.error ? (
            <p className="problem">{target.error}</p>
          ) : (
            <PostSkeleton />
          )}
        </div>
      ) : card.context ? (
        <p className="story">{card.context}</p>
      ) : null}

      <div className="node node-composer" data-up={card.target ? '' : undefined} data-down={card.draft ? '' : undefined}>
        {/* The empty avatar becomes the account's once there are words to post as it. */}
        <span className="slot-avatar" aria-hidden="true" data-filled={words ? '' : undefined}>
          <img src={userOf(account).profile_image_url_https} alt="" />
        </span>
        <label className="composer-label" htmlFor={`${id}-box`}>
          {card.arm === 'human' ? 'Your one-liner' : `Your ${noun}`}
        </label>
        <textarea
          id={`${id}-box`}
          ref={box}
          className="composer"
          rows={1}
          value={composer}
          placeholder="Write it yourself"
          onChange={(e) => {
            setComposer(e.target.value)
            if (!e.target.value) setOrigin('take')
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault()
              approve()
            } else if (e.key === 'Escape') {
              e.currentTarget.blur()
            }
          }}
          aria-describedby={over || MAX_WEIGHTED_CHARS - weight <= 20 ? `${id}-count` : undefined}
        />
        {/* X shows the count only near the limit, and so does this. */}
        {MAX_WEIGHTED_CHARS - weight <= 20 ? (
          <p id={`${id}-count`} className="count" data-over={over ? '' : undefined}>
            {over ? `${weight - MAX_WEIGHTED_CHARS} over X's limit` : `${MAX_WEIGHTED_CHARS - weight} left`}
          </p>
        ) : null}
      </div>

      {preview ? (
        <div className="node node-draft" data-up="" data-unused={words ? '' : undefined}>
          <p className="caption">{`${ARM_LABELS[card.method ?? ''] ?? 'Our'} draft`}</p>
          <Post tweet={preview} linkTo={`https://x.com/${account.handle}`} showReplyTo={card.type === 'reply'} highlight={card.verify} />
        </div>
      ) : null}

      {killing ? (
        <fieldset className="kill" disabled={busy}>
          <legend>{card.draft ? 'Why kill it?' : 'Why skip it?'}</legend>
          <div className="chips">
            {killReasons.map((code, i) => (
              <button
                key={code}
                ref={i === 0 ? firstChip : undefined}
                type="button"
                className="chip"
                aria-pressed={picked.includes(code)}
                onClick={() => setPicked((p) => (p.includes(code) ? p.filter((c) => c !== code) : [...p, code]))}
              >
                {reasons[code] ?? code}
              </button>
            ))}
          </div>
          <label className="sr-only" htmlFor={`${id}-note`}>
            Note
          </label>
          <input id={`${id}-note`} className="note" value={killNote} onChange={(e) => setKillNote(e.target.value)} placeholder="Note (optional)" />
          <div className="actions">
            <button type="button" className="button danger" onClick={() => send({ kind: 'kill', reasons: picked, note: killNote })}>
              {card.draft ? 'Kill draft' : `Skip ${noun}`}
            </button>
            <button type="button" className="button quiet" onClick={() => (setKilling(false), setError(null), requestAnimationFrame(() => killButton.current?.focus()))}>
              Cancel
            </button>
          </div>
        </fieldset>
      ) : (
        <div className="actions">
          {/* A draft with an unchecked fact can't be approved as it stands, so editing it is the main action. */}
          <button
            type="button"
            className="button primary"
            onClick={unchecked ? edit : approve}
            disabled={busy || over || (!words && !card.draft)}
          >
            {unchecked ? 'Edit to check the fact' : label}
          </button>
          {card.draft && !words && !unchecked ? (
            <button type="button" className="button quiet" onClick={edit}>
              Edit draft
            </button>
          ) : null}
          <button type="button" ref={killButton} className="button quiet" onClick={openKill}>
            {card.draft ? 'Kill' : 'Skip'}
          </button>
        </div>
      )}
      {error ? (
        <p className="problem" role="alert">
          {error}
        </p>
      ) : null}
    </article>
  )
}

/** Grows the reply box with its text, so nothing a person wrote scrolls out of sight. */
function useAutosize(ref: React.RefObject<HTMLTextAreaElement | null>, value: string) {
  useEffect(() => {
    const el = ref.current
    if (!el || CSS.supports('field-sizing', 'content')) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [ref, value])
}
