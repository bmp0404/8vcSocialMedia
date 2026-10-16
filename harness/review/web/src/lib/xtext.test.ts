import { describe, expect, it } from 'vitest'
import vectors from './xtext.vectors.json'
import { weightedLength } from './xtext'

// Weights harness/xtext.py computed; tests/test_review_xtext_vectors.py checks the same file against Python.
describe('weightedLength', () => {
  it.each(vectors.map((v) => [v.text, v.weight] as const))('weighs %j as %i', (text, weight) => {
    expect(weightedLength(text)).toBe(weight)
  })
})
