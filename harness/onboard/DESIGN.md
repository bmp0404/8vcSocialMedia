# Brand onboarding design

This is for whoever adds a brand to the platform, or decides whether onboarding is worth keeping. It answers four questions. What does `harness onboard` read and write? How does it pick the watch list, the voice exemplars and the claims in the mission? How did its drafts do against the hand-made devtools profile on three brands from the corpus? What must a person still do with a draft?

## Why it exists

A brand is a mission profile, six files under `mission/profiles/<name>/` (`CONTRIBUTING.md`, "Each brand is a mission profile"). Writing them by hand took an afternoon, and the one dev-tools profile shows what a narrow hand-written mission costs: most of the watcher's dev-tools hot targets ended in `off_mission` abstentions, on Netlify, vLLM, Lovable and Resend posts. 8VC's brief asks for a core that "watches a brand's own material and activity to surface what's genuinely interesting". `harness onboard` drafts the profile from the brand's own posts and site, so adding a brand becomes an edit of a draft.

## Commands

```sh
HARNESS_PROVIDER=anthropic uv run python -m harness onboard --handle supabase --site https://supabase.com/ --profile supabase
uv run python -m harness onboard check --profile supabase       # re-trace the mission's claims after editing it
HARNESS_PROVIDER=anthropic uv run python -m harness.onboard validate --out <dir outside the repo>
```

`onboard` refuses a profile name that already exists before it makes any request, and checks again just before it renames the finished directory into place.

## What it reads

**The timeline.** Both of the account's tabs through `XReader.profile_statuses`: the Posts tab (originals, threads, quotes, reposts) and the Posts and replies tab (its replies to other accounts), up to 800 statuses each, as far back as fxtwitter's pages go. The reader uses the shared throttle in `~/.cache/media-harness/throttle/` with a 2-second interval, so it takes at most half of the 1 request a second that every process on the Mac shares, and the watcher's polls keep their turns. fxtwitter 404s on some deep cursors (it did on @supabase's Posts tab), and `profile_statuses` keeps none of the pages before a failure. So a failed tab is read again with half the limit; the pages it already got are cached, so each retry costs only the page that failed. The report says how far each tab got and why it stopped.

**The site.** The homepage and up to 10 linked pages on the same domain or a subdomain, as text. `site.pick_links` takes one-segment product pages first (`/features`, `/pricing`, `/docs`, `docs.` subdomains), then two-segment ones, then the rest, at most three under one first segment, and never sign-in, legal, job, asset or community pages. Every request waits its turn on the same per-host throttle at a 2-second interval, robots.txt is honored for the harness's User-Agent, and pages are cached for a day under `out/onboard/cache/`.

## What it writes

- `mission/profiles/<name>/mission.md`, `observe.toml` and `ONBOARDING.md`, drafted.
- `panel.json`, `reply_framing.json`, `prompts.toml` and `qualify.toml`, copied from the devtools profile. A timeline and a site don't decide an audience panel, the brand phrases the action prompts insert, a reply framing or a post ranker.
- The site facts, as memory `Fact`s with the page as `source_url`, `as_of` the day it was read and `valid_until` 30 days later, through the facts store into `memory/store/<name>/`. The 30 days copy `harness.facts.posts.POST_DAYS`; nothing measured how fast site copy changes.
- `out/onboard/<name>/sources.json`, the text of every page and post the mission can cite, which `onboard check` needs, and `draft.json`, the structured draft.

## How it decides

**Baselines.** Originals, replies and reposts a day, each over its own window from its oldest post read to the read, because the two tabs reach back different distances. A pinned post years older than the rest stretched @supabase's window to four years, so the oldest post is dropped while the gap after it is over 30 days and over 10 times the median gap. Medians of engagement (likes + reposts + replies + quotes), likes and views use only originals 48 hours or older at the read, the corpus's maturity rule. Formats are shares of originals with a photo, video, gif, link or quote.

**Exemplars.** Settled originals whose engagement is at least twice the account's own median, strongest first, at most five, each quoted in the mission with its link and its multiple of the median. A post needs six words or more, because an exemplar is there to show the voice: on @supabase's timeline the two strongest posts were "Love that guy" and a five-word caption, both riding on an image. Neither 2x nor six words was measured.

**Watch list.** Every reply to, quote of, mention of and repost of another account in the brand's posts, counted once per post. A reply's leading @handles are X's addressing and don't count as mentions. Accounts rank by how many of the brand's posts engaged them, then by how many kinds of engagement, then by the most recent, then by handle. The cut keeps accounts with two posts or more, at most 24: one post is as likely noise as a relationship, and 24 is the size of the devtools list the watcher's request budget was set for (`harness/watcher/DESIGN.md`). All of them go in `[reply_targets] accounts`. The report lists the reason for each and the next ten below the cut.

**Site facts.** One LLM call per page lists what it says about the product, who it's for and named features, each with a quote copied from the page. A fact is kept only when its quote is in the page word for word (after case, whitespace and quote marks are normalized) and its statement passes the trace below against the page.

**Mission.** One LLM call writes the lists from numbered sources: F for site facts, E for exemplars, P for the 40 most recent other originals. "What it is" and "Who it's for" may cite only F ids, so no product claim can rest on a post. Code renders each kept item with links to its sources, so the model never writes a URL. An item is left out, and listed in the report, when it cites nothing usable or fails the trace. The first line of `mission.md` says it is provisional until a person edits it. The Do and Don't rules are the devtools mission's, with its coding-agent examples removed.

**The trace** (`claims.trace`). A cited line passes when:

- every name in it appears as a whole word in what it cites: the capitalized words the slop gate treats as names (`gate.capitalized_names`, which skips a sentence's first word), any word with a capital after its first letter or a digit inside it, and every @handle;
- every number in it appears in what it cites with the same unit, the rule `harness.facts.ground` uses, except that a year, or a number followed by no word or a stopword, needs only the same digits;
- in "What it is" and "Who it's for", 60% of its content words appear in what it cites, and 50% in "Core subjects". Neither threshold was measured.

A paraphrase that keeps the names and numbers but changes what is said about them can still pass, and a sentence's first word isn't checked as a name. That is why `ONBOARDING.md` lists every claim with its sources for a person to read.

## Validation on three corpus brands

RESULTS

## What a person must still do

- Read every cited line in `mission.md` against its source, cut what's wrong, add what the account stands for that no page says, and delete the first line.
- Resolve conflicts between the brand's own voice and the copied Do and Don't rules. @supabase's drafted tone says its giveaway posts ask readers to comment or retweet; the copied rules forbid engagement bait.
- Prune the watch list. It holds people as well as companies, and every account in `accounts` has all of its posts treated as on topic. Move people and broad platforms to `broad_accounts`, where only keyword matches count.
- Rewrite the four copied files for any brand that isn't a developer tool.

## Not verified, and open questions

NOTVERIFIED
