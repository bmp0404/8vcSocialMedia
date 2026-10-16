# Run the post lab demo

Post lab lets you write a post, add its audience and supporting facts, and request a structured evaluation. Results include five rubric scores, comparisons with successful and poor-performing posts, and revision suggestions. Scores describe writing quality, not expected engagement.

The deployed UI is at https://8vc-post-lab-dhilan.fly.dev. Open the URL directly; no access code is required. Write your post and select **Evaluate post**. For replies, include the parent post in supporting context. No account handle is required.

The Fly app holds a public submission queue with authenticated worker endpoints. A worker on the developer's machine pulls jobs and calls the BAML Claude Code judge. Keep that machine awake and the worker running. The UI reports when the worker is offline. Claude credentials and the reference CSV remain on the worker machine; submitted drafts, results, and selected reference text pass through Fly.

## Start the worker

Complete the [judge setup](../../eval/README.md), including SDK generation and Claude Code authentication. Set these variables in your local, ignored `.env` file:

```dotenv
DEMO_URL=https://8vc-post-lab-dhilan.fly.dev
DEMO_WORKER_TOKEN=<worker token configured on Fly>
HARNESS_JUDGE_PROVIDER=claude_code
HARNESS_JUDGE_MODEL=sonnet
```

From the repository root, start the worker and leave it running:

```sh
uv run python -u -m engine.demo.worker
```

Stop a foreground worker with Ctrl-C. This checkout's launched demo worker records its PID in `out/demo-worker.pid` and logs in `out/demo-worker.log`. These files are ignored by Git.

## Deploy the UI

The app requires a random `DEMO_WORKER_TOKEN` secret of at least 24 characters. Import it with `fly secrets import`; do not commit it. Anyone with the app URL can submit a post. Results are retrieved by their unguessable job IDs. Worker endpoints still require the worker token.

Deploy from this directory:

```sh
fly deploy --config fly.toml --remote-only --ha=false
```

The image contains only the server and HTML. The configuration runs one shared CPU with 256 MB of RAM. Keep one machine: SQLite jobs live on its ephemeral filesystem and disappear when the machine is replaced. Jobs expire after 24 hours during queue operations. The queue accepts at most 20 pending evaluations. This is a team demo without durable history or separate user accounts.

For local UI development, export the worker token and run `uv run python -m engine.demo.server` from the repository root. The default port is 8080. Point `DEMO_URL` at that server when testing with the worker.

## Verified behavior

The Fly UI completed a browser-to-Claude evaluation of the built-in CLI caching example: 72/100, `Revise`, medium confidence, 19.7 seconds. This was a synthetic post with supplied release notes. Historical references came from `data/devtools_virality_30d.csv`.

The tests cover public submission and worker authentication, queue leases, failed evaluations, and input validation. Run them with `uv run python -m unittest discover -s tests`.

## Harness dashboard

The root URL opens the dashboard. The original post evaluator is at `/eval`.

- **Overview** runs one actual engine round with the mock provider, then displays stage timings, narratives, and draft actions. Mock scores and drafts are labeled.
- **Ingestion** runs the existing sample or Google News source adapter through a separate local bridge. Google News uses the active profile's `observe.toml` queries (`mission/profiles/<name>/`). JSON imports accept up to 100 signal objects, validate their fields, and deduplicate normalized text. No feed result is fabricated when a source fails.
- **Memory** saves human procedures and observations, plus an episode for each completed mock round. Notes feed the next round's memory input. This is demo state, separate from `harness/memstore` and the production lessons file. Human notes stay unverified and mock scores do not become learned outcomes.
- **Post validation** checks required fields, plain character count, `[VERIFY]` markers, missing parent context, and quantified claims requiring review. These checks do not establish factual accuracy or grant posting approval. The same view can send the draft to the existing Claude judge.

Each browser gets a random workspace identifier in local storage. Dashboard state and task results are scoped to that identifier. There is no account login or shared workspace browser. Keep the identifier private. State expires after 24 hours and uses the same ephemeral Fly filesystem as evaluations.

Start the separate dashboard bridge from the repository root with the same `DEMO_URL` and `DEMO_WORKER_TOKEN` used by the judge worker:

```sh
uv run python -u -m engine.demo.bridge
```

The bridge allows only sample ingestion, configured Google News ingestion, and mock engine rounds. Each round gets a temporary database and receives a snapshot of that workspace's signals and notes. It removes local temporary artifacts after returning the result to Fly. It never changes production memory or publishes an action.

The launched bridge writes its PID to `out/dashboard-bridge.pid` and logs to `out/dashboard-bridge.log`. The existing judge worker is independent; leave both running for all dashboard features. The bridge deliberately does not import the BAML judge.

Use `uv run pytest -q` for the complete suite, including the newly merged observation tests. `node --check engine/demo/dashboard.js` checks the browser script syntax.
