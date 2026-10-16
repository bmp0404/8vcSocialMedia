import { describe, expect, it } from 'vitest'
import { storyParts } from './story'

describe('storyParts', () => {
  it('takes the first line as the title and keeps the summary and credit lines in order', () => {
    const text =
      "Sophie Cunningham (Fever) keeps calling out Angel Reese's play and the league\n" +
      "Fever's Sophie Cunningham is in the headlines again over Reese's defense.\n" +
      '(reported by Basketball Network)'
    expect(storyParts(text)).toEqual({
      title: "Sophie Cunningham (Fever) keeps calling out Angel Reese's play and the league",
      rest: "Fever's Sophie Cunningham is in the headlines again over Reese's defense.\n(reported by Basketball Network)",
    })
  })

  it('handles a title alone, blank lines and stray spaces', () => {
    expect(storyParts('  Just a title  ')).toEqual({ title: 'Just a title', rest: '' })
    expect(storyParts('\nTitle\n\n  Summary \n')).toEqual({ title: 'Title', rest: 'Summary' })
    expect(storyParts('')).toEqual({ title: '', rest: '' })
  })
})
