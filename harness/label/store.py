"""The label store: items, LLM labels and human labels under `data/labels/`, read through one API.

Files (the contract the labeling UI is built against):

- `schema.json`: `{"version", "fields": [{"name", "kind", "values", "question", "applies_to"}]}`.
- `items.jsonl`: one item per line, keyed by `item_id` (`x:<status_id>`).
- `llm/<run_id>.jsonl`: one annotator answer per line: `item_id`, `run_id`, `annotator` (`model`,
  `response_model`, `prompt_sha`), `labels`, `rationale`, `usage`, `at`, `error`. The committed
  `rationale` is empty; the text sits in the gitignored `rationales/<run_id>.jsonl` and `llm_labels`
  joins it back where this machine has it.
- `human.jsonl`: append-only human labels: `item_id`, `labeler`, `labels` (may be partial), `note`, `at`.
- `audit.json`: the frozen audit set behind `queue("audit")`, written by `build_audit`.

Every model with a file in `llm/` votes, one vote per model (its latest answer without an error).
A human label never replaces an LLM vote: `consensus` reports both side by side.
"""

import json
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from harness.label.schema import LABELS_DIR

QUEUE_MODES = ("disagree", "unlabeled", "all", "audit")
AUDIT_SIZE = 200
AUDIT_SEED = 20261016


class Store:
    def __init__(self, root: Path = LABELS_DIR):
        self.root = Path(root)
        self._items: dict | None = None
        self._llm: dict | None = None
        self._human: dict | None = None
        self._human_stamp = None
        self._schema: dict | None = None
        self._schema_stamp = None

    # ---- reading -------------------------------------------------------------------------------

    def load_schema(self) -> dict:
        """schema.json as a fresh dict; the store re-reads the file whenever it changes."""
        return json.loads(json.dumps(self._schema_cached()))

    def _schema_cached(self) -> dict:
        path = self.root / "schema.json"
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        if stamp != self._schema_stamp:
            schema = json.loads(path.read_text(encoding="utf-8"))
            for f in schema["fields"]:
                if f["kind"] not in ("enum", "ordinal", "text"):
                    raise ValueError(f"schema field {f['name']} has unknown kind {f['kind']!r}")
            self._schema, self._schema_stamp = schema, stamp
        return self._schema

    def _fields(self, kind: str) -> dict:
        return {f["name"]: f for f in self._schema_cached()["fields"] if kind in f["applies_to"]}

    def _load_items(self) -> dict:
        if self._items is None:
            with (self.root / "items.jsonl").open(encoding="utf-8") as f:
                self._items = {row["item_id"]: row for row in map(json.loads, f)}
        return self._items

    def items(self, ids=None) -> list[dict]:
        """All items in file order, or the given IDs in the given order. An unknown ID is a KeyError."""
        by_id = self._load_items()
        if ids is None:
            return list(by_id.values())
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise KeyError(f"unknown item IDs: {missing[:5]}")
        return [by_id[i] for i in ids]

    def _load_llm(self) -> dict:
        if self._llm is None:
            rows = defaultdict(list)
            for path in sorted((self.root / "llm").glob("*.jsonl")):
                with path.open(encoding="utf-8") as f:
                    for row in map(json.loads, f):
                        rows[row["item_id"]].append(row)
            # Rationales live in the gitignored rationales/ folder (they can quote the item); join them
            # back by (item, run, time) where this machine has them.
            why = {}
            for path in sorted((self.root / "rationales").glob("*.jsonl")):
                with path.open(encoding="utf-8") as f:
                    for r in map(json.loads, f):
                        why[(r["item_id"], r["run_id"], r["at"])] = r["rationale"]
            for v in rows.values():
                for r in v:
                    if not r.get("rationale"):
                        r["rationale"] = why.get((r["item_id"], r["run_id"], r["at"]), "")
                v.sort(key=lambda r: r["at"])
            self._llm = dict(rows)
        return self._llm

    def llm_labels(self, item_id: str) -> list[dict]:
        """Every LLM answer for the item, oldest first, including answers that ended in an error."""
        self._require(item_id)
        return list(self._load_llm().get(item_id, []))

    def _load_human(self) -> dict:
        path = self.root / "human.jsonl"
        stamp = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
        if self._human is None or stamp != self._human_stamp:
            rows = defaultdict(list)
            if path.exists():
                with path.open(encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            row = json.loads(line)
                            rows[row["item_id"]].append(row)
            self._human, self._human_stamp = dict(rows), stamp
        return self._human

    def human_labels(self, item_id: str) -> list[dict]:
        self._require(item_id)
        return list(self._load_human().get(item_id, []))

    def _require(self, item_id: str) -> dict:
        item = self._load_items().get(item_id)
        if item is None:
            raise KeyError(f"unknown item ID {item_id!r}")
        return item

    # ---- consensus -----------------------------------------------------------------------------

    def votes(self, item_id: str) -> dict:
        """Model -> its latest error-free labels for the item."""
        latest = {}
        for row in self.llm_labels(item_id):
            if not row.get("error"):
                latest[row["annotator"]["model"]] = row["labels"]
        return latest

    def consensus(self, item_id: str) -> dict:
        """Field name -> {"value", "agreement", "votes", "human"} for every field that applies to the item.

        value: the strict plurality of the LLM votes, or None on a tie or with no votes.
        agreement: share of votes equal to the plurality value (None with no votes).
        votes: model -> value. human: {"value", "labeler", "at"} from the latest human label that set
        the field, or None. The human value is reported next to the LLM value, never instead of it.
        """
        item = self._require(item_id)
        per_model = self.votes(item_id)
        humans = self.human_labels(item_id)
        out = {}
        for name in self._fields(item["kind"]):
            votes = {m: labels[name] for m, labels in sorted(per_model.items()) if name in labels}
            value = agreement = None
            if votes:
                counts = Counter(json.dumps(v) for v in votes.values()).most_common()
                top, n = counts[0]
                agreement = n / len(votes)
                if len(counts) == 1 or counts[1][1] < n:
                    value = json.loads(top)
            human = None
            for row in humans:  # oldest first; the latest one that set the field wins
                if name in row["labels"]:
                    human = {"value": row["labels"][name], "labeler": row["labeler"], "at": row["at"]}
            out[name] = {"value": value, "agreement": agreement, "votes": votes, "human": human}
        return out

    def disagreements(self, item_id: str) -> list[str]:
        """Fields where two or more LLM votes differ."""
        return [name for name, c in self.consensus(item_id).items()
                if len(c["votes"]) >= 2 and len({json.dumps(v) for v in c["votes"].values()}) > 1]

    # ---- queues --------------------------------------------------------------------------------

    def _entry(self, item: dict, consensus: dict) -> dict:
        disagree = [n for n, c in consensus.items()
                    if len(c["votes"]) >= 2 and len({json.dumps(v) for v in c["votes"].values()}) > 1]
        return {"item": item, "item_id": item["item_id"], "n_disagree": len(disagree), "disagree_fields": disagree,
                "llm_labeled": any(c["votes"] for c in consensus.values()),
                "human_labeled": any(c["human"] is not None for c in consensus.values())}

    def queue(self, mode: str, limit: int = 50, offset: int = 0) -> list[dict]:
        """Items for a person to label, as entries with the item, its disagreeing fields and flags.

        disagree: items whose LLM votes differ on at least one field that no human has settled yet,
          most disagreeing fields first.
        unlabeled: items with no human label, disagreements first.
        all: every item, disagreements first.
        audit: the frozen stratified audit set (`build_audit`), in its stored order, minus items a
          human has labeled.
        Ties keep file order, so pages are stable while nobody labels.
        """
        if mode not in QUEUE_MODES:
            raise ValueError(f"unknown queue mode {mode!r}; use one of {QUEUE_MODES}")
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        if mode == "audit":
            ids = self.audit_ids()
            entries = [self._entry(it, self.consensus(it["item_id"])) for it in self.items(ids)]
            entries = [e for e in entries if not e["human_labeled"]]
            return entries[offset: offset + limit]
        entries = []
        for item in self.items():
            c = self.consensus(item["item_id"])
            e = self._entry(item, c)
            if mode == "disagree":
                open_fields = [n for n in e["disagree_fields"] if c[n]["human"] is None]
                if not open_fields:
                    continue
            elif mode == "unlabeled" and e["human_labeled"]:
                continue
            entries.append(e)
        entries.sort(key=lambda e: -e["n_disagree"])  # stable: file order within a count
        return entries[offset: offset + limit]

    def audit_ids(self) -> list[str]:
        path = self.root / "audit.json"
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing; build it with `python -m harness.label audit`")
        return json.loads(path.read_text(encoding="utf-8"))["items"]

    def build_audit(self, size: int = AUDIT_SIZE, seed: int = AUDIT_SEED) -> dict:
        """Freeze `size` items labeled by two or more models, stratified by kind and by how many fields
        the models disagree on.

        Six strata (post or reply, times 0, 1 to 2, or 3 or more disagreeing fields) get equal shares, so
        one person's 200 labels estimate how often the LLM labels are right both where they agree and
        where they don't. A stratum short of its share passes the rest to the others. Items with a single
        model's vote are left out, since "no disagreement" means nothing for them.
        """
        strata = defaultdict(list)
        for item in self.items():
            if len(self.votes(item["item_id"])) < 2:
                continue
            n = len(self.disagreements(item["item_id"]))
            band = "0" if n == 0 else "1-2" if n <= 2 else "3+"
            strata[f"{item['kind']}/{band}"].append(item["item_id"])
        rng = random.Random(seed)
        for v in strata.values():
            rng.shuffle(v)
        chosen, keys = [], sorted(strata)
        while len(chosen) < size and any(strata[k] for k in keys):
            for k in keys:
                if strata[k] and len(chosen) < size:
                    chosen.append((k, strata[k].pop()))
        audit = {"seed": seed, "size": len(chosen), "strata": dict(Counter(k for k, _ in chosen)),
                 "items": [i for _, i in chosen]}
        (self.root / "audit.json").write_text(json.dumps(audit, indent=1) + "\n", encoding="utf-8")
        return audit

    # ---- writing -------------------------------------------------------------------------------

    def append_human(self, item_id: str, labeler: str, labels: dict, note: str = "") -> dict:
        """Append one human label row. Fields must exist in the schema and apply to the item's kind;
        enum and ordinal values must be listed values. Partial labels are fine."""
        item = self._require(item_id)
        if not isinstance(labeler, str) or not labeler.strip():
            raise ValueError("labeler must be a non-empty string")
        if not isinstance(labels, dict):
            raise ValueError("labels must be a dict of field name to value")
        fields = self._fields(item["kind"])
        for name, value in labels.items():
            field = fields.get(name)
            if field is None:
                raise ValueError(f"field {name!r} does not apply to a {item['kind']} or is not in the schema")
            if field["kind"] == "text":
                if not isinstance(value, str):
                    raise ValueError(f"field {name!r} takes text")
            elif value not in field["values"] or isinstance(value, bool):
                raise ValueError(f"{name}={value!r} is not one of {field['values']}")
        if not isinstance(note, str):
            raise ValueError("note must be a string")
        row = {"item_id": item_id, "labeler": labeler.strip(), "labels": labels, "note": note,
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with (self.root / "human.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row


_default: Store | None = None


def default_store() -> Store:
    global _default
    if _default is None:
        _default = Store()
    return _default


def load_schema() -> dict:
    return default_store().load_schema()


def items(ids=None) -> list[dict]:
    return default_store().items(ids)


def llm_labels(item_id: str) -> list[dict]:
    return default_store().llm_labels(item_id)


def consensus(item_id: str) -> dict:
    return default_store().consensus(item_id)


def queue(mode: str, limit: int = 50, offset: int = 0) -> list[dict]:
    return default_store().queue(mode, limit, offset)


def append_human(item_id: str, labeler: str, labels: dict, note: str = "") -> dict:
    return default_store().append_human(item_id, labeler, labels, note)
