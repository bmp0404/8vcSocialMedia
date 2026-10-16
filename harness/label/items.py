"""Write `data/labels/items.jsonl` from the corpus, in the label-store contract shape.

Contract keys: item_id, kind, status_id, url, author, author_followers, text, created_at, parent_id,
metrics, source. Everything else the annotator prompt or a later analysis needs sits under `context`,
so the contract keys stay exactly as agreed with the UI batch.
"""

import json
from pathlib import Path

from harness.corpus import Corpus
from harness.label.schema import LABELS_DIR

ITEMS_PATH = LABELS_DIR / "items.jsonl"


def item_row(item, corpus: Corpus) -> dict:
    parent = corpus.get(item.parent_id) if item.parent_id else None
    minutes = (item.created_at - parent.created_at).total_seconds() / 60 if parent else None
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "status_id": item.status_id,
        "url": item.url,
        "author": item.author,
        "author_followers": item.author_followers,
        "text": item.text,
        "created_at": item.created_at.isoformat(),
        "parent_id": f"x:{item.parent_id}" if item.parent_id else None,
        "metrics": item.metrics,
        "source": "+".join(item.sources),
        "context": {
            "author_verified": item.author_verified,
            "author_bio": item.author_description,
            "parent_author": item.parent_author,
            "minutes_after_parent": round(minutes, 2) if minutes is not None else None,
            "media_types": item.media_types,
            "n_links": item.n_links,
            "n_mentions": item.n_mentions,
            "quoted_id": f"x:{item.quoted_id}" if item.quoted_id else None,
            "thread_root": f"x:{item.thread_root}" if item.thread_root else None,
            "thread_coverage": item.thread_coverage,
            "reposted_by": item.reposted_by,
            "in_dataset": item.in_dataset,
            "is_thread_continuation": item.is_thread_continuation,
            "boost_suspect": item.boost_suspect,
            "mature": item.mature,
            "lang": item.lang,
        },
    }


def export_items(corpus: Corpus, path: Path = ITEMS_PATH) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for item in corpus.items:
            f.write(json.dumps(item_row(item, corpus), ensure_ascii=False) + "\n")
    return len(corpus.items)
