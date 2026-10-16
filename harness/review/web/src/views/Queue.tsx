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
  const lastDecided = useRef<string[]>([])
  const list = useRef<HTMLDivElement>(null)

  function decide(v: Verdict) {
    setCommand(null)
    lastDecided.current.push(v.card.id)
    transition(() => setDecided((m) => new Map(m).set(v.card.id, v)))
  }

  async function undo(id: string) {
    await api.review(id, { status: 'pending', text: null, note: '', reasons: [] })
    lastDecided.current = lastDecided.current.filter((x) => x !== id)
    transition(() =>
      setDecided((m) => {
        const next = new Map(m)
        next.delete(id)
        return next
      }),
    )
  }

  const open = shown.filter((c) => !decided.has(c.id))

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement
      if (e.metaKey || e.ctrlKey || e.altKey || el.closest('input, textarea, [contenteditable], dialog')) return
      const cards = Array.from(list.current?.querySelectorAll<HTMLElement>('article.card') ?? [])
      if (e.key === 'j' || e.key === 'k') {
        e.preventDefault()
        const next = Math.max(0, Math.min(cards.length - 1, focus + (e.key === 'j' ? 1 : -1)))
        setFocus(next)
        cards[next]?.focus({ preventScroll: true })
        cards[next]?.scrollIntoView({ block: 'start' })
        return
      }
      const names: Record<string, string> = { a: 'approve', e: 'edit', x: 'kill', r: 'reply' }
      if (names[e.key] && open[focus]) {
        e.preventDefault()
        setCommand((c) => ({ id: open[focus].id, name: names[e.key], seq: (c?.seq ?? 0) + 1 }))
      } else if (e.key === 'u' && lastDecided.current.length) {
        e.preventDefault()
        void undo(lastDecided.current[lastDecided.current.length - 1])
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  if (!queue && !error) return <p className="loading">Loading the queue</p>

  return (
    <div ref={list} className="feed">
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
        <button type="button" className="arrivals" onClick={showIncoming}>
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

function Receipt({ verdict, onUndo }: { verdict: Verdict; onUndo: () => void }) {
  const { card, body, text } = verdict
  const [copied, setCopied] = useState(false)
  const to = card.target?.author ? ` to @${card.target.author}` : ''
  const what = card.type === 'reply' ? 'reply' : card.type === 'quote' ? 'quote' : 'post'
  const said =
    body.status === 'approved'
      ? `Approved ${body.text && body.note ? 'your' : 'the'} ${what}${to}.`
      : card.draft
        ? `Killed the ${what}${to}.`
        : `Skipped ${card.target?.author ? `@${card.target.author}'s post` : 'the post'}.`
  return (
    <div className="receipt" role="status">
      <p>{said}</p>
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
