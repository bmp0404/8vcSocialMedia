"""Select reproducible historical comparisons without leaking the candidate."""

import csv
import hashlib
import re
from datetime import date
from pathlib import Path

from baml_sdk import Performance, ReferencePost

LABELS = {"Super Viral": Performance.Successful, "Deadweight": Performance.Poor}
REQUIRED = {"Category", "Account", "Post Content", "Engagement", "Post URL", "Date", "Media"}


def normalized_text(text: str) -> str:
    return " ".join(text.casefold().split())


def load_references(path: Path) -> list[ReferencePost]:
    posts = []
    seen_ids = set()
    seen_texts = set()
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        missing = REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns: {', '.join(sorted(missing))}")
        for line, row in enumerate(reader, 2):
            if row["Category"] not in LABELS:
                continue
            if any(row.get(field) is None for field in REQUIRED):
                raise ValueError(f"{path}: missing values at CSV record {line}")
            text = row["Post Content"].strip()
            post_id = row["Post URL"].strip()
            try:
                engagement = int(row["Engagement"])
                date.fromisoformat(row["Date"])
                if not text or not post_id or engagement < 0:
                    raise ValueError("text and URL must be present, engagement must be nonnegative")
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}: invalid reference at CSV record {line}: {exc}") from exc
            key = normalized_text(text)
            if post_id in seen_ids or key in seen_texts:
                continue
            seen_ids.add(post_id)
            seen_texts.add(key)
            posts.append(ReferencePost(
                id=post_id, text=text, account=row["Account"],
                performance=LABELS[row["Category"]], category=row["Category"],
                engagement=engagement, media=row["Media"], date=row["Date"],
            ))
    return posts


def select_references(
    posts: list[ReferencePost], candidate_text: str, *, per_class: int = 3,
    before: date | None = None, exclude_ids: set[str] | None = None,
) -> list[ReferencePost]:
    """Balance labels, prefer lexical relevance, and cap each account at one per class."""
    if not 1 <= per_class <= 10:
        raise ValueError("per_class must be between 1 and 10")
    candidate_key = normalized_text(candidate_text)
    if not candidate_key:
        raise ValueError("candidate text must not be empty")
    words = set(re.findall(r"\w{3,}", candidate_key))
    eligible = [p for p in posts if p.id not in (exclude_ids or set())
                and normalized_text(p.text) != candidate_key
                and (before is None or date.fromisoformat(p.date) < before)]
    eligible.sort(key=lambda p: (
        -len(words & set(re.findall(r"\w{3,}", p.text.casefold()))),
        hashlib.sha256(p.id.encode()).hexdigest(),
    ))
    selected = []
    for label in Performance:
        accounts = set()
        group = []
        for post in eligible:
            if post.performance != label or post.account.casefold() in accounts:
                continue
            group.append(post)
            accounts.add(post.account.casefold())
            if len(group) == per_class:
                break
        if len(group) < per_class:
            raise ValueError(f"Need {per_class} distinct-account {label.value} references after exclusions; got {len(group)}")
        selected.extend(group)
    # Interleave labels instead of presenting all successful examples first.
    return [p for pair in zip(selected[:per_class], selected[per_class:]) for p in pair]
