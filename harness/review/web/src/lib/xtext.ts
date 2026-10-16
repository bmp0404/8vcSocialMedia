// X's weighted character count, a port of harness/xtext.py so the reply box can count as you type.
// The server checks again on approval; xtext.vectors.json holds counts Python computed, and both test
// suites check them, so the two stay equal.

export const MAX_WEIGHTED_CHARS = 280
const URL_WEIGHT = 23
const LIGHT_RANGES: [number, number][] = [
  [0, 4351],
  [8192, 8205],
  [8208, 8223],
  [8242, 8247],
]
const LINK = /https?:\/\/\S+/g
const TRAILING = '.,;:!?)]}\'"'
const ZWJ = 0x200d
const VS15 = 0xfe0e
const VS16 = 0xfe0f
const KEYCAP = 0x20e3
const EMOJI_EXTRA = new Set([0x2b50, 0x2b55, 0x2934, 0x2935, 0x3030, 0x303d, 0x3297, 0x3299])

const light = (cp: number) => LIGHT_RANGES.some(([lo, hi]) => lo <= cp && cp <= hi)
const emoji = (cp: number) => (cp >= 0x1f000 && cp <= 0x1faff) || (cp >= 0x2600 && cp <= 0x27bf) || EMOJI_EXTRA.has(cp)
const modifier = (cp: number) =>
  cp === VS15 || cp === VS16 || cp === KEYCAP || (cp >= 0x1f3fb && cp <= 0x1f3ff) || (cp >= 0xe0020 && cp <= 0xe007f)

/** Index just past the emoji sequence starting at i, or i when no emoji starts there. */
function emojiEnd(cps: number[], i: number): number {
  const cp = cps[i]
  if (cp >= 0x1f1e6 && cp <= 0x1f1ff) {
    return i + 1 < cps.length && cps[i + 1] >= 0x1f1e6 && cps[i + 1] <= 0x1f1ff ? i + 2 : i + 1
  }
  let j: number
  if ('0123456789#*©®'.includes(String.fromCodePoint(cp)) && i + 1 < cps.length && (cps[i + 1] === VS16 || cps[i + 1] === KEYCAP)) {
    j = i + 1
  } else if (emoji(cp)) {
    j = i + 1
  } else {
    return i
  }
  while (j < cps.length) {
    if (modifier(cps[j])) j += 1
    else if (cps[j] === ZWJ && j + 1 < cps.length && emoji(cps[j + 1])) j += 2
    else break
  }
  return j
}

function count(text: string): number {
  const cps = Array.from(text, (c) => c.codePointAt(0) ?? 0)
  let n = 0
  let i = 0
  while (i < cps.length) {
    const end = emojiEnd(cps, i)
    if (end > i) {
      n += 2
      i = end
    } else {
      n += light(cps[i]) ? 1 : 2
      i += 1
    }
  }
  return n
}

export function weightedLength(raw: string): number {
  const text = raw.normalize('NFC')
  let n = 0
  let last = 0
  for (const m of text.matchAll(LINK)) {
    let link = m[0]
    while (link && TRAILING.includes(link[link.length - 1])) link = link.slice(0, -1)
    const start = m.index ?? 0
    n += count(text.slice(last, start)) + URL_WEIGHT
    last = start + link.length
  }
  return n + count(text.slice(last))
}
