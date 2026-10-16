// Times as a person reads them at a glance: how long ago, never a raw timestamp.

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

export function date(iso: string, now: Date = new Date()): string {
  const t = new Date(iso)
  const day = `${MONTHS[t.getMonth()]} ${t.getDate()}`
  return t.getFullYear() === now.getFullYear() ? day : `${day}, ${t.getFullYear()}`
}

export function ago(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return ''
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return ''
  const s = Math.max(0, (now.getTime() - t.getTime()) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  if (s < 7 * 86400) return `${Math.floor(s / 86400)} d ago`
  return date(iso, now)
}

// The reply lane drafts only for posts under 10 minutes old, and its own cutoff is 30 minutes
// (ActionConfig.reply_max_age_minutes), so those are the two lines a hot target's age crosses.
export type Freshness = 'fresh' | 'closing' | 'late'

export function freshness(iso: string | null | undefined, now: Date = new Date()): Freshness {
  if (!iso) return 'late'
  const minutes = (now.getTime() - new Date(iso).getTime()) / 60000
  return minutes < 10 ? 'fresh' : minutes < 30 ? 'closing' : 'late'
}

/** 1 -> "1st", 12 -> "12th", 23 -> "23rd": a reply's place in its thread. */
export function ordinal(n: number): string {
  const tens = n % 100
  const suffix = tens >= 11 && tens <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[n % 10] ?? 'th'
  return `${n}${suffix}`
}
