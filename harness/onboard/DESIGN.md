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

**Mission.** One LLM call writes the lists from numbered sources: F for site facts, E for exemplars, P for the 40 most recent other originals. "What it is" and "Who it's for" may cite only F ids, so no product claim can rest on a post. Code renders each kept item with links to its sources, so the model never writes a URL. An item is left out, and listed in the report, when it cites nothing usable or fails the trace. The first line of `mission.md` says it is provisional until a person edits it. The Do and Don't rules are adapted from the devtools mission: its coding-agent examples and its rule against posing as a customer are gone, since the account is the brand itself, and a product may be described as far as the mission's own sources go.

**The trace** (`claims.trace`). A cited line passes when:

- every name in it appears as a whole word in what it cites: the capitalized words the slop gate treats as names (`gate.capitalized_names`, which skips a sentence's first word), any word with a capital after its first letter or a digit inside it, and every @handle;
- every number in it appears in what it cites with the same unit, the rule `harness.facts.ground` uses, except that a year, or a number followed by no word or a stopword, needs only the same digits;
- in "What it is" and "Who it's for", 60% of its content words appear in what it cites, and 50% in "Core subjects". Neither threshold was measured.

A paraphrase that keeps the names and numbers but changes what is said about them can still pass, and a sentence's first word isn't checked as a name. That is why `ONBOARDING.md` lists every claim with its sources for a person to read.

## Validation on three corpus brands

`python -m harness.onboard validate` replays each brand's corpus timeline (`data/corpus/raw/`, rebuilt as `harness/corpus/DESIGN.md` describes) as it stood before 2026-10-01, drafts its profile from that and its site, and scores the draft on the two weeks after. The corpus timelines run to 2026-10-15, so the cutoff leaves six weeks or more before it and two weeks after; no other cutoff was tried.

**Brands.** @supabase, @resend and @vllm_project. All three are on the devtools watch list, so the comparison is against a list built to cover them, and all three have a full timeline read in the corpus. vLLM and Resend are two of the four brands whose watcher hot targets ended `off_mission`. Of those four, they posted the most originals after the cutoff (29 each; Netlify 22, Lovable 9). Supabase posted the most of any watched brand (63).

**Watch-list recall.** Of the accounts the brand replied to, quoted, mentioned or reposted after the cutoff, the share each list holds, and the share of those engagements (posts) it covers:

| Brand | Accounts engaged after | Drafted list (size) | Devtools list (24) | Other brands' top 24 | Every account engaged before (size) |
| --- | --- | --- | --- | --- | --- |
| @supabase | 37 | 7 (19%), 14 of 45 posts (16) | 1 (3%), 1 of 45 | 2 (5%), 2 of 45 | 13 (35%), 21 of 45 (68) |
| @resend | 26 | 5 (19%), 6 of 28 posts (14) | 1 (4%), 1 of 28 | 1 (4%), 2 of 28 | 5 (19%), 6 of 28 (44) |
| @vllm_project | 25 | 7 (28%), 17 of 36 posts (16) | 0 | 2 (8%), 5 of 36 | 9 (36%), 19 of 36 (52) |

"Other brands' top 24" ranks the accounts that the most other corpus brands engaged with before the cutoff, a list built from the same data without the brand's own history. The last column is the ceiling for any list built from the brand's history: most accounts a brand engaged with after the cutoff it had never engaged with before (24 of 37, 21 of 26, 16 of 25). The drafted lists reach most of that ceiling with 14 to 16 accounts. Filling the list to 24 without the two-post floor added no hits on any brand. The hits are mostly companies the brand works with (Stripe, Y Combinator, Clerk, Laravel, NVIDIA, DeepSeek, Red Hat AI) and, for Supabase, its own staff.

**Mission fit.** The reply planner (`lanes.plan_targets`, one post per call as on the watcher's hot path, no lessons, no live facts) judged each post under the drafted mission and under the devtools mission. "Brand's own" is a random 60 of its originals after the cutoff (all of them when fewer); "it engaged with" is every post after the cutoff it reposted, quoted or replied to whose text the corpus holds; "peers" is a random 40 originals after the cutoff by other corpus brands that it didn't engage with. Off-mission rates:

| Brand | Brand's own | It engaged with | Both | Peers |
| --- | --- | --- | --- | --- |
| @supabase, drafted | 0 of 60 | 2 of 29 | **2%** (2 of 89) | 22% (9 of 40) |
| @supabase, devtools | 48 of 60 | 24 of 29 | 81% (72 of 89) | 30% (12 of 40) |
| @resend, drafted | 0 of 29 | 1 of 3 | **3%** (1 of 32) | 35% (14 of 40) |
| @resend, devtools | 22 of 29 | 3 of 3 | 78% (25 of 32) | 48% (19 of 40) |
| @vllm_project, drafted | 0 of 29 | 0 of 1 | **0%** (0 of 30) | 88% (35 of 40) |
| @vllm_project, devtools | 27 of 29 | 1 of 1 | 93% (28 of 30) | 48% (19 of 40) |

On the brand's own conversations the two missions disagreed on 70, 24 and 28 posts, and every time it was the devtools mission that said off-mission (sign test p < 10⁻⁶ for each brand). On peers they disagreed in both directions: 6 and 3 for Supabase, 10 and 5 for Resend (neither significant), and 3 and 19 for vLLM, whose drafted mission rejects most dev-tool peers' posts (p = 0.0009). So the drafted missions don't accept everything: vLLM's is about LLM inference and calls a Supabase or Grafana post off-mission. The two off-mission calls on Supabase's own conversations were a third party's post about Supabase hiring and a joke tagging a guest. The devtools mission's reasons were what its narrowness predicts, for example "It has nothing on coding agents, MCP, worktrees" on a photo from Supabase's own hackathon.

**Claims.** Every cited line in the three drafted missions traces to what it cites: 30 of 30 for each brand (8, 6 and 6 product lines; 4, 2 and 4 audience lines; the rest subjects, tone, goals and quoted exemplars). The trace dropped 6, 9 and 7 site facts and left 4, 1 and 2 mission lines out, most for a number or name its source didn't hold.

**Calls.** 542 planner calls for mission fit and 33 drafting calls (one per page and one for the mission), 575 in all, under the brief's 1,200. Every call recorded asked for `claude-sonnet-5-5` and the gateway reported `claude-sonnet-5-5`. The first invocation, which drafted and judged Supabase and Resend (426 calls), crashed on vLLM's site before writing its model record, so its calls were counted from the verdict files and the pages read, not recorded. The rerun's 149 calls were recorded. `~/.long-run/8vc-harness/evidence/b8/validate/` holds the drafted profiles, verdicts and `results.json`; it stays out of the repository because verdict rows quote people's posts.

**Live runs.** The final `harness onboard` run on the same three accounts read 800, 400 and 400 statuses per tab and 11, 11 and 8 pages, and every cited line traced (33, 31 and 33). fxtwitter 404'd on a deep cursor in five of the six live reads over two runs, at different depths, which is why a failed tab is read again with half the limit. The report flagged 12 of Supabase's 24 watch-list accounts, 6 of Resend's 20 and 5 of vLLM's 24 as having a bio that mentions the brand. The drafts are kept local, not committed: their watch lists and reports name individual X users.

## What a person must still do

- Read every cited line in `mission.md` against its source, cut what's wrong, add what the account stands for that no page says, and delete the first line.
- Resolve conflicts between the brand's own voice and the copied Do and Don't rules. @supabase's drafted tone says its giveaway posts ask readers to comment or retweet; the copied rules forbid engagement bait.
- Prune the watch list. It holds people as well as companies, and every account in `accounts` has all of its posts treated as on topic. Move people and broad platforms to `broad_accounts`, where only keyword matches count. Accounts flagged for a bio that names the brand are usually its own staff, which the brand already amplifies.
- Rewrite subjects and goals tied to one date. Resend's draft names a conference on October 21 and a running club on October 20; both go stale within a week.
- Rewrite the four copied files for any brand that isn't a developer tool.

## Not verified, and open questions

- The "brand's own" posts come from the same account whose earlier posts built the mission, so most of the fit gap is the drafted mission describing the account and the devtools mission describing a coding-agent commentator. The posts the brand engaged with are the less circular test, and only Supabase has many (29).
- Nothing says whether the peer abstentions are right. vLLM's drafted mission calling 88% of dev-tool peers off-mission may be correct for an inference engine, or too narrow.
- The planner judged each post once under each mission; its own run-to-run noise wasn't measured.
- The site was read on 2026-10-16 for drafts cut at 2026-10-01, so site facts postdate the cutoff. The timeline was cut; the site couldn't be.
- The corpus timelines come from the Posts tab, so a brand's replies to other accounts appear only where a reply sample caught them (7 of Supabase's, none of the others'). Recall here is mostly over mentions and reposts. The live command reads both tabs.
- No person has checked a drafted claim against its page. I read the Supabase and vLLM validation missions and nothing looked wrong, but the trace can pass a paraphrase that changes the meaning.
- No watcher has polled a drafted watch list, and no lane has read the site facts saved to the memory store. The drafts loaded through `watchlist.load` and passed `harness run --provider mock`.
- Every threshold here is a guess: exemplars at 2x the median and six words, the two-post floor and the 24 cap on the watch list, 60% and 50% word overlap, 30-day facts, and the stray-post rule.
- The validation drafts for Supabase and Resend have no keywords: the mission call returned them in a shape the code then dropped, fixed after the run. Keywords don't enter any of the three measures.
