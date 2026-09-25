import plistlib
import stat

import pytest
from memriver import launch_agent
from memriver.launch_agent import (
    LaunchctlFailed,
    RestoreFailed,
    install,
    plist_path,
    render,
    uninstall,
)


class Launchctl:
    """launchd in miniature: one job, loaded or not; `print` can fail to answer, bootout
    can fail, and the next `bootstrap_fails` bootstraps fail."""

    def __init__(self, *, loaded: bool = False, bootout_fails: bool = False,
                 bootstrap_fails: int = 0, print_fails: bool = False) -> None:
        self.loaded, self.bootout_fails, self.bootstrap_fails = loaded, bootout_fails, \
            bootstrap_fails
        self.print_fails = print_fails
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> int:
        self.calls.append(list(args))
        if args[0] == "print":
            if self.print_fails:
                return 5                    # launchd could not say
            return 0 if self.loaded else 113
        if args[0] == "bootout":
            if self.bootout_fails:
                return 5
            self.loaded = False
            return 0
        if self.bootstrap_fails:
            self.bootstrap_fails -= 1
            return 5
        self.loaded = True
        return 0


def _plist(tmp_path, at: str = "04:30") -> bytes:
    return render(program=["/opt/memriver", "dream", "run", "--trigger", "schedule"],
                  schedule_at=at, env={"HOME": "/home/u", "PATH": "/opt:/usr/bin:/bin"},
                  log_path=tmp_path / "dream.log")


def test_render_holds_the_schedule_program_environment_and_log(tmp_path):
    rendered = plistlib.loads(_plist(tmp_path))
    assert rendered["Label"] == "io.github.path-pass.memriver.dream"
    assert rendered["ProgramArguments"] == ["/opt/memriver", "dream", "run", "--trigger",
                                            "schedule"]
    assert rendered["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}
    assert rendered["EnvironmentVariables"] == {"HOME": "/home/u", "PATH": "/opt:/usr/bin:/bin"}
    assert rendered["StandardOutPath"] == rendered["StandardErrorPath"] == str(
        tmp_path / "dream.log")
    assert plistlib.loads(render(program=["x"], schedule_at="01:00", env={}, log_path=tmp_path,
                                 label="test.dream"))["Label"] == "test.dream"


def test_install_loads_the_agent_and_a_second_install_replaces_it(tmp_path):
    launchctl = Launchctl()
    install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=launchctl)
    path = plist_path(tmp_path)
    assert path == tmp_path / "Library/LaunchAgents/io.github.path-pass.memriver.dream.plist"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert [call[0] for call in launchctl.calls] == ["print", "bootstrap"]
    install(home=tmp_path, plist=_plist(tmp_path, "05:00"), uid=501, launchctl=launchctl)
    assert launchctl.calls[2:] == [
        ["print", "gui/501/io.github.path-pass.memriver.dream"],
        ["bootout", "gui/501", str(path)],
        ["print", "gui/501/io.github.path-pass.memriver.dream"],
        ["bootstrap", "gui/501", str(path)]]
    assert plistlib.loads(path.read_bytes())["StartCalendarInterval"]["Hour"] == 5


def test_a_job_that_will_not_unload_keeps_the_old_plist_and_fails(tmp_path):
    launchctl = Launchctl()
    install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=launchctl)
    old = plist_path(tmp_path).read_bytes()
    launchctl.bootout_fails = True
    with pytest.raises(LaunchctlFailed):
        install(home=tmp_path, plist=_plist(tmp_path, "05:00"), uid=501, launchctl=launchctl)
    assert plist_path(tmp_path).read_bytes() == old


def test_a_failed_replacement_restores_the_previous_plist_and_reloads_it(tmp_path):
    launchctl = Launchctl()
    install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=launchctl)
    old = plist_path(tmp_path).read_bytes()
    launchctl.bootstrap_fails = 1                         # the new job only
    with pytest.raises(LaunchctlFailed) as caught:
        install(home=tmp_path, plist=_plist(tmp_path, "05:00"), uid=501, launchctl=launchctl)
    assert not isinstance(caught.value, RestoreFailed)
    assert plist_path(tmp_path).read_bytes() == old and launchctl.loaded is True
    assert launchctl.calls[-1] == ["bootstrap", "gui/501", str(plist_path(tmp_path))]


def test_a_write_failing_after_the_unload_restores_and_reloads_the_previous_job(
        tmp_path, monkeypatch):
    launchctl = Launchctl()
    install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=launchctl)
    old, new = plist_path(tmp_path).read_bytes(), _plist(tmp_path, "05:00")
    real_write = launch_agent._write

    def full_disk(path, data):
        if data == new:
            raise OSError("no space left on device")
        real_write(path, data)

    monkeypatch.setattr(launch_agent, "_write", full_disk)
    with pytest.raises(OSError):
        install(home=tmp_path, plist=new, uid=501, launchctl=launchctl)
    assert plist_path(tmp_path).read_bytes() == old and launchctl.loaded is True


def test_a_failed_replacement_of_an_unloaded_job_leaves_it_unloaded(tmp_path):
    path = plist_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"previous plist")
    launchctl = Launchctl(bootstrap_fails=1)
    with pytest.raises(LaunchctlFailed):
        install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=launchctl)
    assert path.read_bytes() == b"previous plist" and launchctl.loaded is False
    assert [call[0] for call in launchctl.calls] == ["print", "bootstrap"]


def test_a_replacement_whose_restore_also_fails_says_so(tmp_path):
    launchctl = Launchctl()
    install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=launchctl)
    old = plist_path(tmp_path).read_bytes()
    launchctl.bootstrap_fails = 2                         # the new job, then the old one
    with pytest.raises(RestoreFailed):
        install(home=tmp_path, plist=_plist(tmp_path, "05:00"), uid=501, launchctl=launchctl)
    assert plist_path(tmp_path).read_bytes() == old and launchctl.loaded is False


def test_a_launchd_that_cannot_answer_fails_and_touches_nothing(tmp_path):
    launchctl = Launchctl()
    install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=launchctl)
    old = plist_path(tmp_path).read_bytes()
    launchctl.print_fails = True
    with pytest.raises(LaunchctlFailed):
        uninstall(home=tmp_path, uid=501, launchctl=launchctl)
    with pytest.raises(LaunchctlFailed):
        install(home=tmp_path, plist=_plist(tmp_path, "05:00"), uid=501, launchctl=launchctl)
    assert plist_path(tmp_path).read_bytes() == old and launchctl.loaded is True
    assert [call[0] for call in launchctl.calls[2:]] == ["print", "print"]


def test_a_failed_first_install_leaves_no_plist(tmp_path):
    launchctl = Launchctl(bootstrap_fails=1)
    with pytest.raises(LaunchctlFailed):
        install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=launchctl)
    assert not plist_path(tmp_path).exists() and launchctl.loaded is False


def test_uninstall_tells_nothing_installed_from_a_failed_bootout(tmp_path):
    assert uninstall(home=tmp_path, uid=501, launchctl=Launchctl()) is False
    launchctl = Launchctl()
    install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=launchctl)
    launchctl.bootout_fails = True
    with pytest.raises(LaunchctlFailed):
        uninstall(home=tmp_path, uid=501, launchctl=launchctl)
    assert plist_path(tmp_path).exists()                  # still loaded: nothing removed
    launchctl.bootout_fails = False
    assert uninstall(home=tmp_path, uid=501, launchctl=launchctl) is True
    assert not plist_path(tmp_path).exists() and launchctl.loaded is False


def test_a_separate_label_is_a_separate_agent(tmp_path):
    launchctl = Launchctl()
    install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=launchctl,
            label="test.memriver.dream")
    assert plist_path(tmp_path, "test.memriver.dream").exists()
    assert not plist_path(tmp_path).exists()


def test_a_link_planted_at_a_temporary_name_is_never_written_through(tmp_path):
    other = tmp_path / "other-config"
    other.write_bytes(b"KEEP")
    other.chmod(0o600)
    path = plist_path(tmp_path)
    path.parent.mkdir(parents=True)
    (path.parent / f".{path.name}.tmp").symlink_to(other)
    install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=Launchctl())
    assert other.read_bytes() == b"KEEP" and stat.S_IMODE(other.stat().st_mode) == 0o600
    assert not path.is_symlink() and plistlib.loads(path.read_bytes())["Label"]


def test_a_restore_whose_bootstrap_cannot_run_says_so(tmp_path):
    class Vanishing(Launchctl):
        """The old job's bootstrap cannot even be started (launchctl gone meanwhile)."""

        def __call__(self, args):
            if args[0] == "bootstrap" and self.bootstrap_fails == 0 and self.calls[-1][0] \
                    == "bootstrap":
                raise OSError("launchctl vanished")
            return super().__call__(args)

    launchctl = Vanishing()
    install(home=tmp_path, plist=_plist(tmp_path), uid=501, launchctl=launchctl)
    old = plist_path(tmp_path).read_bytes()
    launchctl.bootstrap_fails = 1                         # the new job; the old one raises
    with pytest.raises(RestoreFailed):
        install(home=tmp_path, plist=_plist(tmp_path, "05:00"), uid=501, launchctl=launchctl)
    assert plist_path(tmp_path).read_bytes() == old
