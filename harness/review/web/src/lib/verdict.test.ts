import { describe, expect, it } from 'vitest'
import { primaryLabel, reviewBody, TAKE_NOTE, VerdictError } from './verdict'

const DRAFT = 'two days. ballhalla better be loud'

describe('reviewBody', () => {
  it('approves the draft as written when the box is empty', () => {
    expect(reviewBody({ kind: 'approve', draft: DRAFT, composer: '  ', origin: 'take' })).toEqual({
      status: 'approved',
      text: null,
      note: '',
      reasons: [],
    })
  })

  it('sends a take verbatim, trimmed, with the take note', () => {
    expect(reviewBody({ kind: 'approve', draft: DRAFT, composer: ' my own words \n', origin: 'take' })).toEqual({
      status: 'approved',
      text: 'my own words',
      note: TAKE_NOTE,
      reasons: [],
    })
  })

  it('sends an edit with no note, and an unchanged edit as the draft itself', () => {
    expect(reviewBody({ kind: 'approve', draft: DRAFT, composer: `${DRAFT}!`, origin: 'edit' }).text).toBe(`${DRAFT}!`)
    expect(reviewBody({ kind: 'approve', draft: DRAFT, composer: `${DRAFT}!`, origin: 'edit' }).note).toBe('')
    expect(reviewBody({ kind: 'approve', draft: DRAFT, composer: ` ${DRAFT} `, origin: 'edit' }).text).toBeNull()
  })

  it('refuses to approve nothing', () => {
    expect(() => reviewBody({ kind: 'approve', draft: null, composer: '', origin: 'take' })).toThrow(VerdictError)
  })

  it('kills with the picked reasons, once each, and a trimmed note', () => {
    expect(reviewBody({ kind: 'kill', reasons: ['tone', 'slop', 'tone'], note: ' meh ' })).toEqual({
      status: 'killed',
      text: null,
      note: 'meh',
      reasons: ['tone', 'slop'],
    })
    expect(() => reviewBody({ kind: 'kill', reasons: [], note: '' })).toThrow(VerdictError)
  })

  it('undoes to pending', () => {
    expect(reviewBody({ kind: 'undo' })).toEqual({ status: 'pending', text: null, note: '', reasons: [] })
  })
})

describe('primaryLabel', () => {
  it('names what Approve will send', () => {
    expect(primaryLabel('reply', DRAFT, '', 'take')).toBe('Approve draft')
    expect(primaryLabel('reply', null, '', 'take')).toBe('Approve your reply')
    expect(primaryLabel('quote', DRAFT, 'mine', 'take')).toBe('Approve your quote')
    expect(primaryLabel('post', DRAFT, 'mine', 'take')).toBe('Approve your post')
    expect(primaryLabel('mention', DRAFT, 'mine', 'take')).toBe('Approve your post')
    expect(primaryLabel('reply', DRAFT, `${DRAFT}.`, 'edit')).toBe('Approve edit')
    expect(primaryLabel('reply', DRAFT, DRAFT, 'edit')).toBe('Approve draft')
  })
})
