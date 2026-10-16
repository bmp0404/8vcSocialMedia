// The one transition the page animates: the queue closing up when a card is decided or undone. It runs
// tens of times a session, so it is short, and it uses the View Transitions API, which moves the other
// cards with transforms off the main thread. Without the API, or with reduced motion, the change is instant.

import { flushSync } from 'react-dom'

export function transition(update: () => void): void {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  if (reduce || !('startViewTransition' in document)) {
    update()
    return
  }
  document.startViewTransition(() => flushSync(update))
}

/** A view-transition-name for a card: CSS idents allow letters, digits, hyphen and underscore. */
export function transitionName(id: string): string {
  return `card-${id.replace(/[^A-Za-z0-9_-]/g, '_')}`
}
