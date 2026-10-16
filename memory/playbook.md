# Coordigent X Playbook v1

**Evidence caveat:** Our own "winners" are reviewer scores (1.0 and 0.7), not engagement data. They show which drafts the critics liked, so treat them as hypotheses. The outside evidence is stronger. 9 of 12 viral dev-tool posts had video and 3 had a photo. 4 of the 6 flops had no media, and all 6 were thin on context. The creative brief outranks this playbook: stories with a shape, no named characters, fresh small worlds, and a visual pitched for every post that could carry one.

**Mix:** about 1 in 4 posts names Coordigent. Patterns marked **[Product]** do, and the rest build the audience of agent-wranglers. Patterns 1-5 and 12 should be the most frequent.

## Patterns

1. **The timestamp day** (our 0.7 winner, "9:50am... By noon:")
   - Template: *A hopeful morning time, a turn at noon, three agents in trouble, each described by what it did, and a last line about the human's afternoon.*
   - Example: "9:50am: you launch 4 agents and feel like a tech lead. By noon: one agent redid a finished migration, two are in the same file, and one has been waiting on you since 10:40. 3pm: you are reviewing."
   - Tag: audience-builder.
   - Craft note: the critic said to cut the "4x" framing so the afternoon of reviewing is the punchline. Add a visual: a calendar-style graphic with the agents as colored blocks overlapping.

2. **One agent, one misplaced good deed**
   - Template: *One small scene, one agent described by what it did, one specific act of misplaced helpfulness.*
   - Example: "An agent looked at the migration you finished an hour ago and thought: I can make this more me."
   - Tag: audience-builder.
   - Craft note: no invented names. A stranger has to get it with zero context.

3. **Agent confession in its own voice** (Railway's raila-chan; our reply "agent 14... we have a lot of compute")
   - Template: *First person, specific minutes and filenames, ending on the funniest line.*
   - Example: "agent 14, checking in. i've been in schema.sql for 20 minutes. agent 31 just opened it too. we have never met. we have a lot of compute."
   - Tag: audience-builder.
   - Craft note: end on the best line. Critics flagged this twice, and a trailing "nobody told us" weakens it.

4. **A small world, pictured** (brief: kitchen, control tower, group project)
   - Template: *One metaphor, three concrete details, and a landing that points back at agents.*
   - Example: "Five chefs, one stove, no one calling orders. Everyone is cooking. Nobody is eating."
   - Tag: audience-builder.
   - Visual to pitch: a simple illustration or a one-panel comic of the kitchen, with the stove labeled `auth.ts`. A tower with no radio and planes labeled with file names would work the same way.

5. **The thesis flip with a short runway** (our top-scored hook post)
   - Template: *One beat of setup, then the reframe as the final line.*
   - Example: "Your agents got faster. Your afternoon didn't. Throughput was never the bottleneck. Coordination was."
   - Tag: audience-builder. Attach a product mention only in a follow-up reply.
   - Craft note: the critic said "Coordination was." should land sooner, so cut the setup to a single beat.

6. **The stance: warn, don't block** (Zed's confident-stance winner, with a warmer aside)
   - Template: *A confident opinion about agent behavior that is secretly our design.*
   - Example: "Agents shouldn't be locked out of a file. They should just hear 'someone's in there' before they walk in. Grown-ups get a heads-up, not a bouncer."
   - Tag: **[Product]**. It is accurate: Coordigent warns and never blocks.

7. **Name the thing** (Cognition's coined terms; our "You are the handoff")
   - Template: *Coin a short label for a pain, then show the fix in one plain line.*
   - Example: "You are the handoff. Your agent hits its limit at 90% done, and you spend ten minutes typing 'ok so basically...' It should leave one itself: the original ask, what changed, what's left."
   - Tag: **[Product]**. Keep the product mention to one short beat, as the critic asked.
   - Craft note: "Agent collision" is another term worth coining. It works well as a pure audience-builder with no product mention.

8. **Short screen-capture clip as the whole post** (9 of 12 viral posts had video)
   - Template: *One line of caption plus a 10-20s recording of one moment, with no voiceover needed.*
   - Example: "An agent reaches for `auth.ts`. Another agent is already in it. It gets a warning first. Nothing is blocked."
   - Tag: **[Product]**.
   - Visual: a terminal recording of exactly that moment. Do not post this as text only.

9. **The screenshot with a story** (fix for the "spikiness" screenshot flop)
   - Template: *A view of our UI, captioned with what an agent did, a timestamp, and a feeling.*
   - Example: "10:40am: an agent stops and asks you a question. 1:15pm: you find out. Now it shows up in 'Needs you' instead."
   - Tag: **[Product]**.
   - Visual: a "Needs you" view screenshot with one agent highlighted. A screenshot alone is a flop, so the story must come first.

10. **Team-mode scene** (two people, two agents, one file)
    - Template: *"Your agent and your teammate's agent" micro-story, then the fix as a one-line reveal.*
    - Example: "Your agent and your teammate's agent both reach for `schema.sql`. In the old world you find out at merge. This time both of them hear about it first."
    - Tag: **[Product]**.
    - Visual: a split screen with two terminals and a teammate's agent showing up live in yours.

11. **Lead with what you don't have to do, then the command last** (Railway's "no signup, no API keys"; Cline's bare-command flop)
    - Template: *Open with the answer to the first objection, add the tool list only if it earns its place, and put the command at the end after a hook or clip.*
    - Example: "No account. Never uploads a transcript. Never pushes code. Works with Claude Code, Codex, Cursor and the rest. `curl -fsSL https://coordigent.com/install | sh`"
    - Tag: **[Product]**.
    - Craft note: the critic wanted the no-account and no-upload line first, with the tool list trimmed.

12. **The in-thread bit** (our reply winners)
    - Template: *Reply to a big agent-fleet or model-release thread with a joke in an agent's voice, or one sentence of real opinion.*
    - Example: "Fifty agents is easy to provision. Fifty agents that know two of them are in `auth.ts` is the hard part."
    - Tag: audience-builder by default. Mention Coordigent in a reply only when it answers a direct question.
    - Craft note: one sentence, with the key image up front. Don't take shots at the person. A hard sell in someone else's replies was the critics' main complaint.

## Avoid (from the flops and the critics)

- **Bare CTAs with no hook**, like Modal's "Learn more and deploy..." (0) and Cline's "Try it with `npm i -g cline`" (4). Hook first, command last.
- **Tag soup, RSVPs, and booth or giveaway replies** (3-5). Nobody outside the tagged circle cares.
- **Logo-drop recruiting copy.** It is also a customer claim we can't make.
- **Metric or UI screenshots with no human reason to care.** Pair them with what an agent did and a time (pattern 9).
- **Text-only product announcements.** Most flops had no media.
- **Emoji-led launch formatting** ("🚀 vX is out!"). It's off-voice. Those posts worked only because of the video and the credited user.
- **Burying the punchline.** Our critics flagged this three times, so cut the runway and put the best line last.
- **Cryptic lines** such as "so a deal doesn't break your setup." Name the concrete pain instead.
- **Stat-shaped claims.** Don't invent pricing, user counts, customers, funding, or benchmarks, and don't borrow the "18x lower cost" style. Fictional agent numbers like "agent 14" are fine because they are clearly a bit.
- **Anything beyond the fact sheet.** Never say we block edits or guarantee zero conflicts. We warn, and agents message each other.
- **Closing the loop with a user** (Zed's "Thanks tiny-paris!") is on hold. We have no real requests or users to credit yet, so don't fabricate any.