import os
import stat
import subprocess
import sys
import textwrap

from memriver_dream.lock import run_lock


def test_a_second_run_finds_the_lock_held_and_it_is_free_again_after(tmp_path):
    with run_lock(tmp_path) as first, run_lock(tmp_path) as second:
        assert (first, second) == (True, False)
    with run_lock(tmp_path) as again:
        assert again
    assert stat.S_IMODE(os.stat(tmp_path / "dream").st_mode) == 0o700


def test_a_lock_held_by_another_process_is_seen_and_released_when_it_dies(tmp_path):
    (tmp_path / "dream").mkdir(mode=0o700)
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import fcntl, os, time
        fd = os.open({str(tmp_path / "dream" / ".lock")!r}, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        print("held", flush=True)
        time.sleep(60)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with run_lock(tmp_path) as held:
            assert held is False
    finally:
        holder.kill()
        holder.wait()
    with run_lock(tmp_path) as held:
        assert held is True
