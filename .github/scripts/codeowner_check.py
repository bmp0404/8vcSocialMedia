"""Fail a pull request that changes someone else's files until that owner approves.

Owners come from `.github/CODEOWNERS` on the base branch, so a pull request can't grant itself
ownership. A file is cleared when one of these holds:
- no CODEOWNERS line matches it,
- the author is its only owner, or
- an owner other than the author approved the pull request.

Supported patterns: `*`, `**`, `*.ext`, `/dir/` (rooted), `dir/` (any depth), `/path/file`, and
`dir/*` (direct children only). The last matching line wins, as in GitHub. Only `@user` owners
count; teams and emails are ignored.
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path


def parse_codeowners(text: str) -> list[tuple[re.Pattern, set[str]]]:
    rules = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        users = {o[1:].lower() for o in owners if o.startswith("@") and "/" not in o}
        rules.append((_compile(pattern), users))
    return rules


def _compile(pattern: str) -> re.Pattern:
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    direct_only = pattern.endswith("/*")
    body = pattern.strip("/")
    regex = ""
    i = 0
    while i < len(body):
        if body.startswith("**/", i):
            regex += "(?:.*/)?"
            i += 3
        elif body.startswith("**", i):
            regex += ".*"
            i += 2
        elif body[i] == "*":
            regex += "[^/]*"
            i += 1
        else:
            regex += re.escape(body[i])
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    # A directory or plain name also covers everything under it; `dir/*` covers direct children only.
    suffix = "" if direct_only else "(?:/.*)?"
    return re.compile(f"{prefix}{regex}{suffix}")


def owners_for(path: str, rules: list[tuple[re.Pattern, set[str]]]) -> set[str]:
    owners: set[str] = set()
    for regex, users in rules:
        if regex.fullmatch(path):
            owners = users
    return owners


def pending(files: list[str], rules, author: str, approvers: set[str]) -> dict[str, list[str]]:
    """Map each file still waiting on an owner to the owners who could clear it."""
    author = author.lower()
    waiting = {}
    for path in files:
        others = owners_for(path, rules) - {author}
        if others and not others & approvers:
            waiting[path] = sorted(others)
    return waiting


def _get_all(url: str, token: str) -> list[dict]:
    items, page = [], 1
    while True:
        req = urllib.request.Request(
            f"{url}?per_page=100&page={page}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req) as resp:
            batch = json.load(resp)
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def main() -> int:
    codeowners = Path(".github/CODEOWNERS")
    if not codeowners.exists():
        print("No .github/CODEOWNERS on the base branch, so every change is allowed.")
        return 0
    rules = parse_codeowners(codeowners.read_text(encoding="utf-8"))

    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    pr = event["pull_request"]
    api = f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}/pulls/{pr['number']}"
    token = os.environ["GITHUB_TOKEN"]

    files = []
    for f in _get_all(f"{api}/files", token):
        files.append(f["filename"])
        if f.get("previous_filename"):
            files.append(f["previous_filename"])

    # A reviewer's latest approve, request-changes or dismissal decides; comments don't change it.
    latest = {}
    for review in _get_all(f"{api}/reviews", token):
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[review["user"]["login"].lower()] = review["state"]
    approvers = {user for user, state in latest.items() if state == "APPROVED"}

    waiting = pending(files, rules, pr["user"]["login"], approvers)
    if not waiting:
        print(f"All {len(files)} changed files are cleared.")
        return 0

    lines = ["Waiting on code owner approval:", ""]
    lines += [f"- `{path}`: approval from @{' or @'.join(owners)}" for path, owners in sorted(waiting.items())]
    report = "\n".join(lines)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as out:
            out.write(report + "\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
