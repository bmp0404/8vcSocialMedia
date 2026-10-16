import { describe, expect, it } from 'vitest'
import { entitiesOf, escapeHtml } from './entities'
import { draftTweet, initialAvatar, storedTweet, type Account, type StoredPost } from './tweet'

const SGU: Account = { handle: '_sgu_', name: 'Sam', avatar_url: 'https://pbs.twimg.com/a.jpg', verified: false }
const AT = '2026-10-16T00:12:00.000Z'

describe('entitiesOf', () => {
  it('finds mentions, hashtags and links at code-point indices', () => {
    const text = '🔥 @AtlantaDream #WNBAFinals https://espn.com/a.'
    const e = entitiesOf(text)
    const chars = Array.from(text)
    const slice = ([a, b]: [number, number]) => chars.slice(a, b).join('')
    expect(e.user_mentions.map((m) => [m.screen_name, slice(m.indices)])).toEqual([['AtlantaDream', '@AtlantaDream']])
    expect(e.hashtags.map((h) => [h.text, slice(h.indices)])).toEqual([['WNBAFinals', '#WNBAFinals']])
    expect(e.urls.map((u) => [u.url, u.display_url, slice(u.indices)])).toEqual([
      ['https://espn.com/a', 'espn.com/a', 'https://espn.com/a'],
    ])
    expect(e.symbols).toEqual([])
  })

  it('leaves out an @ inside an email, a # inside a link and a number-only hashtag', () => {
    const e = entitiesOf('mail me@x.com, see https://x.com/a#top and #1')
    expect(e.user_mentions).toEqual([])
    expect(e.hashtags).toEqual([])
    expect(e.urls.map((u) => u.url)).toEqual(['https://x.com/a#top'])
  })

  it('indexes the escaped text and keeps a link\'s real characters', () => {
    const text = escapeHtml('a & b @c https://x.com/?a=1&b=2 <https://d.co>')
    const e = entitiesOf(text)
    const chars = Array.from(text)
    expect(e.user_mentions.map((m) => chars.slice(...m.indices).join(''))).toEqual(['@c'])
    expect(e.urls.map((u) => [u.expanded_url, chars.slice(...u.indices).join('')])).toEqual([
      ['https://x.com/?a=1&b=2', 'https://x.com/?a=1&amp;b=2'],
      ['https://d.co', 'https://d.co'],
    ])
  })

  it('keeps the whole address for display, with no ellipsis', () => {
    const url = 'https://www.espn.com/wnba/story/_/id/50189491/fever-forward-aliyah-boston-arthroscopic-knee-surgery'
    expect(entitiesOf(url).urls[0].display_url).toBe(url.slice('https://'.length))
  })
})

describe('draftTweet', () => {
  it('is a reply from the brand account to the target, with the text range in code points', () => {
    const t = draftTweet({ account: SGU, text: 'two days 🏆', replyTo: { status_id: '42', author: 'AtlantaDream' }, createdAt: AT })
    expect(t.user).toEqual({
      id_str: '',
      name: 'Sam',
      screen_name: '_sgu_',
      profile_image_url_https: 'https://pbs.twimg.com/a.jpg',
      profile_image_shape: 'Circle',
      verified: false,
      is_blue_verified: false,
    })
    expect(t.display_text_range).toEqual([0, 10])
    expect([t.in_reply_to_screen_name, t.in_reply_to_status_id_str]).toEqual(['AtlantaDream', '42'])
    expect([t.__typename, t.created_at, t.text, t.favorite_count, t.conversation_count]).toEqual(['Tweet', AT, 'two days 🏆', 0, 0])
    expect(t.quoted_tweet).toBeUndefined()
  })

  it('escapes markup, so a draft can never run as HTML in TweetBody', () => {
    const t = draftTweet({ account: SGU, text: '<img src=x onerror=alert(1)> & co', createdAt: AT })
    expect(t.text).toBe('&lt;img src=x onerror=alert(1)&gt; &amp; co')
    expect(t.display_text_range).toEqual([0, Array.from(t.text).length])
  })

  it('embeds the quoted post for a quote and says nothing about replying for a post', () => {
    const quoted = draftTweet({ account: SGU, text: 'original', createdAt: AT })
    const t = draftTweet({ account: SGU, text: 'this', quoted, createdAt: AT })
    expect(t.in_reply_to_screen_name).toBeUndefined()
    expect(t.quoted_tweet?.text).toBe('original')
    expect(t.quoted_tweet?.self_thread).toEqual({ id_str: quoted.id_str })
  })

  it('draws an initial when the account has no picture or name', () => {
    const t = draftTweet({ account: { handle: '_sgu_', name: null, avatar_url: null, verified: false }, text: 'x', createdAt: AT })
    expect(t.user.name).toBe('_sgu_')
    expect(t.user.profile_image_url_https).toBe(initialAvatar('_sgu_'))
    expect(decodeURIComponent(initialAvatar('_sgu_'))).toContain('>S</text>')
  })
})

describe('storedTweet', () => {
  const stored: StoredPost = {
    status_id: '2110885747083399199',
    author: 'AtlantaDream',
    text: "We're only two days away",
    created_at: '2026-10-16T00:09:15+00:00',
    likes: 40,
    replies: 10,
    reposts: 3,
    quotes: 0,
    author_followers: null,
    in_reply_to: null,
    in_reply_to_status: null,
  }

  it('carries the stored text, author, time and counts', () => {
    const t = storedTweet(stored)
    expect([t.id_str, t.user.screen_name, t.user.name, t.text, t.created_at]).toEqual([
      '2110885747083399199',
      'AtlantaDream',
      'AtlantaDream',
      "We're only two days away",
      '2026-10-16T00:09:15+00:00',
    ])
    expect([t.favorite_count, t.conversation_count]).toEqual([40, 10])
    expect(t.in_reply_to_screen_name).toBeUndefined()
  })

  it('names the parent only when it knows both its author and its id', () => {
    expect(storedTweet({ ...stored, in_reply_to: 'pnpmjs' }).in_reply_to_screen_name).toBeUndefined()
    const t = storedTweet({ ...stored, in_reply_to: 'pnpmjs', in_reply_to_status: '856484673572261888' })
    expect([t.in_reply_to_screen_name, t.in_reply_to_status_id_str]).toEqual(['pnpmjs', '856484673572261888'])
  })

  it('treats missing counts as zero and a missing author as unknown', () => {
    const t = storedTweet({ ...stored, author: '', likes: null, replies: null })
    expect([t.user.screen_name, t.favorite_count, t.conversation_count]).toEqual(['unknown', 0, 0])
  })
})
