// The story a caption or post draft is about. Decide writes it as the narrative's title, its summary,
// then the source credit, one per line (`source` in harness/minds/lanes.py), so the first line is the title.

export type Story = { title: string; rest: string }

export function storyParts(text: string): Story {
  const [title = '', ...rest] = text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
  return { title, rest: rest.join('\n') }
}
