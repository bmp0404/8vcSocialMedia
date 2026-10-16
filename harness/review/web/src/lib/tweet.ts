// Tweets the page builds itself, so they render with the same react-tweet components as X's own:
// our draft, written as a post from the brand account, and a post X's syndication endpoint couldn't
// return, rebuilt from what the harness stored about it (GET /api/tweet/<id> sends those fields).

import type { Tweet, TweetUser } from 'react-tweet/api'
import { entitiesOf, escapeHtml } from './entities'

export type Account = { handle: string; name: string | null; avatar_url: string | null; verified: boolean }

export type StoredPost = {
  status_id: string
  author: string
  text: string
  created_at: string | null
  likes: number | null
  replies: number | null
  reposts: number | null
  quotes: number | null
  author_followers: number | null
  in_reply_to: string | null
  in_reply_to_status: string | null
}

/** A round avatar with the handle's first letter, for an account whose picture the page couldn't read. */
export function initialAvatar(handle: string): string {
  const letter = (handle.replace(/^@/, '').match(/[A-Za-z0-9]/)?.[0] ?? '?').toUpperCase()
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">` +
    `<rect width="48" height="48" fill="#8b98a5"/>` +
    `<text x="24" y="31" font-family="sans-serif" font-size="22" font-weight="700" fill="#fff" text-anchor="middle">${letter}</text>` +
    `</svg>`
  return `data:image/svg+xml,${encodeURIComponent(svg)}`
}

export function userOf(account: Account): TweetUser {
  return {
    id_str: '',
    name: account.name || account.handle,
    screen_name: account.handle,
    profile_image_url_https: account.avatar_url || initialAvatar(account.handle),
    profile_image_shape: 'Circle',
    verified: false,
    is_blue_verified: account.verified,
  }
}

type Base = { id: string; text: string; user: TweetUser; createdAt: string }

function base({ id, text: raw, user, createdAt }: Base): Tweet {
  const text = escapeHtml(raw) // see entities.ts: TweetBody writes plain text as HTML
  return {
    __typename: 'Tweet',
    lang: 'en',
    created_at: createdAt,
    display_text_range: [0, Array.from(text).length],
    entities: entitiesOf(text),
    id_str: id,
    text,
    user,
    edit_control: { edit_tweet_ids: [id], editable_until_msecs: '0', is_edit_eligible: false, edits_remaining: '0' },
    isEdited: false,
    isStaleEdit: false,
    favorite_count: 0,
    conversation_count: 0,
    news_action_type: 'conversation',
  }
}

export type DraftInput = {
  account: Account
  text: string
  /** The post the draft answers; react-tweet then shows "Replying to @author". */
  replyTo?: { status_id: string; author: string | null } | null
  /** The post a quote draft quotes, embedded the way X shows a quote. */
  quoted?: Tweet | null
  createdAt: string
}

export function draftTweet({ account, text, replyTo, quoted, createdAt }: DraftInput): Tweet {
  const t = base({ id: 'draft', text, user: userOf(account), createdAt })
  if (replyTo?.author) {
    t.in_reply_to_screen_name = replyTo.author
    t.in_reply_to_status_id_str = replyTo.status_id
    t.in_reply_to_user_id_str = ''
  }
  if (quoted) {
    t.quoted_tweet = {
      ...quoted,
      reply_count: quoted.conversation_count,
      retweet_count: 0,
      self_thread: { id_str: quoted.id_str },
    }
  }
  return t
}

export function storedTweet(post: StoredPost): Tweet {
  const author = post.author || 'unknown'
  const t = base({
    id: post.status_id,
    text: post.text,
    user: userOf({ handle: author, name: null, avatar_url: null, verified: false }),
    createdAt: post.created_at ?? '',
  })
  t.favorite_count = post.likes ?? 0
  t.conversation_count = post.replies ?? 0
  // react-tweet links "Replying to" to the parent, so it needs both the author and the status id.
  if (post.in_reply_to && post.in_reply_to_status) {
    t.in_reply_to_screen_name = post.in_reply_to
    t.in_reply_to_status_id_str = post.in_reply_to_status
    t.in_reply_to_user_id_str = ''
  }
  return t
}
