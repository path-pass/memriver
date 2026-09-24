"""The store purge moved from `memriver uninstall`: structured outcomes, no output."""

from __future__ import annotations

from pathlib import Path

import pytest
from memriver_core.repository import directories
from memriver_core.repository.directories import (
    PurgePlan,
    PurgeRefusal,
    plan_purge,
    purge,
)


@pytest.fixture
def places(tmp_path):
    home, cwd, store = tmp_path / "home", tmp_path / "cwd", tmp_path / "store"
    for d in (home, cwd, store):
        d.mkdir()
    (store / "memriver.db").write_text("x")
    (store / "sub").mkdir()
    (store / "sub" / "f").write_text("y")
    return home, cwd, store


def test_a_confirmed_store_is_removed(places):
    home, cwd, store = places
    with plan_purge(store, home=home, cwd=cwd) as plan:
        assert isinstance(plan, PurgePlan) and plan.exists and plan.fd is not None
        result = purge(plan)
    assert (result.outcome, result.path) == ("removed", store.resolve())
    assert not store.exists()
    assert plan.fd is None                       # the context manager closed it


def test_a_missing_store_is_a_plan_with_nothing_to_remove(places):
    home, cwd, store = places
    plan = plan_purge(store.parent / "absent", home=home, cwd=cwd)
    assert isinstance(plan, PurgePlan) and not plan.exists and plan.fd is None


def test_a_dry_run_plan_opens_nothing(places):
    home, cwd, store = places
    plan = plan_purge(store, home=home, cwd=cwd, dry_run=True)
    assert isinstance(plan, PurgePlan) and plan.exists and plan.fd is None
    assert store.exists()


def test_a_target_holding_home_or_cwd_is_too_broad(places):
    home, cwd, _ = places
    for target in (home, cwd, home.parent):
        refusal = plan_purge(target, home=home, cwd=cwd)
        assert isinstance(refusal, PurgeRefusal) and refusal.kind == "too-broad"
        assert refusal.canonical == target.resolve()


def test_a_symlinked_target_is_refused_not_followed(places, tmp_path):
    home, cwd, store = places
    link = tmp_path / "link"
    link.symlink_to(store)
    refusal = plan_purge(link, home=home, cwd=cwd)
    assert isinstance(refusal, PurgeRefusal) and (refusal.kind, refusal.path) == ("symlink", link)
    assert store.exists()


def test_a_file_target_is_not_a_directory(places, tmp_path):
    home, cwd, _ = places
    target = tmp_path / "file"
    target.write_text("x")
    refusal = plan_purge(target, home=home, cwd=cwd)
    assert isinstance(refusal, PurgeRefusal) and refusal.kind == "not-directory"


def test_a_relative_target_resolves_against_cwd(places):
    home, cwd, _ = places
    (cwd / "rel").mkdir()
    plan = plan_purge(Path("rel"), home=home, cwd=cwd, dry_run=True)
    assert isinstance(plan, PurgePlan) and plan.canonical == (cwd / "rel").resolve()


def test_a_directory_swapped_after_confirmation_is_left_alone(places, tmp_path):
    home, cwd, store = places
    with plan_purge(store, home=home, cwd=cwd) as plan:
        store.rename(tmp_path / "moved")
        store.mkdir()
        result = purge(plan)
    assert result.outcome == "not-confirmed"
    assert store.exists() and (tmp_path / "moved" / "memriver.db").exists()


def test_a_failing_walk_reports_partial_removal(places, monkeypatch):
    home, cwd, store = places

    def refuse(fd, *, directory, replaced):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(directories, "_empty_directory", refuse)
    with plan_purge(store, home=home, cwd=cwd) as plan:
        result = purge(plan)
    assert (result.outcome, result.detail) == ("partly-removed", "denied")


def test_purge_without_an_opened_plan_is_a_programming_error(places):
    home, cwd, store = places
    with pytest.raises(ValueError):
        purge(plan_purge(store, home=home, cwd=cwd, dry_run=True))


def test_purge_of_a_closed_plan_is_a_programming_error(places):
    home, cwd, store = places
    plan = plan_purge(store, home=home, cwd=cwd)
    plan.close()
    with pytest.raises(ValueError):
        purge(plan)
    assert store.exists()
