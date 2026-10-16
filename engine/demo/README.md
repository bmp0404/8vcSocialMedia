# Run the post lab demo

Post lab lets you write a post, add its audience and supporting facts, and request a structured evaluation. Results include five rubric scores, comparisons with successful and poor-performing posts, and revision suggestions. Scores describe writing quality, not expected engagement.

The deployed UI is at https://8vc-post-lab-dhilan.fly.dev. Open the private team link or enter the team access code. Write your post and select **Evaluate post**. For replies, include the parent post in supporting context. No account handle is required.

The Fly app holds an authenticated queue. A worker on the developer's machine pulls jobs and calls the BAML Claude Code judge. Keep that machine awake and the worker running. The UI reports when the worker is offline. Claude credentials and the reference CSV remain on the worker machine; submitted drafts, results, and selected reference text pass through Fly.

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

Stop a foreground worker with Ctrl-C. This checkout's launched demo worker records its PID in `out/demo-worker.pid` and logs in `out/demo-worker.log`. The private team link is in `out/demo-access.txt`. These files are ignored by Git.

## Deploy the UI

The app requires distinct `DEMO_ACCESS_TOKEN` and `DEMO_WORKER_TOKEN` secrets of at least 24 characters. Generate random tokens and import them with `fly secrets import`; do not commit them. Share only the access token with testers. A team link uses `/#access=<access token>`; the UI removes the fragment and retains the token in session storage.

Deploy from this directory:

```sh
fly deploy --config fly.toml --remote-only --ha=false
```

The image contains only the server and HTML. The configuration runs one shared CPU with 256 MB of RAM. Keep one machine: SQLite jobs live on its ephemeral filesystem and disappear when the machine is replaced. Jobs expire after 24 hours during queue operations. The queue accepts at most 20 pending evaluations. This is a team demo without durable history or separate user accounts.

For local UI development, export both tokens and run `uv run python -m engine.demo.server` from the repository root. The default port is 8080. Point `DEMO_URL` at that server when testing with the worker.

## Verified behavior

The Fly UI completed a browser-to-Claude evaluation of the built-in CLI caching example: 72/100, `Revise`, medium confidence, 19.7 seconds. This was a synthetic post with supplied release notes. Historical references came from `data/devtools_virality_30d.csv`.

The tests cover token-role separation, queue leases, failed evaluations, and input validation. Run them with `uv run python -m unittest discover -s tests`.
