import subprocess
import sys


def test_version_flag():
    out = subprocess.run([sys.executable, "-m", "memriver.cli", "--version"],
                         capture_output=True, text=True)
    assert out.returncode == 0 and "0.1.0" in out.stdout
