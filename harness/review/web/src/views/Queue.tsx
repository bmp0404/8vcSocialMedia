import { useEffect, useRef, useState } from 'react'
import { api, type Card, type QueueData, type Status } from '../lib/api'
import type { Account } from '../lib/tweet'
import { QueueCard, type Verdict } from '../components/QueueCard'
import { useAccounts } from '../lib/accounts'
import { transition, transitionName } from '../lib/motion'

type Props = {
  queue: QueueData | null
  shown: Card[]
  incoming: Card[]
  showIncoming: () => void
  decided: Map<string, Verdict>
  setDecided: (fn: (m: Map<string, Verdict>) => Map<string, Verdict>) => void
  status: Status | null
  error: string | null
}

export function Queue({ queue, shown, incoming, showIncoming, decided, setDecided, status, error }: Props) {
  const accounts = useAccounts(shown.map((c) => c.account ?? status?.account ?? null))
  const [focus, setFocus] = useState(0)
  const [command, setCommand] = useState<{ id: string; name: string; seq: number } | null>(null)
  // One live region that stays mounted, so each verdict is read out; a region mounted with its text isn't.
  const [said, setSaid] = useState('')
  const lastDecided = useRef<string[]>([])
  const byKey = useRef(false) // the verdict in flight came from the keyboard, so it doesn't animate
  const focusNext = useRef<string | null>(null) // the card to focus once the list has re-rendered
  const list = useRef<HTMLDivElement>(null)

  const open = shown.filter((c) => !decided.has(c.id))

  function decide(v: Verdict) {
    setCommand(null)
    lastDecided.current.push(v.card.id)
    const at = open.findIndex((c) => c.id === v.card.id)
    focusNext.current = (open[at + 1] ?? open[at - 1])?.id ?? null
    setSaid(receiptText(v))
    transition(() => setDecided((m) => new Map(m).set(v.card.id, v)), !byKey.current)
    byKey.current = false
  }

  async function undo(id: string, viaKey = false) {
    await api.review(id, { status: 'pending', text: null, note: '', reasons: [] })
    lastDecided.current = lastDecided.current.filter((x) => x !== id)
    focusNext.current = id
    setSaid('Undone. The card is back in the queue.')
    transition(
      () =>
        setDecided((m) => {
          const next = new Map(m)
          next.delete(id)
          return next
        }),
      !viaKey,
    )
  }

  // After a verdict, focus moves to the next card instead of falling to the page; after Undo or Show, to
  // the card that came back or arrived.
  useEffect(() => {
    if (!focusNext.current) return
    const el = list.current?.querySelector<HTMLElement>(`article.card[data-card="${CSS.escape(focusNext.current)}"]`)
    focusNext.current = null
    el?.focus({ preventScroll: true })
  }, [decided, shown])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement
      if (e.metaKey || e.ctrlKey || e.altKey || el.closest('input, textarea, [contenteditable], dialog')) return
      const cards = Array.from(list.current?.querySelectorAll<HTMLElement>('article.card') ?? [])
      const inside = !!list.current?.contains(document.activeElement)
      const current = cards.findIndex((c) => c.contains(document.activeElement))
      if (e.key === 'j' || e.key === 'k') {
        e.preventDefault()
        // From outside the queue, j and k go to the card last in focus instead of moving past it.
        const from = current >= 0 ? current : focus
        const next = Math.max(0, Math.min(cards.length - 1, from + (inside ? (e.key === 'j' ? 1 : -1) : 0)))
        setFocus(next)
        cards[next]?.focus({ preventScroll: true })
        cards[next]?.scrollIntoView({ block: 'start' })
        return
      }
      // Letters that act on a card work only while focus is in the queue (WCAG 2.1.4), so a stray key or a
      // spoken word elsewhere on the page can't approve anything.
      if (!inside) return
      const card = open[current >= 0 ? current : focus]
      const names: Record<string, string> = { a: 'approve', e: 'edit', x: 'kill', r: 'reply' }
      if (names[e.key] && card) {
        e.preventDefault()
        byKey.current = e.key === 'a'
        setCommand((c) => ({ id: card.id, name: names[e.key], seq: (c?.seq ?? 0) + 1 }))
      } else if (e.key === 'u' && lastDecided.current.length) {
        e.preventDefault()
        void undo(lastDecided.current[lastDecided.current.length - 1], true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  if (!queue && !error) return <p className="loading">Loading the queue</p>

  return (
    <div ref={list} className="feed">
      <p className="sr-only" role="status">
        {said}
      </p>
      {error ? (
        <p className="problem" role="alert">
          {error}
        </p>
      ) : null}
      {queue?.problems.map((p) => (
        <p key={p} className="problem">
          {p}
        </p>
      ))}
      {incoming.length ? (
        <button
          type="button"
          className="arrivals"
          onClick={() => {
            focusNext.current = incoming[0].id
            showIncoming()
          }}
        >
          {arrivalsLabel(incoming)}
        </button>
      ) : null}
      {shown.map((card) => {
        const v = decided.get(card.id)
        const account = accountFor(accounts, card, status)
        return (
          <div key={card.id} className="slot" style={{ viewTransitionName: transitionName(card.id) }}>
            {v ? (
              <Receipt verdict={v} onUndo={() => undo(card.id)} />
            ) : (
              <QueueCard
                card={card}
                account={account}
                reasons={queue?.reasons ?? {}}
                onDecided={decide}
                command={command?.id === card.id ? command : null}
              />
            )}
          </div>
        )
      })}
      {open.length === 0 ? <EmptyQueue status={status} /> : null}
    </div>
  )
}

function accountFor(accounts: Map<string, Account>, card: Card, status: Status | null): Account {
  const handle = card.account ?? status?.account ?? 'account'
  return accounts.get(handle) ?? { handle, name: null, avatar_url: null, verified: false }
}

function arrivalsLabel(cards: Card[]): string {
  const hot = cards.filter((c) => c.source === 'hot').length
  if (hot === cards.length) return hot === 1 ? 'Show 1 new hot target' : `Show ${hot} new hot targets`
  return cards.length === 1 ? 'Show 1 new draft' : `Show ${cards.length} new drafts`
}

function EmptyQueue({ status }: { status: Status | null }) {
  return (
    <div className="empty">
      <p className="empty-title">Nothing to decide right now.</p>
      <p>
        {status?.watcher === 'running'
          ? 'The watcher is running. A hot target appears here as soon as it catches a fresh post.'
          : (
            <>
              The watcher is off, so no hot targets will arrive. Start it with <code>harness watch</code>, or run a round.
            </>
          )}
      </p>
    </div>
  )
}

function receiptText({ card, body }: Verdict): string {
  const to = card.target?.author ? ` to @${card.target.author}` : ''
  const what = card.type === 'reply' ? 'reply' : card.type === 'quote' ? 'quote' : 'post'
  if (body.status === 'approved') return `Approved ${body.text && body.note ? 'your' : 'the'} ${what}${to}.`
  if (card.draft) return `Killed the ${what}${to}.`
  return `Skipped ${card.target?.author ? `@${card.target.author}'s post` : 'the post'}.`
}

function Receipt({ verdict, onUndo }: { verdict: Verdict; onUndo: () => void }) {
  const { card, body, text } = verdict
  const [copied, setCopied] = useState(false)
  return (
    <div className="receipt">
      <p>{receiptText(verdict)}</p>
      <div className="receipt-actions">
        {body.status === 'approved' ? (
          <button
            type="button"
            className="button quiet small"
            onClick={() => navigator.clipboard.writeText(text).then(() => setCopied(true))}
          >
            {copied ? 'Copied' : 'Copy text'}
          </button>
        ) : null}
        {body.status === 'approved' && card.target?.url ? (
          <a className="button quiet small" href={card.target.url} target="_blank" rel="noreferrer">
            Open post on X
          </a>
        ) : null}
        <button type="button" className="button quiet small" onClick={onUndo}>
          Undo
        </button>
      </div>
    </div>
  )
}
