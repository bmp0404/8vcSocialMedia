"""`python -m harness.facts refresh [--profile NAME]`: write the profile's live facts to its memory store.
`python -m harness.facts show [--profile NAME]`: list the facts live now."""

import argparse
from datetime import datetime, timezone

from harness import config
from harness.facts import store


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m harness.facts")
    p.add_argument("command", choices=["refresh", "show"])
    p.add_argument("--profile", default=None, help="mission profile; defaults to HARNESS_PROFILE")
    args = p.parse_args()
    profile = args.profile or config.profile_name()
    if args.command == "refresh":
        skipped: dict[str, str] = {}
        counts = store.refresh(profile, skipped=skipped)
        for handle, error in skipped.items():
            print(f"[facts] skipped @{handle}: {error}")
        left_out = f"; skipped {len(skipped)} unreadable account(s): {', '.join('@' + h for h in skipped)}" if skipped else ""
        print(f"[facts] {profile}: wrote {sum(counts.values())} facts ({', '.join(f'{k} {v}' for k, v in counts.items())})"
              + left_out)
        return
    with config.use_profile(profile):
        facts = store.load_live(datetime.now(timezone.utc))
    for f in sorted(facts, key=lambda f: (f.subject, f.as_of)):
        print(f"{f.as_of}..{f.valid_until}  {f.subject}: {' '.join(f.answer.split())[:160]}")
    print(f"[facts] {profile}: {len(facts)} live")


if __name__ == "__main__":
    main()
