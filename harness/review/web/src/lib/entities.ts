// A post's text in the form X's syndication answer uses, so react-tweet's TweetBody renders it the way it
// renders X's own posts. X sends the text with &, < and > escaped, and TweetBody writes the plain-text
// stretches as HTML, so text that isn't escaped here would run as markup: a draft or a stored post that
// contains "<img onerror=...>" would execute in this page. Entity indices count code points of the
// escaped text, as X's do (react-tweet slices `Array.from(text)`).

import type { TweetEntities } from 'react-tweet/api'

// twitter-text ends a link before trailing punctuation; harness/xtext.py uses the same set.
const TRAILING = '.,;:!?)]}\'"'
// A link ends at whitespace or at an escaped < or >, as it would before the raw character.
const LINK = /https?:\/\/(?:(?!&lt;|&gt;)\S)+/g
const MENTION = /(^|[^\w@])@(\w{1,15})(?!\w)/g
const HASHTAG = /(^|[^\w&#])#(\w*[A-Za-z_]\w*)/g

/** The code-point index of UTF-16 offset `i` in `text`. */
function cp(text: string, i: number): number {
  return Array.from(text.slice(0, i)).length
}

export function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function unescapeHtml(text: string): string {
  return text.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')
}

function trimLink(link: string): string {
  let end = link.length
  while (end > 0 && TRAILING.includes(link[end - 1])) end--
  return link.slice(0, end)
}

/** Entities of `text`, which must already be escaped (escapeHtml). Links keep their real characters,
 * because react-tweet uses them as the href and as plain React text. */
export function entitiesOf(text: string): TweetEntities {
  const urls: TweetEntities['urls'] = []
  const taken: [number, number][] = [] // UTF-16 spans already claimed by a link
  for (const m of text.matchAll(LINK)) {
    const link = trimLink(m[0])
    const start = m.index ?? 0
    taken.push([start, start + link.length])
    const real = unescapeHtml(link)
    urls.push({
      url: real,
      expanded_url: real,
      // The whole address without its scheme: X shortens it with an ellipsis, but this page never cuts text.
      display_url: real.replace(/^https?:\/\//, ''),
      indices: [cp(text, start), cp(text, start + link.length)],
    })
  }
  const inLink = (i: number) => taken.some(([a, b]) => i >= a && i < b)
  const user_mentions: TweetEntities['user_mentions'] = []
  for (const m of text.matchAll(MENTION)) {
    const start = (m.index ?? 0) + m[1].length
    if (inLink(start)) continue
    user_mentions.push({
      id_str: '',
      name: m[2],
      screen_name: m[2],
      indices: [cp(text, start), cp(text, start + 1 + m[2].length)],
    })
  }
  const hashtags: TweetEntities['hashtags'] = []
  for (const m of text.matchAll(HASHTAG)) {
    const start = (m.index ?? 0) + m[1].length
    if (inLink(start)) continue
    hashtags.push({ text: m[2], indices: [cp(text, start), cp(text, start + 1 + m[2].length)] })
  }
  return { hashtags, urls, user_mentions, symbols: [] }
}
