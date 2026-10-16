"""Mission profiles: one resolver for every brand file, loud on a missing path, and memory kept per profile."""

import pytest

from harness import config, memory, memstore
from harness.minds import writer
from harness.models import Action
from harness.qualify import load_panel
from harness.sources.watch import load_watch

PROFILES = ("wnba", "devtools")


def test_default_profile_is_wnba_and_the_env_switches_it(monkeypatch):
    assert config.profile_name() == "wnba"
    assert config.profile_file("mission.md") == config.PROFILES_DIR / "wnba" / "mission.md"
    monkeypatch.setenv("HARNESS_PROFILE", "devtools")
    assert config.profile_file("mission.md") == config.PROFILES_DIR / "devtools" / "mission.md"
    assert config.lessons_path() == config.MEMORY_LESSONS_DIR / "devtools.md"


def test_use_profile_switches_for_a_block_only():
    with config.use_profile("devtools"):
        assert config.profile_name() == "devtools"
        assert "Provisional" in config.mission().splitlines()[0]
    assert config.profile_name() == "wnba"


def test_a_missing_profile_or_file_names_the_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_PROFILE", "nosuchbrand")
    with pytest.raises(config.ProfileError, match=r"no mission profile at .*nosuchbrand.*\['devtools', 'wnba'\]"):
        config.mission()
    with pytest.raises(config.ProfileError, match="no mission profile"):
        with config.use_profile("nosuchbrand"):
            pass
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path)
    (tmp_path / "half").mkdir()
    monkeypatch.setenv("HARNESS_PROFILE", "half")
    with pytest.raises(config.ProfileError, match=f"panel.json not found at {tmp_path / 'half' / 'panel.json'}"):
        load_panel()


def test_a_profile_name_cannot_leave_the_profiles_folder(monkeypatch):
    monkeypatch.setenv("HARNESS_PROFILE", "../mission")
    with pytest.raises(config.ProfileError, match="must match"):
        config.profile_name()


@pytest.mark.parametrize("name", PROFILES)
def test_every_profile_has_four_files_that_load(name):
    with config.use_profile(name):
        assert config.mission().strip()
        watch = load_watch()
        assert watch.brand_handle == "_sgu_"
        assert watch.reply_targets.accounts
        assert load_panel()
        writer.load_framing()  # raises on a malformed file; None is allowed


def test_profiles_differ_where_the_brand_does():
    with config.use_profile("wnba"):
        wnba = (config.mission(), load_panel(), writer.load_framing())
    with config.use_profile("devtools"):
        devtools = (config.mission(), load_panel(), writer.load_framing())
    assert "Caitlin Clark" in wnba[0] and "Caitlin Clark" not in devtools[0]
    assert set(wnba[1]).isdisjoint(devtools[1])
    assert wnba[2] == writer.ReplyFraming("clarkfanaccount") and devtools[2] is None


@pytest.fixture
def live_stores(tmp_path, monkeypatch):
    """The real per-profile store layout and seeds, with memory/store/ moved to a temp folder."""
    monkeypatch.delenv("MEMSTORE_PATH", raising=False)
    monkeypatch.setattr(config, "PROVIDER", "anthropic")
    monkeypatch.setattr(config, "MEMORY_STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(config, "LESSONS_PATH", tmp_path / "no-lessons.md")
    return tmp_path / "store"


def devtools_seed_titles() -> list[str]:
    seed = config.memory_seed_dir("devtools")
    notes = [memstore.parse(p.read_text(encoding="utf-8"), p) for p in sorted(seed.rglob("*.md"))]
    assert notes, f"no devtools seed notes under {seed}"
    return [n.title for n in notes] + [n.id for n in notes]


def test_no_wnba_prompt_contains_a_devtools_note(live_stores):
    memstore.list_notes()  # the first store access seeds the wnba store
    wnba_prompt = config.lessons() + config.mission()
    assert [t for t in devtools_seed_titles() if t in wnba_prompt] == []
    with config.use_profile("devtools"):
        memstore.list_notes()
        devtools_prompt = config.lessons()
    # The same check finds the seed in the profile it belongs to, so it can fail.
    assert "dev-audience-voice" in devtools_prompt
    assert (live_stores / "devtools" / "procedures" / "dev-audience-voice.md").is_file()
    assert not (live_stores / "wnba" / "procedures").exists()


def test_runs_belong_to_the_profile_they_were_made_for(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")
    wnba = memory.save_run([Action(type="post", text="a", mind="m", narrative_id="n")])
    with config.use_profile("devtools"):
        devtools = memory.save_run([Action(type="post", text="b", mind="m", narrative_id="n")])
    with memory.connect() as conn:  # a run saved before profiles existed has no profile row
        legacy = conn.execute("INSERT INTO runs (started_at, provider, sources) VALUES ('t', 'mock', '')").lastrowid
    assert memory.profile_run_ids("wnba") == {wnba, legacy}
    assert memory.profile_run_ids("devtools") == {devtools}
