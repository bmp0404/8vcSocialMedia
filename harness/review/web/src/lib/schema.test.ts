import { describe, expect, it } from 'vitest'
import { controlsFor, labelsPayload, optionLabel, type Schema } from './schema'

// The shape of data/labels/schema.json on sam/corpus-clean (version 2), trimmed to one field per kind.
const SCHEMA: Schema = {
  version: 2,
  fields: [
    { name: 'format', kind: 'enum', values: ['announcement', 'tip_or_howto', 'other'], question: 'What is this item mainly doing?', applies_to: ['post', 'reply'] },
    { name: 'specificity', kind: 'ordinal', values: [1, 2, 3, 4, 5], question: 'How concrete is it?', applies_to: ['post', 'reply'] },
    { name: 'adds_to_parent', kind: 'ordinal', values: [1, 2, 3], question: 'Does it add to the post it answers?', applies_to: ['reply'] },
    { name: 'why', kind: 'text', values: [], question: 'Anything else?', applies_to: ['post'] },
  ],
}

describe('controlsFor', () => {
  it('makes one control per field that applies to the kind, in schema order', () => {
    expect(controlsFor(SCHEMA, 'post').map((c) => c.name)).toEqual(['format', 'specificity', 'why'])
    expect(controlsFor(SCHEMA, 'reply').map((c) => c.name)).toEqual(['format', 'specificity', 'adds_to_parent'])
    expect(controlsFor(SCHEMA, 'thread')).toEqual([])
  })

  it('keeps each value as the schema has it, with a readable label', () => {
    const [format, specificity, why] = controlsFor(SCHEMA, 'post')
    expect(format.options).toEqual([
      { value: 'announcement', label: 'Announcement' },
      { value: 'tip_or_howto', label: 'Tip or howto' },
      { value: 'other', label: 'Other' },
    ])
    expect(specificity.options.map((o) => o.value)).toEqual([1, 2, 3, 4, 5])
    expect(specificity.options[0].label).toBe('1')
    expect([why.kind, why.options]).toEqual(['text', []])
    expect(format.question).toBe('What is this item mainly doing?')
  })
})

describe('labelsPayload', () => {
  const controls = controlsFor(SCHEMA, 'post')

  it('sends answered fields only, numbers as numbers and text trimmed', () => {
    expect(labelsPayload(controls, { format: 'other', specificity: 3, why: '  ok ' })).toEqual({ format: 'other', specificity: 3, why: 'ok' })
    expect(labelsPayload(controls, { specificity: undefined, why: '   ' })).toEqual({})
  })

  it('refuses a value the schema does not list', () => {
    expect(() => labelsPayload(controls, { specificity: '3' })).toThrow("specificity=3 isn't one of the schema's values")
    expect(() => labelsPayload(controls, { format: 'joke' })).toThrow()
  })
})

describe('optionLabel', () => {
  it('reads a code as words', () => {
    expect(optionLabel('main_point')).toBe('Main point')
    expect(optionLabel(5)).toBe('5')
  })
})
