// The review server's routes (harness/review/*.py) and the shapes they return.

import type { Tweet } from 'react-tweet/api'
import type { Schema, Value } from './schema'
import type { Account, StoredPost } from './tweet'
import type { Lane, ReviewBody } from './verdict'

export type Target = { status_id: string; url: string | null; author: string | null }

export type Card = {
  id: string
  source: 'hot' | 'run'
  run_id?: number
  type: Lane
  arm: 'continuation' | 'styled' | 'human' | null
  method: string | null
  account: string | null
  draft: string | null
  verify: string[]
  target: Target | null
  context: string | null
  at: string
}

export type QueueData = { cards: Card[]; problems: string[]; reasons: Record<string, string>; limit: number }

export type PostData = { data: Tweet | null; stored: StoredPost | null; missing: null | 'not_found' | 'tombstone' | 'unreachable' }

export type Outcome = {
  state: 'posted' | 'not_posted' | 'waiting' | 'unverified' | 'untracked'
  url: string | null
  posted_at: string | null
  rank: number | null
  page_replies: number | null
  thread_replies: number | null
  likes: number | null
  checked_at: string | null
}

export type HistoryItem = {
  id: string
  type: Lane | 'follow'
  status: 'approved' | 'killed'
  text: string
  edited: boolean
  target: Target | null
  reasons: string[]
  note: string
  reviewed_at: string
  outcome: Outcome | null
}

export type AuditPost = {
  item_id: string
  kind: string
  status_id: string
  url: string
  author: string
  author_followers: number | null
  text: string
  created_at: string
  parent_id: string | null
}

export type AuditData = { schema: Schema; item: AuditPost | null; parent: AuditPost | null; remaining: number; total: number }

export type Consensus = Record<
  string,
  { value: Value | null; agreement: number | null; votes: Record<string, Value>; human: { value: Value; labeler: string; at: string } | null }
>

export type Status = { last_round: { id: number; started_at: string; provider: string } | null; watcher: 'running' | 'stopped' | 'unknown'; account: string | null }

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

async function call<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, body === undefined ? undefined : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const json = await res.json().catch(() => ({ error: `${res.status} from ${path}` }))
  if (!res.ok) throw new ApiError(json.error ?? `${res.status} from ${path}`, res.status)
  return json as T
}

export const api = {
  queue: () => call<QueueData>('/api/queue'),
  review: (id: string, body: ReviewBody) => call<{ ok: true; export: string }>(`/api/actions/${encodeURIComponent(id)}/review`, body),
  post: (statusId: string) => call<PostData>(`/api/tweet/${statusId}`),
  account: (handle: string) => call<Account>(`/api/account/${encodeURIComponent(handle)}`),
  history: () => call<{ items: HistoryItem[]; ledger: boolean }>('/api/history'),
  audit: (offset: number) => call<AuditData>(`/api/audit?offset=${offset}`),
  answer: (itemId: string, labels: Record<string, Value>, note: string) =>
    call<{ ok: true; consensus: Consensus }>(`/api/audit/${encodeURIComponent(itemId)}`, { labels, note }),
  status: () => call<Status>('/api/status'),
}
