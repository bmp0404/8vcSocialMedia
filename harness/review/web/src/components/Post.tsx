// A post drawn with react-tweet's own parts, the way X draws it. The page leaves out the parts that
// would act on X (Follow, like, reply) and the counts nobody needs at that moment; each caller adds
// the one line of facts it does need.

import { memo, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  QuotedTweet,
  TweetBody,
  TweetContainer,
  TweetHeader,
  TweetInReplyTo,
  TweetMedia,
  TweetSkeleton,
  enrichTweet,
  type TwitterComponents,
} from 'react-tweet'
import type { Tweet } from 'react-tweet/api'
import { api, type PostData } from '../lib/api'
import { storedTweet, wholeLinks, withFullText } from '../lib/tweet'

type Props = {
  tweet: Tweet
  /** For a draft, which isn't on X yet: every link in the post goes to the account instead. */
  linkTo?: string
  showReplyTo?: boolean
  /** [VERIFY: ...] markers to highlight in the text. */
  highlight?: string[]
  footer?: ReactNode
}

// Pictures below the first screen wait until they are scrolled near; a queue can hold a dozen posts.
const LAZY: TwitterComponents = {
  AvatarImg: (p) => <img {...p} loading="lazy" decoding="async" />,
  MediaImg: (p) => <img {...p} loading="lazy" decoding="async" />,
}

// Memoized, so typing in a card's reply box doesn't re-render the posts around it.
export const Post = memo(function Post({ tweet: raw, linkTo, showReplyTo = false, highlight, footer }: Props) {
  const tweet = useMemo(() => {
    const t = enrichTweet(raw)
    if (linkTo) t.url = linkTo
    return t
  }, [raw, linkTo])
  const ref = useRef<HTMLDivElement>(null)
  useMarkers(ref, highlight, tweet)
  return (
    <div ref={ref} className="post" data-draft={linkTo ? '' : undefined}>
      <TweetContainer>
        <TweetHeader tweet={tweet} components={LAZY} />
        {showReplyTo && tweet.in_reply_to_status_id_str ? <TweetInReplyTo tweet={tweet} /> : null}
        <TweetBody tweet={tweet} />
        {tweet.mediaDetails?.length ? <TweetMedia tweet={tweet} components={LAZY} /> : null}
        {tweet.quoted_tweet ? <QuotedTweet tweet={tweet.quoted_tweet} /> : null}
        {footer ? <div className="post-footer">{footer}</div> : null}
      </TweetContainer>
    </div>
  )
})

/** Marks [VERIFY: ...] in the rendered text with the CSS Custom Highlight API, which styles text ranges
 * without touching the DOM react-tweet built. Where the API is missing the marker still reads as text. */
function useMarkers(ref: React.RefObject<HTMLDivElement | null>, markers: string[] | undefined, tweet: unknown) {
  useEffect(() => {
    const el = ref.current
    if (!el || !markers?.length || typeof CSS === 'undefined' || !('highlights' in CSS)) return
    const ranges: Range[] = []
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.textContent ?? ''
      for (const m of markers) {
        for (let i = text.indexOf(m); i >= 0; i = text.indexOf(m, i + m.length)) {
          const r = new Range()
          r.setStart(node, i)
          r.setEnd(node, i + m.length)
          ranges.push(r)
        }
      }
    }
    const registry = CSS.highlights
    const all = registry.get('verify') ?? new Highlight()
    ranges.forEach((r) => all.add(r))
    registry.set('verify', all)
    return () => ranges.forEach((r) => all.delete(r))
  }, [ref, markers, tweet])
}

const cache = new Map<string, Promise<PostData>>()

/** A post by status id from GET /api/tweet/<id>: X's own copy, else the harness's. Requests are shared. */
export function usePost(statusId: string | null | undefined) {
  const [state, setState] = useState<{ post?: PostData; error?: string }>({})
  useEffect(() => {
    if (!statusId) return
    let live = true
    let p = cache.get(statusId)
    if (!p) {
      p = api.post(statusId)
      cache.set(statusId, p)
      p.catch(() => cache.delete(statusId))
    }
    p.then(
      (post) => live && setState({ post }),
      (e: Error) => live && setState({ error: e.message }),
    )
    return () => {
      live = false
    }
  }, [statusId])
  const post = state.post
  const tweet = useMemo(
    () => (post ? (post.data ? wholeLinks(withFullText(post.data, post.stored)) : post.stored ? storedTweet(post.stored) : null) : null),
    [post],
  )
  return { tweet, missing: state.post?.missing ?? null, error: state.error, loading: !state.post && !state.error && !!statusId }
}

export function PostSkeleton() {
  return (
    <div className="post" role="status">
      <span className="sr-only">Loading the post</span>
      <TweetSkeleton />
    </div>
  )
}

/** One quiet line for a post X no longer has, which matters before replying to it. */
export function missingNote(missing: PostData['missing']): string | null {
  if (missing === 'tombstone') return 'This post is no longer public on X.'
  if (missing === 'not_found') return 'X has no post with this id; this is the copy the harness read.'
  return null
}
