// The audit form, built from data/labels/schema.json alone, so a new field or value needs no page change.

export type SchemaField = {
  name: string
  kind: 'enum' | 'ordinal' | 'text'
  values: (string | number)[]
  question: string
  applies_to: string[]
}
export type Schema = { version: number | string; fields: SchemaField[] }
export type Value = string | number
export type Option = { value: Value; label: string }
export type Control = { name: string; kind: SchemaField['kind']; question: string; options: Option[] }

/** "tip_or_howto" -> "Tip or howto", 3 -> "3". */
export function optionLabel(value: Value): string {
  if (typeof value === 'number') return String(value)
  const words = value.replace(/_/g, ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** The controls for one item: the fields that apply to its kind, in schema order. */
export function controlsFor(schema: Schema, kind: string): Control[] {
  return schema.fields
    .filter((f) => f.applies_to.includes(kind))
    .map((f) => ({
      name: f.name,
      kind: f.kind,
      question: f.question,
      options: f.kind === 'text' ? [] : f.values.map((v) => ({ value: v, label: optionLabel(v) })),
    }))
}

/** The labels to send: answered fields only, with text trimmed and blank text dropped. */
export function labelsPayload(controls: Control[], answers: Record<string, Value | undefined>): Record<string, Value> {
  const out: Record<string, Value> = {}
  for (const c of controls) {
    const v = answers[c.name]
    if (v === undefined) continue
    if (c.kind === 'text') {
      const t = String(v).trim()
      if (t) out[c.name] = t
    } else if (c.options.some((o) => o.value === v)) {
      out[c.name] = v
    } else {
      throw new Error(`${c.name}=${String(v)} isn't one of the schema's values`)
    }
  }
  return out
}
