// The audit set, one item at a time: the post as X shows it, the schema's questions, and the models'
// labels only after the person has answered, so they can't anchor on them.

import { useEffect, useId, useRef, useState } from 'react'
import { api, ApiError, type AuditData, type AuditPost, type Consensus } from '../lib/api'
import { controlsFor, labelsPayload, optionLabel, type Control, type Value } from '../lib/schema'
import { date } from '../lib/time'
import { Post, PostSkeleton, usePost } from '../components/Post'

export function Audit() {
  const [offset, setOffset] = useState(0)
  const [loads, setLoads] = useState(0) // a saved item leaves the queue, so the same offset reloads to the next one
  const [data, setData] = useState<AuditData | null>(null)
  const [error, setError] = useState<{ message: string; status: number } | null>(null)
  const [answers, setAnswers] = useState<Record<string, Value | undefined>>({})
  const [note, setNote] = useState('')
  const [revealed, setRevealed] = useState<Consensus | null>(null)
  const [busy, setBusy] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const shownItem = useRef<string | null>(null)
  shownItem.current = data?.item?.item_id ?? null

  useEffect(() => {
    let live = true
    setData(null)
    api.audit(offset).then(
      (d) => live && (setData(d), setError(null)),
      (e: ApiError) => live && setError({ message: e.message, status: e.status }),
    )
    return () => {
      live = false
    }
  }, [offset, loads])

  if (error?.status === 503)
    return (
      <div className="empty">
        <p className="empty-title">Nothing to audit here yet.</p>
        <p>{error.message}</p>
      </div>
    )
  if (error)
    return (
      <p className="problem" role="alert">
        {error.message}
      </p>
    )
  if (!data) return <p className="loading">Loading the next item</p>
  if (!data.item)
    return (
      <div className="empty">
        <p className="empty-title">{data.remaining ? 'You skipped the rest.' : `All ${data.total} audit items are labeled.`}</p>
        {data.remaining ? (
          <button type="button" className="button quiet" onClick={() => setOffset(0)}>
            Start again from the first skipped item
          </button>
        ) : null}
      </div>
    )

  const item = data.item
  const controls = controlsFor(data.schema, item.kind)
  const answered = Object.values(answers).some((v) => v !== undefined && String(v).trim() !== '')

  async function save() {
    setSaveError(null)
    setBusy(true)
    try {
      const res = await api.answer(item.item_id, labelsPayload(controls, answers), note.trim())
      // Only reveal votes beside the item they belong to.
      if (shownItem.current === item.item_id) setRevealed(res.consensus)
    } catch (e) {
      if (shownItem.current === item.item_id) setSaveError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function next(skip: boolean) {
    setAnswers({})
    setNote('')
    setRevealed(null)
    setSaveError(null)
    if (skip) setOffset((o) => o + 1)
    else setLoads((n) => n + 1)
    window.scrollTo({ top: 0 })
  }

  const position = data.total - data.remaining + offset + 1
  return (
    <div className="audit">
      <p className="progress">
        {position} of {data.total}
      </p>
      <div className="card">
        {data.parent ? (
          <div className="node node-target" data-down="">
            <AuditPostView post={data.parent} />
          </div>
        ) : null}
        <div className="node" data-up={data.parent ? '' : undefined}>
          <AuditPostView post={item} showReplyTo={!data.parent} />
          <p className="facts">
            Posted {date(item.created_at)}
            {item.author_followers !== null ? `, to ${item.author_followers.toLocaleString()} followers` : ''}.
          </p>
        </div>
      </div>

      <form
        className="questions"
        onSubmit={(e) => {
          e.preventDefault()
          if (revealed) next(false)
          else if (answered) void save()
        }}
      >
        {controls.map((c) => (
          <Question
            key={`${item.item_id}-${c.name}`}
            control={c}
            value={answers[c.name]}
            onChange={(v) => setAnswers((a) => ({ ...a, [c.name]: v }))}
            disabled={!!revealed || busy}
            result={revealed?.[c.name]}
          />
        ))}
        {!revealed ? (
          <>
            <label className="field">
              <span className="field-label">Note</span>
              <input className="note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional" />
            </label>
            <div className="actions">
              <button type="submit" className="button primary" disabled={!answered || busy}>
                Save answers
              </button>
              <button type="button" className="button quiet" onClick={() => next(true)} disabled={busy}>
                Skip
              </button>
            </div>
          </>
        ) : (
          <div className="actions">
            <button type="submit" className="button primary" autoFocus>
              Next item
            </button>
          </div>
        )}
        {saveError ? (
          <p className="problem" role="alert">
            {saveError}
          </p>
        ) : null}
      </form>
    </div>
  )
}

function AuditPostView({ post, showReplyTo = false }: { post: AuditPost; showReplyTo?: boolean }) {
  const { tweet, error } = usePost(post.status_id)
  if (error) return <p className="problem">{error}</p>
  if (!tweet) return <PostSkeleton />
  return <Post tweet={tweet} showReplyTo={showReplyTo} />
}

type QuestionProps = {
  control: Control
  value: Value | undefined
  onChange: (v: Value | undefined) => void
  disabled: boolean
  result?: Consensus[string]
}

function Question({ control: c, value, onChange, disabled, result }: QuestionProps) {
  const id = useId()
  return (
    <fieldset className="question" disabled={disabled}>
      <legend>{c.question}</legend>
      {c.kind === 'text' ? (
        <textarea
          aria-label={c.question}
          className="composer"
          rows={2}
          value={(value as string) ?? ''}
          onChange={(e) => onChange(e.target.value || undefined)}
        />
      ) : (
        <div className="options" data-scale={c.kind === 'ordinal' ? '' : undefined}>
          {c.options.map((o) => (
            <label key={String(o.value)} className="option">
              <input
                type="radio"
                name={id}
                checked={value === o.value}
                onChange={() => onChange(o.value)}
                onClick={() => value === o.value && onChange(undefined)}
              />
              <span>{o.label}</span>
            </label>
          ))}
        </div>
      )}
      {result ? <Votes result={result} mine={value} /> : null}
    </fieldset>
  )
}

/** The models' labels for one field. The person's own answer is the selected option just above, so it isn't
 * repeated; a model that agrees with it says "Same". */
function Votes({ result, mine }: { result: Consensus[string]; mine: Value | undefined }) {
  const models = Object.entries(result.votes)
  if (!models.length) return <p className="votes-none">No model labeled this.</p>
  return (
    <dl className="votes">
      {models.map(([model, v]) => (
        <div key={model} data-differs={mine !== undefined && v !== mine ? '' : undefined}>
          <dt>{model}</dt>
          <dd>{mine !== undefined && v === mine ? 'Same' : optionLabel(v)}</dd>
        </div>
      ))}
    </dl>
  )
}
