import os
import subprocess
import sys

from harness import config


def test_mock_run_exits_zero(tmp_path):
	# The offline mock run is the one check every branch must keep passing (CLAUDE.md). It writes to tmp_path,
	# so the suite leaves the checkout's data/harness.db and out/ alone.
	real_out = sorted(os.listdir(config.OUT_DIR)) if config.OUT_DIR.is_dir() else []
	env = {**os.environ, "HARNESS_DB_PATH": str(tmp_path / "harness.db"), "HARNESS_OUT_DIR": str(tmp_path / "out"),
	       "MEMSTORE_PATH": str(tmp_path / "store")}
	result = subprocess.run(
		[sys.executable, "-m", "harness", "run", "--provider", "mock"],
		capture_output=True,
		text=True,
		env=env,
	)
	assert result.returncode == 0, result.stderr
	assert (tmp_path / "harness.db").is_file() and list((tmp_path / "out").glob("recommendations-*.json"))
	assert (sorted(os.listdir(config.OUT_DIR)) if config.OUT_DIR.is_dir() else []) == real_out


def test_a_mock_store_is_removed_when_its_process_exits(tmp_path):
	script = ("from harness import config; config.PROVIDER = 'mock'\n"
	          "from harness.memstore import list_notes, store_root\n"
	          "list_notes(); print(store_root().parent)")
	env = {k: v for k, v in os.environ.items() if k != "MEMSTORE_PATH"}
	out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=config.ROOT)
	assert out.returncode == 0, out.stderr
	root = out.stdout.strip().splitlines()[-1]
	assert root and not os.path.exists(root)
