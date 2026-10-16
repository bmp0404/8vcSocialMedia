# Action: how decide produces replies worth posting

Readers: teammates deciding what to build next in the decide stage. Code is in `harness/minds/`, experiments in `harness/minds/lab/`, their outputs in `action/experiments/`.

## Never let a model rewrite a person's words

This is the cleanest result we have. A real reply, held out of the lineup, was picked out as the AI-written one 4 times in 19 by Opus 5.5. The same reply after one "polish this for X" pass on Sonnet 5.5 was picked out 18 times in 19 (`human_raw` against `human_polish` in `action/experiments/reply_experiment.json`). The research thread's re-judge of an earlier run got 20% against 100% (`8vc-context/research/action.md`, finding 6).

So the human raw-material path carries the person's text through untouched (`lanes._human_takes`). The gate only reports on it, except for X's 280-character limit. The same evidence predicts that any "make it punchier" pass on drafts makes them more detectable. That includes the hook-rewrite pass in `harness/hooks.py` on `aarav/self-improving`, which swaps in an LLM-written opening line whenever a pairwise judge prefers it.

## What made a reply indistinguishable from real ones

Continuing the real thread as a document, then vetoing. On 19 real X threads, with Opus as the judge:

| How the reply was made | Opus spotted it | artsa alias spotted it | los alias spotted it |
| --- | --- | --- | --- |
| `pipeline`: the shipped reply lane (continuation, gate, two-order Opus veto, random pick) | 4/18 (1 abstention) | 5/18 | 6/18 |
| `cont_real`: one continuation of a page of real replies | 6/19 | 6/19 | 8/19 |
| `human_raw`: a real reply held out of the lineup | 4/19 | 4/19 | 6/19 |
| `naive`: today's persona prompt (`reply_sniper` + mission) | 19/19 | 19/19 | 19/19 |
| `remix`: style rules plus 12 real top replies as examples | 19/19 | 19/19 | 12/19 |
| `bestof`: 5 remix samples, gate, an LLM picks the least AI-sounding | 18/19 | 18/19 | 8/19 |
| `bestof_random`: the same 5 samples, gate, a random pick | 18/19 | 18/19 | 9/19 |
| `bestof_rubric`: the same 5 samples, picked by Dhilan's `RankCandidates` prompt (6 of 19 runs returned unparseable JSON) | 13/13 | 13/13 | 8/13 |
| `continuation` without real replies (my first version) | 17/19 | 17/19 | 16/19 |
| `human_polish`: the held-out real reply after an LLM polish | 18/19 | 19/19 | 19/19 |

Each lineup shows the post and five replies: the four most-liked real replies and one candidate. The judge names the one it thinks an AI wrote. Chance is 4/19. Against `human_raw` under Opus, Fisher's exact test gives p=1.0 for `pipeline` and p=0.71 for `cont_real`; pooled over the three judges, p=0.80 and p=0.46. Every instruct-prompt arm is at p<0.0001. Generators: `claude-sonnet-5-5`, and `claude-opus-4-5-20251101` for continuation.

The three judges are named by the id sent to the gateway: `claude-opus-5-5`; `claude-fable-5-dd-artsa-6-tpg` (gateway reports gpt-6-astra), called the artsa alias below; and `claude-fable-5-dd-los-6.5-tpg` (gateway reports gpt-5.6-sol), called the los alias below. The only evidence of which model serves an alias is the `model` field in the gateway's response. The experiment records that field per call from now on; calls made before the probe on 2026-10-15 didn't record it.

The research thread ran the same comparison independently: 4 threads, 16 drafts per condition, judged by AUC. It found the same direction under both judges. Continuation scored AUC 0.44, the persona prompt 1.00 (`research/action.md`, finding 2). Two things made continuation work, and my first version had neither. The page needs the thread's real replies, and the replies need neutral handles and no like counts. Given only a fake header with counts ("1,894 likes"), the model writes a hype "top reply" and invents stats ("27/5/8 against the Lynx in Game 4").

Three readings matter for anyone building on this.

- **Selection doesn't fix an instruct prompt.** A random pick among five samples was caught exactly as often as the "least AI-sounding" pick (18/19 each under Opus). The pick only fooled the weakest judge. My first summary said `bestof` matched humans; that came from one pass of the los alias and doesn't survive Opus or the artsa alias.
- **Judges prefer the register they catch.** Asked "which reply gets more likes?", the los alias picked `remix` over the thread's most-liked real reply 14 times in 19. It picked `cont_real` only 2 times in 19. A best-of-N ranker built on that preference pulls drafts toward the AI register. The research run measured exactly that: selecting continuation drafts for likes raised AUC from 0.44 to 0.70.
- **The los alias is a weak detector.** It caught `remix` 12/19 where Opus and the artsa alias caught it 19/19. Treat Opus as the bar.

## The reply lane comes first, and timing beats wording

A two-follower account is seen mainly inside other people's threads. On 24 real threads (670 direct replies, `action/experiments/reply_dynamics.json`):

- Earlier replies get more likes. The mean within-thread Spearman correlation is -0.30 for minutes after the post, +0.16 for the replier's follower count, and +0.02 for length.
- The top 3 replies landed a median 28 minutes after the post. The rest landed at a median of 56 minutes. 53 of the 72 top-3 replies came within 60 minutes.
- 35 of the 72 top-3 replies came from accounts under 1,000 followers. A 114-follower account got 2,026 likes for "Soft launching that gambling sponsorship".

These numbers are correlational. The fixture holds X's first page of replies, which X ranks by engagement, so late low-like replies are under-sampled. X's ranking code backs the mechanism (context and observe research reports, reading `xai-org/x-algorithm`):

- A reply from an account the viewer doesn't follow never reaches For You.
- Mutes and reports weigh -58.8 and -234, against 0.66 for a like.
- Accounts labeled `llm_slop_user` lose non-follower reach for 7 days.

That last point is the platform's own reason for the AI-register veto. A slop label would cost the account its only growth channel.

The post lane gets less weight for a second reason. In the dev-tool data, within an account, quote posts and posts made within 2 hours of the account's last post are the clearest losers (Context worker, PR #18). Super Viral and Medium Viral posts convert viewers at nearly the same rate (1.22% against 1.17% engagement per view); Super Viral posts reached 4.6 times more people per follower. Distribution separates them, not wording. These are brand accounts, not a fan account, and quotes do reach the quoter's followers. Still, quote volume isn't free, so the quote caps are 1 per run and 2 per day, and posts stay at least 120 minutes apart (`post_spacing_minutes`).

## What decide does now

`decide()` in `harness/minds/__init__.py` keeps its signature. Rushi's `vetoed=` and the persona chorus are unchanged. It hands everything to `lanes.run`:

1. **Reply lane** (`lanes.reply_lane`). It reads `reply_targets` from every narrative, not only the ones the chorus saw, and orders them by engagement per hour, one per conversation root. Targets older than `reply_max_age_minutes` (30) abstain as `stale_target`. One planning call (`lanes.plan_targets`) decides per target whether to reply and with what intent. It writes no text. In live runs it abstained on off-mission posts, and on the Aliyah Boston surgery post as `mute_risk`.
2. **Writer** (`writer.continuations`). It builds a saved page from the post and up to 12 real replies. Observe attaches them as `thread`; otherwise `thread.fetch_replies` reads them through api.fxtwitter.com, read-only. The veto judge first scores the page once and drops replies it reads as bots (`page_max_p_ai`, research finding 10). `claude-opus-4-5-20251101` then continues the page 6 times, each with the prefill ending in `@<handle>:` and the stop sequence `["\n@"]`. A page with fewer than 5 replies, or a provider that can't prefill, falls back to the styled prompt on Opus 4.5 (`writer.styled_replies`; research AUC 0.79).
3. **Slop gate** (`gate.check`). It checks style rules, plus grounding rules against the post, the thread and the facts. It also rejects content-free drafts: no words at all (ellipses, emoji), or fewer than 3 words that share nothing with the post. That cutoff (`min_words_without_post_reference`) is uncalibrated, and it rejects some human-style one-word replies ("Queen") on purpose.
4. **Veto** (`veto.veto`). Opus 5.5 rates each draft against `mission/mission.md` and the context for four things: P(AI), unsupported claims, mission fit (sincerely cheering a rival or saying nothing is off mission; banter at a rival is fine), and mute or report risk (insults at a person, which X's `FosnrAbuseInsults` also demotes). It runs once in order and once reversed. A draft passes only if both passes clear it. A split counts as a veto. A judge answer missing the mission field counts as a veto too.
5. **Pick.** One survivor, chosen at random and seeded by the target URL. If Dhilan's `post_judge` is installed, its grounding score adds one more veto. Nothing passes means the target abstains as `judge_rejected`, and the candidates stay in the log.
6. **Post lane** (`lanes.post_lane`), in the order research finding 8 gives. First, a person's take from `action/takes.json`, verbatim. Else a caption of a real source, under 100 characters, that reads on its own as a reaction plus the fact it reacts to: a quote of an X post, or a news link credited the way a person would ("via ESPN", from the signal's publisher, or "via @espnW" for an X account). Opaque Google News redirects are never linked; when they're the only source, the lane abstains with `no_linkable_source`. Integration's live round 36 is why: the critic vetoed captions that linked a `news.google.com/rss/articles` redirect with "(via news.google.com)" and only made sense beside the article. Chorus text never ships. It becomes angles attached to an `ask_human` question. Chorus follows ship when the handle appears in the narratives.

Model routing looks at what the LLM object exposes, never at its class (`harness/minds/routing.py`). An Anthropic messages client means continuation and the Opus veto are available; `is_mock` means no network and no budget. A tracing wrapper that forwards attributes keeps both working. A fallback to a weaker route prints a warning once per run.

Every decision lands in `lanes.PENDING`. `cli.py` writes `out/decisions-run<id>.json` after `memory.save_run`: abstentions with reason codes, every candidate with its gate findings and veto verdicts, and questions for a person.

Daily caps and post spacing count only actions a person approved in the review dashboard (`out/approved-run<id>.json`), never recommendations. The harness can't see what Grok actually posted, and counting recommendations let one round block the next when nothing went out: Integration's run 31 hit `budget_exhausted` after run 22's single unapproved quote. Killing an approval frees the budget, and mock runs never spend it.

### Why the veto checks mission fit

Iterate promoted the reply lane on 27 real threads (Opus detection 1.000 to 0.370). But the drafts copy whatever register the thread has, so it shipped "Let's go Angel Reese, another championship 🏆" (cheering the rival, with an unsupported title), a bare "...", and "talking shit about a rookie". The old veto passed them; nothing checked whose side the account is on.

With the mission check, a live Opus run on those three strings vetoed all three. The first was off mission ("sincerely cheers rival Angel Reese"). The other two were off mission for saying nothing. The same run also vetoed two drafts we'd want: an on-voice jab ("six all-rookies and the fever didn't need one, we have caitlin") and a real fan reply, both on split verdicts between the two orders. So the mission check costs pass rate. Iterate is rerunning its instrument with the check frozen to measure how much.

### Writing from the account's side

Iterate's v3 retest reverted the first promotion of this lane. Continuing another team's thread made the model write as that team's fans (12 of 20 vetoes), and 10 more vetoes were for content-free lines. Two changes answer it. Neither has a measurement against real engagement.

- **The writer replies under a fan account's handle** (`framing_handle` in `mission/profiles/wnba/reply_framing.json`, `clarkfanaccount`). This handle only frames the drafts. The account the harness reads and measures is `[brand] x_handle` in the profile's `observe.toml`. The page is otherwise unchanged: neutral handles for everyone else, no like counts. A probe on 4 threads with 16 drafts per framing (`harness/minds/lab/framing_probe.py`) picked this framing. Neutral handles gave 2 lines of UI junk or empty text and 2 veto passes. The account's handle gave 0 and 4. Adding the display name gave 1 and 2, and it invented stats. A "signed in as" header with a bio was worse than all three: the model wrote reply-box text like "[reply on this post]" and "Draft my reply". On the Angel Reese thread, the neutral page wrote "A BORN WINNER ✨" and the account's handle wrote "Please change your name to Clark National Basketball Association".
- **The planning call labels each post** `own`, `rival` or `neutral`. On a rival post, `rival_thread_mode` decides: banter with the styled prompt (the default), abstain, or continue anyway.

The rerun on the same 19 threads (`harness/minds/lab/side_experiment.py`) reports lineup detection and veto pass rate together. All three arms faced one veto: the current prompt, on Opus, with mission fit, in two orders.

| Writer | Veto pass rate per draft | Threads shipped | Opus spotted the shipped draft |
| --- | --- | --- | --- |
| Current lane: plan, then continuation replying as the account | 15/48 (0.31) | 7/19 | 2/7 |
| Earlier lane: continuation with neutral handles | 23/114 (0.20) | 11/19 | 4/11 |
| Today's persona prompt | 0/19 (0.00) | 0/19 | none shipped |

Notes on the table:

- The persona prompt's drafts failed mostly as "reads as AI" (13 of 19). That's why the 0.00 here differs from Iterate's 0.556 for `main`'s persona prompt, which came from a veto without the AI-register check.
- Content-free lines failed the gate on 29% of the current lane's drafts, against 42% before.
- The planner abstained on 12 of 19 threads: 8 `no_angle`, 2 `mute_risk`, 1 `off_mission`, and 1 abstained before the plan. That included all 3 rival threads, so banter against continuation on rival posts is still unmeasured.
- Shipped examples: "Who knew a shoe launch could outshine a whole league💀" and "If only the @wnba loved Caitlin Clark as much as the @IndianaFever has."
- The veto also passed "Lmao she will never be as good as CC" under the espnW JuJu Watkins post. That one is borderline for "punch at takes, not people".

### What the live runs did

Two runs on live WNBA, Fever, Clark, espnW, Dream and Valkyries posts (`action/experiments/live_decide-*.json`). The second widened the age cutoff to 48 hours to have anything to answer.

- **First run.** The planner abstained on 7 targets and 3 were stale. On the one target it answered, the gate dropped "weird how caitlin averaged 5 steals in her head", and the veto caught two invented plays ("the clark no-look steal on chicago deserved a spot"). It shipped "fever defense next year though 👀".
- **Second run.** It shipped "valkyries moving at a pace that shouldn't be legal" and "strawberry but let's be honest i've eaten an entire bag before halftime". It also quoted the Boston surgery post with "rest up aliyah, take all the time you need".
- **The cost of continuation.** The one draft written by continuation copied its thread's register: "THESE WOMEN KILLED IT THIS SEASON🔥🔥🔥🔥". It's human-sounding and bland. Continuation inherits whatever the thread does, including insults, which the veto's risk check exists for.

## Ranking of the approaches

| # | Approach | Verdict | Evidence |
| --- | --- | --- | --- |
| 1 | Pick the target and the moment: fresh posts, early replies, abstain otherwise | Built | Reply timing above; X's ranker weights |
| 2 | Continue the real thread on a prefill model | Built, primary writer | `pipeline` 4/18 and `cont_real` 6/19 against `human_raw` 4/19 (Opus); research AUC 0.44 |
| 3 | Judge as a veto, two orders, split = veto | Built | Judges prefer the AI register (14/19 for `remix`); research: likes-selection raised AUC 0.44 to 0.70 |
| 4 | Deterministic gate | Built, necessary but not sufficient | Catches invented numbers, names, handles and links (live runs); passes drafts Opus flags (research finding 4) |
| 5 | People supply the take; the harness never rewrites it | Built (`action/takes.json`, `ask_human`) | `human_polish` 18/19 against 4/19 |
| 6 | Abstain by default, with per-run and daily budgets | Built | The budget numbers have no measurement behind them (see `settings.py`) |

Rejected, with the reason:

- **The persona JSON call writing final text.** Caught 19/19 by every judge. It now only plans: which target, reply or abstain, and the intent.
- **Best-of-N ranked by an LLM's quality judgment, including Dhilan's `rank_candidates`.** The pick didn't beat a random pick. `bestof_rubric` was caught 13/13. On the hard question (adjacent real replies among the top 5), judges barely beat a coin; see the calibration section. Dhilan's judge stays as a grounding veto only.
- **Exemplar remix and "constrained" style prompts.** They help against the los alias but are caught 18 to 19 of 19 by Opus.
- **Base-model continuation through a non-chat model.** The gateway has none. `grok-4.6` returns 401 ("Saved Cursor API key is locked"). Claude 5.5 models reject prefill. `claude-opus-4-5-20251101` is the only route, and the lane falls back when it's missing.
- **Verbalized sampling.** It adds variety, not human-likeness (research finding 9: AUC 0.88, the same as the plain styled prompt). It's worth trying for angles, not for final text.
- **Meme-format filling.** Next in the post lane's order; not built tonight.

## The judge calibration

Best-of-N only works if the judge can tell a good reply from a slightly better one. On the easy question, most-liked against least-liked real reply in a thread, the los alias picked the more-liked one 31 times in 40, with no position bias (picked A 19 times in 40). On the question best-of-N actually faces, adjacent ranks among the top 5 asked in both orders, Opus picked the more-liked reply 78 times in 138 (57%) and the los alias (`claude-fable-5-dd-los-6.5-tpg`, gateway reports gpt-5.6-sol) 76 times in 138 (55%). Each gave the same answer in both orders on only about half the pairs (34 of 69 and 32 of 69). That is under the 60% the research report set as the bar, so a best-of-N ranker would be choosing close to at random among plausible drafts. The lane picks at random on purpose and spends the judge on vetoes. The research run found Spearman 0.15 (Opus) and about 0 (the los alias) between judge score and real likes among the top 6 replies, with the sign flipping when only the rest of the lineup changed.

## Live facts let the gate accept names people bring from outside the thread

The gate's grounding rules reject a name, number or handle that the post, the thread and the facts block don't contain. Real fans bring names and numbers from outside, so these rules alone reject 18.7% of the 670 real replies in `action/fixtures/x_threads.json`. The gate as a whole now rejects 35.7% of them. The 29% this page used to quote came from the gate before the content-free rule, and `action/experiments/gate_calibration.json` still holds that run.

`harness/facts/` keeps dated facts in memory's `Fact` kind, each with `as_of` and `valid_until`. A fact is live from `as_of` up to, but not including, `valid_until`. `python -m harness.facts refresh` writes them to the profile's memory store:

- **wnba:** ESPN's keyless site API (`harness/facts/espn.py`). It writes each team's roster, its last finished game (the result and one line per player who played), its next game, and the league's injury list.
- **devtools:** the recent posts of each account in `[reply_targets]` (`harness/facts/posts.py`). The thread under a target becomes a fact at draft time, covering replies past the page's dozen.

The reply lane loads the live facts once per run. The gate then counts a capitalized name as grounded when it appears as a whole word in a live fact. A number has to pass a stricter test. A live fact about something the draft, the post or the target's handle names must hold the same number with the same unit, meaning the word after it with stat synonyms folded together. A hyphenated pair such as a score or a record matches without a unit. The planning call sees up to five facts about each post's author or the names in it. The writer's page never sees them, because a page header with counts made the model write hype and invent stats (see above).

Measured with `uv run python -m harness.minds.lab.fact_grounding --invent` (report in `action/experiments/fact_grounding.json`). Facts for each thread are the ones live when its post went out, so no game that ended later counts. Intervals resample threads, 2,000 draws.

| Set | Gate rejects, without facts | With facts | Change, 95% CI |
| --- | --- | --- | --- |
| wnba real replies (670, 24 threads) | 35.7% | 32.5% | −3.1 points [−4.6, −1.8] |
| the same, last 7 days only (392, 16 threads) | 35.5% | 31.9% | −3.6 [−5.8, −1.5] |
| the same, plus today's rosters | 35.5% | 31.1% | −4.3 [−6.8, −2.0] |
| devtools real replies (6,663, 315 threads) | 28.2% | 27.1% | −1.1 [−1.4, −0.8] |

| Invented-stat drafts (claude-sonnet-5-5, from the post alone, told to make a number up) | Caught without facts | With facts | Let through by facts |
| --- | --- | --- | --- |
| wnba, design batch (143) | 87.4% | 86.0% | 2: Clark's 14 assists the day before (true), "6 threes in the gold medal game" (true for a WNBA game, not that one) |
| wnba, held-out batch (144) | 84.0% | 82.6% | 2: the Dream's 30-14 regular season (true), Canada's 11 assists "in game one" (she had 11 in two recent games, neither of them a game one) |
| devtools, design batch (235) | 88.5% | 88.5% | 0 |
| devtools, held-out batch (239) | 87.0% | 86.2% | 2, both coincidences: "40%" of a quota matched "30–40% cheaper", and a 256k context window matched another model's |

Readings:

- **Almost all of the gain is names.** On the WNBA set, facts ground no number the gate used to reject, because fans write "6-for-18" and "27 and 10", not "27 points". On the dev-tool set, number findings fell by 28 of 592 and name findings by 160 of 890. A first version matched a number by value alone. It let 9 of 143 invented WNBA stats through ("dropping 31 tonight" matched 31 minutes, "27-17" matched the opponent's record) and 15 of 235 dev-tool numbers, so the unit rule replaced it. Its numbers are in the report under `first_rule_number_without_unit`.
- **About 4 in 761 invented numbers now pass that a person would call wrong.** The held-out batches were written after the rule was fixed, so they test it rather than tune it.
- **Many remaining name rejections aren't names.** They include weekdays and months (Saturday, January), ordinary words capitalized for emphasis (Kidding, Queen, Phenomenal) and misspellings (Caitlyn, Valkeries). A list of weekday and month names would clear a few; nothing here measures that.
- **Rosters were measured only on recent threads.** ESPN serves today's roster only, so the main row leaves rosters out. The roster row dates today's rosters back to threads from the last week, which assumes no roster moved during the playoffs.
- **The veto doesn't see the facts.** A draft the facts ground can still be vetoed as "unsupported". Giving the veto the same facts is the obvious next step, and it is unmeasured.
- **The watcher's hot path doesn't pass facts yet.** `lanes.answer_target` takes them as `live_facts=`; without it the gate grounds on context alone, as before.

## The typed actions

Each opportunity resolves to one JSON tool call, checked by `schema.parse_call` against its lane and against what was offered. Errors name the tool, the field and the reason. For example: `reply.target_url 'https://www.espn.com/x' was not offered; offered: [...]` or `tool 'post' is not allowed in the reply lane`. Unknown fields are rejected, never dropped.

| Lane | Tools | What it needs |
| --- | --- | --- |
| `reply` | `reply`, `quote`, `follow`, `abstain` | A live X post from `narrative.reply_targets`. URLs and handles must be among those offered. |
| `post` | `post`, `quote`, `mention`, `follow`, `ask_human`, `abstain` | A story, a real source to caption, or a person's take. Handles must appear in the narratives. |

`abstain` takes a reason from `schema.ABSTAIN_REASONS`. Each code names the stage at fault, so eval can assign blame: `no_target`, `stale_target`, `no_angle`, `gate_rejected`, `judge_rejected`, `budget_exhausted`, `unverifiable`, `off_mission`, `mute_risk`, `no_source`, `no_linkable_source`. `schema.parse_plan` checks the planning call the same way. `schema.json_schema(lane)` emits JSON Schema for prompts and for other workers.

`Action` in `harness/models.py` gained three optional fields: `lane`, `target_text` and `draft_meta`. `draft_meta["target"]` is the reply target as Observe sent it. `draft_meta["judge"]` holds the veto verdicts in both orders, the batch totals, and the pick rule. `gate.Finding` carries `start` and `end` offsets for the operator UI.

## Knobs

Every number the stage uses sits in `settings.ActionConfig`, with a comment saying where it came from or that nothing measured it. Override it per call with `decide(..., cfg=...)`, or with a JSON file at `HARNESS_ACTION_CONFIG`. The `sam/iterate` loop tunes these against the lineup experiment. The objective is Opus detection rate plus abstention rate, guarded by the veto.

## How this fits other people's work

- **Dhilan** (`harness/post_judge/`, PR #5). Its grounding criterion is an optional veto (`veto.PostJudgeGrounding`). Its overall score and `rank_candidates` aren't used. Its rubric rewards clarity and specificity, the register the lineups catch, and its default references are dev-tool posts.
- **Aarav** (`aarav/self-improving`). `tournament.py` ranks drafts across targets by pairwise "which spreads further". `hooks.py` swaps in LLM-rewritten openings that win that comparison. Both rest on a pairwise preference that, in the data above, prefers AI-polished text. The two branches also both rewrite `harness/minds/__init__.py`. Mine keeps that diff small and puts the work in new files.
- **Rushi** (PR #6). Narrative targeting and the veto-retry loop stay, but a retry only runs when the critic vetoed a revisable persona draft. Persona text no longer ships, so in practice `decide(vetoed=...)` returns nothing and `cli.run`'s loop ends. Before this, the retry reran the caption on the same source and could ship a second caption in one run. A revising call never recaptions or re-ships a take.
- **Observe** (PR #10). Reply targets carry `thread`, the 15 most-liked replies, which the writer uses directly.
- **Eval** owns `qualify`, which still ranks whatever decide ships.

## Gaps and open questions

- **Fresh targets are rare.** Tonight's live targets were 90 minutes to 2 days old, so the 30-minute cutoff abstains on nearly everything. The lane needs Observe polling big accounts every few minutes, or a looser cutoff. Sam's call.
- **Continuation copies the thread.** In a hostile thread, the drafts are hostile, and the veto's risk check is the only guard.
- **Real engagement is unmeasured.** Every number here is a judge's prediction or a correlation. Grok's own bar is unknown, and people detect AI on short posts at close to chance, so "Opus can't tell" is a stricter bar than a fan applies.
- **The facts block is soft.** It is the mission plus orient's summaries, which are LLM text. A name that appears only in a wrong summary passes the grounding rule. Live facts (above) are a separate, sourced input to the gate.
- **Grounding rules cost real replies.** With only the post and thread as context, the grounding rules reject 18.7% of real replies, mostly names and numbers people bring from outside. Live facts bring that to 15.5% (see "Live facts let the gate accept names"). That's the price of never shipping an unverifiable fact.
- **One model holds up the writer.** If `claude-opus-4-5-20251101` leaves the gateway, every reply falls back to the styled prompt, which scored AUC 0.79 in the research run, where 0.5 means a judge can't tell it from people.
- **The account isn't verified.** Verified replies rank higher: mean rank 32.8 against 59.8 (arXiv 2602.03090, correlational).
- **The `pipeline` arm ran before the page bot filter existed.** Its numbers measure the lane without it.

## Reproduce

```sh
uv run python -m harness.minds.lab.fetch_x_threads IndianaFever WNBA CaitlinClark22 espnW AtlantaDream valkyries --per-account 4
uv run python -m harness.minds.lab.reply_dynamics
uv run python -m harness.minds.lab.gate_calibration
uv run python -m harness.minds.lab.fact_grounding          # add --invent for the invented-stat drafts (LLM calls)
HARNESS_PROVIDER=anthropic uv run python -m harness.facts refresh   # live facts for the active profile
HARNESS_PROVIDER=anthropic uv run python -m harness.minds.lab.reply_experiment --threads 19
HARNESS_PROVIDER=anthropic uv run python -m harness.minds.lab.live_decide CaitlinClark22 IndianaFever espnW --max-age 30
uv run python -m pytest tests
```

`reply_experiment` caches every call in `action/experiments/reply_experiment_raw.json`, so a rerun only pays for new arms.
