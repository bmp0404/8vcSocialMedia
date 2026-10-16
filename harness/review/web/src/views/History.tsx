// Past verdicts, newest first, each with what happened on X beside it once `harness outcomes sync` knows.

import { useEffect, useState } from 'react'
import { api, type HistoryItem, type Outcome } from '../lib/api'
import { ago, ordinal } from '../lib/time'

export function History({ reasons }: { reasons: Record<string, string> }) {
  const [data, setData] = useState<{ items: HistoryItem[]; ledger: boolean } | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    api.history().then(setData, (e: Error) => setError(e.message))
  }, [])

  if (error)
    return (
      <p className="problem" role="alert">
        {error}
      </p>
    )
  if (!data) return <p className="loading">Loading past verdicts</p>
  if (!data.items.length)
    return (
      <div className="empty">
        <p className="empty-title">No verdicts yet.</p>
        <p>Approve or kill a draft in the queue and it shows up here, with how it did on X once it has been checked.</p>
      </div>
    )
  const anyOutcome = data.items.some((i) => i.outcome)
  return (
    <div className="history">
      {!anyOutcome ? (
        <p className="hint">
          No outcomes yet. Each approved post gets its rank and likes here after <code>harness outcomes sync</code> finds it on X.
        </p>
      ) : null}
      <ol className="verdicts">
        {data.items.map((item) => (
          <li key={item.id} className="verdict">
            <div className="verdict-main">
              <p className="verdict-head">
                <span data-status={item.status}>{item.status === 'approved' ? 'Approved' : 'Killed'}</span>{' '}
                {item.target?.author ? (
                  <>
                    {item.type === 'quote' ? 'quote of' : 'reply to'}{' '}
                    {item.target.url ? (
                      <a href={item.target.url} target="_blank" rel="noreferrer">
                        @{item.target.author}
                      </a>
                    ) : (
                      `@${item.target.author}`
                    )}
                  </>
                ) : (
                  item.type
                )}{' '}
                <time dateTime={item.reviewed_at}>{ago(item.reviewed_at)}</time>
              </p>
              <p className="verdict-text">{item.text || 'No text'}</p>
              {item.status === 'killed' && item.reasons.length ? (
                <p className="verdict-why">{item.reasons.map((r) => reasons[r] ?? r).join(', ')}</p>
              ) : null}
            </div>
            {item.status === 'approved' && item.outcome ? <OutcomeView outcome={item.outcome} reply={item.type === 'reply'} /> : null}
          </li>
        ))}
      </ol>
    </div>
  )
}

function OutcomeView({ outcome: o, reply }: { outcome: Outcome; reply: boolean }) {
  if (o.state !== 'posted') {
    const said = {
      not_posted: 'Not posted',
      waiting: 'Not on X yet',
      unverified: 'Not found on X yet',
      untracked: 'Not tracked',
    }[o.state]
    return <div className="outcome muted">{said}</div>
  }
  return (
    <div className="outcome">
      {reply && o.thread_replies ? (
        <p className="outcome-rank">
          {o.rank ? `${ordinal(o.rank)} of ${o.thread_replies.toLocaleString()} replies` : `Below the top ${o.page_replies} of ${o.thread_replies.toLocaleString()}`}
        </p>
      ) : null}
      {o.likes !== null ? <p>{o.likes === 1 ? '1 like' : `${o.likes.toLocaleString()} likes`}</p> : null}
      {o.checked_at ? (
        <p className="muted">
          {o.url ? (
            <a href={o.url} target="_blank" rel="noreferrer">
              Checked {ago(o.checked_at)}
            </a>
          ) : (
            `Checked ${ago(o.checked_at)}`
          )}
        </p>
      ) : null}
    </div>
  )
}
