"""The report file (spec §6.8): layout, withheld text, the two-part change line of R9,
"Needs you" at the footer, and mark_interrupted's wording (§6.1)."""

from __future__ import annotations

import os
import stat

import memriver_dream.report as report_module
import pytest
from memriver_dream.report import INTERRUPTED, WITHHELD, Report, mark_interrupted

T0 = "2026-09-27T04:00:00.000000Z"
T1 = "2026-09-27T04:05:00.000000Z"


def _report(tmp_path, check=lambda text: None) -> Report:
    return Report(tmp_path / "run0000001.txt", check)


def _policy(text: str) -> str | None:
    return "rule-x" if "SECRET" in text else None


def test_a_report_reads_header_sections_changes_needs_you_and_footer(tmp_path):
    report = _report(tmp_path)
    report.header(run_id="run0000001", started_at=T0, trigger="manual", executor="fake")
    report.section("Policy scan")
    report.line("policy hits: 0; left out of model steps: 0")
    report.needs_you("contradiction aaaaaaaaaa bbbbbbbbbb")
    report.section("Project layer: demo (pppppppppp)")
    report.applying("merge", ["aaaaaaaaaa", "bbbbbbbbbb"], creates=True)
    report.applied("cccccccccc")
    report.applying("supersede", ["dddddddddd"])
    report.not_applied("conflict version dddddddddd")
    assert "Needs you" not in report.path.read_text()     # collected until the footer
    report.footer(status="completed", finished_at=T1)
    assert report.path.read_text() == (
        "memriver dream run run0000001\n"
        f"started: {T0}\n"
        "trigger: manual\n"
        "executor: fake\n"
        "\n== Policy scan ==\n"
        "policy hits: 0; left out of model steps: 0\n"
        "\n== Project layer: demo (pppppppppp) ==\n"
        "applying merge aaaaaaaaaa bbbbbbbbbb (creates a memory) -> change cccccccccc; "
        "undo: memriver undo cccccccccc\n"
        "applying supersede dddddddddd -> not applied: conflict version dddddddddd\n"
        "\n== Needs you ==\n"
        "contradiction aaaaaaaaaa bbbbbbbbbb\n"
        "\nstatus: completed\n"
        f"finished: {T1}\n")
    assert stat.S_IMODE(os.stat(report.path).st_mode) == 0o600


def test_no_executor_and_no_needs_you_leave_those_parts_plain(tmp_path):
    report = _report(tmp_path)
    report.header(run_id="run0000001", started_at=T0, trigger="schedule", executor=None)
    report.footer(status="completed", finished_at=T1)
    assert report.path.read_text() == (
        f"memriver dream run run0000001\nstarted: {T0}\ntrigger: schedule\nexecutor: none\n"
        f"\nstatus: completed\nfinished: {T1}\n")


def test_safe_withholds_text_the_policy_hits_checked_whole_before_any_cut(tmp_path):
    # §6.8: every description and model reason is checked in full, before any cut
    seen: list[str] = []

    def check(text: str) -> str | None:
        seen.append(text)
        return _policy(text)

    report = _report(tmp_path, check)
    long = "a" * 5000 + "SECRET"
    assert report.safe(long) == WITHHELD
    assert seen == [long]
    assert report.safe("two\nlines\u2028here") == "two lines here"


def test_a_withheld_description_never_reaches_the_file(tmp_path):   # §10 item 12
    report = _report(tmp_path, _policy)
    report.line(f'  aaaaaaaaaa "{report.safe("cue holding SECRET text")}"')
    report.needs_you(f"contradiction aaaaaaaaaa: {report.safe('because SECRET')}")
    report.footer(status="completed", finished_at=T1)
    text = report.path.read_text()
    assert "SECRET" not in text
    assert f'  aaaaaaaaaa "{WITHHELD}"\n' in text
    assert f"contradiction aaaaaaaaaa: {WITHHELD}\n" in text


def test_a_line_never_spans_two_lines(tmp_path):
    report = _report(tmp_path)
    report.line("first\nsecond")
    report.needs_you("one\ntwo")
    report.footer(status="completed", finished_at=T1)
    lines = report.path.read_text().splitlines()
    assert "first second" in lines and "one two" in lines


def test_a_footer_after_an_unfinished_applying_line_marks_it_unknown(tmp_path):
    report = _report(tmp_path)
    report.applying("supersede", ["aaaaaaaaaa"])
    report.footer(status="failed", finished_at=T1)
    assert report.path.read_text() == (
        "applying supersede aaaaaaaaaa -> outcome unknown — see memriver history aaaaaaaaaa\n"
        f"\nstatus: failed\nfinished: {T1}\n")


def test_a_failed_completion_append_keeps_the_line_open_for_the_next_write(
        tmp_path, monkeypatch):
    # a completion append can fail after core already committed the change; the
    # pending line must survive so the very next write (here, the failure
    # footer) still closes it as "outcome unknown" instead of losing it
    report = _report(tmp_path)
    report.applying("rewrite", ["aaaaaaaaaa"])

    def broken(path, text):
        raise OSError("injected")

    monkeypatch.setattr(report_module, "_append", broken)
    with pytest.raises(OSError, match="^injected$"):
        report.applied("cccccccccc")
    assert report._pending == "applying rewrite aaaaaaaaaa"
    monkeypatch.undo()
    report.footer(status="failed", finished_at=T1)
    assert report.path.read_text() == (
        "applying rewrite aaaaaaaaaa -> outcome unknown — see memriver history aaaaaaaaaa\n"
        f"\nstatus: failed\nfinished: {T1}\n")


def test_a_not_applied_reason_is_single_lined(tmp_path):
    report = _report(tmp_path)
    report.applying("supersede", ["aaaaaaaaaa"])
    report.not_applied("multi\nline reason")
    assert report.path.read_text() == (
        "applying supersede aaaaaaaaaa -> not applied: multi line reason\n")


def test_an_applying_kind_with_a_newline_is_single_lined_too(tmp_path):
    report = _report(tmp_path)
    report.applying("super\nsede", ["aaaaaaaaaa"])
    report.not_applied("ok")
    assert report.path.read_text() == "applying super sede aaaaaaaaaa -> not applied: ok\n"


def test_mark_interrupted_on_an_unreadable_report_marks_nothing_and_does_not_raise(tmp_path):
    directory = tmp_path / "run0000001.txt"
    directory.mkdir()
    mark_interrupted(directory)      # must not raise: there is nothing safe to read or write
    assert directory.is_dir() and list(directory.iterdir()) == []


def test_mark_interrupted_completes_a_dangling_line_and_closes_the_report(tmp_path):
    # §10 item 12; §6.1 wording
    path = tmp_path / "run0000001.txt"
    done = ("applying rewrite aaaaaaaaaa -> change cccccccccc; "
            "undo: memriver undo cccccccccc")
    path.write_text(f"memriver dream run run0000001\n{done}\n"
                    "applying merge bbbbbbbbbb dddddddddd (creates a memory)")
    mark_interrupted(path)
    assert path.read_text() == (
        f"memriver dream run run0000001\n{done}\n"
        "applying merge bbbbbbbbbb dddddddddd (creates a memory) -> outcome unknown — "
        "see memriver history bbbbbbbbbb; see memriver history dddddddddd; a created "
        "memory, if any, is not listed — see memriver list\n"
        f"\n{INTERRUPTED}\nstatus: failed\n")


def test_a_dangling_group_that_only_creates_names_no_history_ids(tmp_path):
    path = tmp_path / "run0000001.txt"
    path.write_text("applying new (creates a memory)")
    mark_interrupted(path)
    assert path.read_text() == (
        "applying new (creates a memory) -> outcome unknown; a created memory, if any, "
        "is not listed — see memriver list\n"
        f"\n{INTERRUPTED}\nstatus: failed\n")


def test_a_report_ending_cleanly_or_missing_only_gets_the_interrupted_lines(tmp_path):
    path = tmp_path / "run0000001.txt"
    path.write_text("memriver dream run run0000001\napplying rewrite aaaaaaaaaa -> change "
                    "cccccccccc; undo: memriver undo cccccccccc\n")
    before = path.read_text()
    mark_interrupted(path)
    assert path.read_text() == before + f"\n{INTERRUPTED}\nstatus: failed\n"
    missing = tmp_path / "missing.txt"
    mark_interrupted(missing)
    assert missing.read_text() == f"\n{INTERRUPTED}\nstatus: failed\n"
