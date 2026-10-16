import { describe, expect, it } from 'vitest'
import { ago, date, freshness, ordinal } from './time'

const NOW = new Date('2026-10-16T12:00:00Z')

describe('ago', () => {
  it('says how long ago in the largest whole unit', () => {
    expect(ago('2026-10-16T11:59:30Z', NOW)).toBe('just now')
    expect(ago('2026-10-16T11:56:00Z', NOW)).toBe('4 min ago')
    expect(ago('2026-10-16T09:00:00Z', NOW)).toBe('3 h ago')
    expect(ago('2026-10-14T12:00:00Z', NOW)).toBe('2 d ago')
    expect(ago('2026-09-01T12:00:00Z', NOW)).toBe('Sep 1')
    expect(ago('2017-12-20T16:27:41Z', NOW)).toBe('Dec 20, 2017')
  })

  it('shows nothing for a missing or broken time, and just now for a clock ahead of ours', () => {
    expect(ago(null, NOW)).toBe('')
    expect(ago('soon', NOW)).toBe('')
    expect(ago('2026-10-16T12:05:00Z', NOW)).toBe('just now')
  })
})

describe('freshness', () => {
  it('splits at the hot path’s 10 minutes and the reply lane’s 30', () => {
    expect(freshness('2026-10-16T11:51:00Z', NOW)).toBe('fresh')
    expect(freshness('2026-10-16T11:50:00Z', NOW)).toBe('closing')
    expect(freshness('2026-10-16T11:30:01Z', NOW)).toBe('closing')
    expect(freshness('2026-10-16T11:30:00Z', NOW)).toBe('late')
    expect(freshness(null, NOW)).toBe('late')
  })
})

describe('ordinal and date', () => {
  it('reads a rank as English does', () => {
    expect([1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101, 111].map(ordinal)).toEqual(
      ['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '23rd', '101st', '111th'],
    )
    expect(date('2026-03-05T10:00:00Z', NOW)).toBe('Mar 5')
  })
})
