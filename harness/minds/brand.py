"""Brand wording the action prompts insert, read from the active profile's prompts.toml.

The prompts in writer.py, lanes.py and veto.py are brand-agnostic templates. The few phrases that name a
brand's world (an example caption, which news needs care, who counts as a rival, which people the risk
rule protects) live in mission/profiles/<name>/prompts.toml, so the same code runs for every profile.
"""

import tomllib

from harness import config

KEYS = ("caption_example", "plan_care", "side_rule", "risk_people", "rival_angle", "ask_detail")


def wording() -> dict[str, str]:
    """Every key, from the active profile. A missing file, a missing or extra key, or an empty value raises."""
    path = config.profile_file("prompts.toml")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(raw) != set(KEYS) or not all(isinstance(v, str) and v.strip() for v in raw.values()):
        missing, extra = sorted(set(KEYS) - set(raw)), sorted(set(raw) - set(KEYS))
        raise ValueError(f"{path}: needs exactly {list(KEYS)} as non-empty strings; missing {missing}, extra {extra}")
    return raw


def text(key: str) -> str:
    return wording()[key]
