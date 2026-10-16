import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type Card, type QueueData, type Status } from './lib/api'
import { ago } from './lib/time'
import type { Verdict } from './components/QueueCard'
import { Queue } from './views/Queue'
import { History } from './views/History'
import { Audit } from './views/Audit'

const VIEWS = [
  { path: '/', name: 'Queue' },
  { path: '/history', name: 'History' },
  { path: '/audit', name: 'Audit' },
] as const
type Path = (typeof VIEWS)[number]['path']
// How often the queue checks for new hot targets. The watcher drafts within about a minute of seeing a
// post; half a minute adds little to that and costs the local server one cheap read.
const POLL_MS = 30_000

function pathOf(): Path {
  return VIEWS.find((v) => v.path === location.pathname)?.path ?? '/'
}

export function App() {
  const [path, setPath] = useState<Path>(pathOf)
  const [status, setStatus] = useState<Status | null>(null)
  const queue = useQueue()
  const sheet = useRef<HTMLDialogElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)

  useEffect(() => {
    const back = () => setPath(pathOf())
    window.addEventListener('popstate', back)
    return () => window.removeEventListener('popstate', back)
  }, [])

  useEffect(() => {
    const load = () => api.status().then(setStatus, () => setStatus(null))
    load()
    const t = setInterval(() => document.visibilityState === 'visible' && load(), POLL_MS)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement
      if (e.key !== '?' || el.closest('input, textarea, [contenteditable]')) return
      e.preventDefault()
      const d = sheet.current
      if (!d) return
      // Opened by its key it appears at once, like any keyboard command; the ? button fades it in.
      d.toggleAttribute('data-instant', true)
      if (d.open) d.close()
      else d.showModal()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  function go(to: Path) {
    if (to === path) return
    history.pushState(null, '', to)
    setPath(to)
    window.scrollTo({ top: 0 })
    // A screen reader hears the new view's name, and Tab starts from the top of it.
    requestAnimationFrame(() => heading.current?.focus({ preventScroll: true }))
  }

  const waiting = queue.shown.filter((c) => !queue.decided.has(c.id)).length
  const view = VIEWS.find((v) => v.path === path)!.name
  useEffect(() => {
    document.title = path === '/' && waiting ? `Queue, ${waiting} waiting` : view
  }, [path, view, waiting])
  return (
    <>
      <header className="top">
        <nav className="tabs" aria-label="Views">
          {VIEWS.map((v) => (
            <a
              key={v.path}
              href={v.path}
              className="tab"
              aria-current={v.path === path ? 'page' : undefined}
              onClick={(e) => {
                e.preventDefault()
                go(v.path)
              }}
            >
              {v.name}
              {v.path === '/' && waiting ? (
                <span className="tab-count">
                  {waiting}
                  <span className="sr-only"> waiting</span>
                </span>
              ) : null}
            </a>
          ))}
        </nav>
        <p className="status">
          <StatusLine status={status} />
          <button type="button" className="keys" aria-label="Keyboard shortcuts" onClick={() => (sheet.current?.removeAttribute('data-instant'), sheet.current?.showModal())}>
            ?
          </button>
        </p>
      </header>
      <main className="column">
        <h1 ref={heading} tabIndex={-1} className="sr-only">
          {view}
        </h1>
        {path === '/' ? (
          <Queue
            queue={queue.data}
            shown={queue.shown}
            incoming={queue.incoming}
            showIncoming={queue.showIncoming}
            decided={queue.decided}
            setDecided={queue.setDecided}
            status={status}
            error={queue.error}
          />
        ) : path === '/history' ? (
          <History reasons={queue.data?.reasons ?? {}} />
        ) : (
          <Audit />
        )}
      </main>
      <Shortcuts ref={sheet} />
    </>
  )
}

function StatusLine({ status }: { status: Status | null }) {
  if (!status) return <span>Checking the harness</span>
  const round = status.last_round ? `Last round ${ago(status.last_round.started_at)}.` : 'No rounds yet.'
  const watcher = { running: 'Watcher on.', stopped: 'Watcher off.', unknown: '' }[status.watcher]
  return (
    <span>
      {round} {watcher}
    </span>
  )
}

/** The queue as the person sees it. New cards wait behind a "Show" button instead of pushing the card
 * someone is typing in down the page; an empty queue takes them at once. */
function useQueue() {
  const [data, setData] = useState<QueueData | null>(null)
  const [shown, setShown] = useState<Card[]>([])
  const [incoming, setIncoming] = useState<Card[]>([])
  const [decided, setDecided] = useState(new Map<string, Verdict>())
  const [error, setError] = useState<string | null>(null)
  const shownRef = useRef<Card[]>([])
  shownRef.current = shown
  const decidedRef = useRef(decided)
  decidedRef.current = decided

  const load = useCallback(async () => {
    try {
      const d = await api.queue()
      setData(d)
      setError(null)
      const known = new Set(shownRef.current.map((c) => c.id))
      const fresh = d.cards.filter((c) => !known.has(c.id))
      const stillThere = new Set(d.cards.map((c) => c.id))
      // A card decided on this page stays as its receipt; one decided elsewhere leaves.
      const kept = shownRef.current.filter((c) => stillThere.has(c.id) || decidedRef.current.has(c.id))
      if (kept.every((c) => decidedRef.current.has(c.id))) {
        setShown(withArrivals(kept, fresh)) // nobody is mid-card, so new cards can't push anything away
        setIncoming([])
      } else {
        setShown(kept)
        setIncoming(fresh)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void load()
    const t = setInterval(() => document.visibilityState === 'visible' && void load(), POLL_MS)
    return () => clearInterval(t)
  }, [load])

  const showIncoming = () => {
    setShown((s) => withArrivals(s, incoming))
    setIncoming([])
    window.scrollTo({ top: 0 })
  }
  return { data, shown, incoming, showIncoming, decided, setDecided, error }
}

/** New hot targets go on top, as the queue orders them; new round drafts go after what is there. */
function withArrivals(shown: Card[], arrivals: Card[]): Card[] {
  return [...arrivals.filter((c) => c.source === 'hot'), ...shown, ...arrivals.filter((c) => c.source !== 'hot')]
}

const SHORTCUTS: [string, string][] = [
  ['j / k', 'Next or previous card'],
  ['r', 'Write your own reply'],
  ['a', 'Approve'],
  ['e', 'Edit the draft'],
  ['x', 'Kill or skip'],
  ['u', 'Undo the last verdict'],
  ['Cmd or Ctrl + Enter', 'Approve while typing'],
  ['Esc', 'Leave the reply box'],
  ['?', 'Show or hide this sheet'],
]

function Shortcuts({ ref }: { ref: React.Ref<HTMLDialogElement> }) {
  return (
    <dialog ref={ref} className="sheet" aria-labelledby="keys-title" onClick={(e) => e.target === e.currentTarget && e.currentTarget.close()}>
      <h2 id="keys-title">Keyboard shortcuts</h2>
      <dl>
        {SHORTCUTS.map(([k, what]) => (
          <div key={k}>
            <dt>
              <kbd>{k}</kbd>
            </dt>
            <dd>{what}</dd>
          </div>
        ))}
      </dl>
      <form method="dialog">
        <button className="button quiet">Close</button>
      </form>
    </dialog>
  )
}
