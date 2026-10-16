import subprocess
import sys


def test_mock_run_exits_zero():
	# The offline mock run is the one check every branch must keep passing (CLAUDE.md).
	result = subprocess.run(
		[sys.executable, "-m", "harness", "run", "--provider", "mock"],
		capture_output=True,
		text=True,
	)
	assert result.returncode == 0, result.stderr
