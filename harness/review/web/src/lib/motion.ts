// The one transition the page animates: the queue closing up when a card is decided or undone with the
// pointer. It runs tens of times a session, so it is short, and it uses the View Transitions API, which moves
// the other cards with transforms off the main thread. A keyboard verdict, reduced motion, or a browser
// without the API changes the list at once: an action repeated from the keyboard should never wait on motion.

import { flushSync } from 'react-dom'

export function transition(update: () => void, animate = true): void {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  if (!animate || reduce || !('startViewTransition' in document)) {
    update()
    return
  }
  document.startViewTransition(() => flushSync(update))
}

/** A view-transition-name for a card: CSS idents allow letters, digits, hyphen and underscore. */
export function transitionName(id: string): string {
  return `card-${id.replace(/[^A-Za-z0-9_-]/g, '_')}`
}
