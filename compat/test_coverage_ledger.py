"""compat/COVERAGE.md accounts for every Python test function (see its header)."""

from __future__ import annotations

from .gen_coverage import LEDGER, REPO, _rust_tests, listing, parse, problems


def test_every_python_test_function_of_a_finished_phase_has_a_target():
    text = LEDGER.read_text()
    assert sorted(parse(text).targets) == listing(REPO), "run compat/gen_coverage.py"
    assert problems(text, REPO) == []


def test_a_missing_or_dangling_target_fails(tmp_path):
    (tmp_path / "compat").mkdir()
    (tmp_path / "compat" / "test_x.py").write_text("def test_real():\n    pass\n")
    crate = tmp_path / "crates" / "memriver-core"
    (crate / "src" / "deep").mkdir(parents=True)
    (crate / "tests").mkdir()
    (crate / "src" / "lib.rs").write_text("#[test]\nfn at_the_root() {}\n")
    (crate / "src" / "a.rs").write_text(
        "fn helper() {}\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn real_test() {}\n"
        "    #[test]\n    #[should_panic(expected = \"x\")]\n    fn panics() {}\n"
        "    mod inner {\n        #[test]\n        fn nested() { let _ = \"}\"; }\n    }\n}\n"
        "#[test]\nfn after_the_module() {}\n")
    (crate / "src" / "deep" / "mod.rs").write_text("#[tokio::test]\nasync fn in_mod_rs() {}\n")
    (crate / "tests" / "it.rs").write_text("#[test]\nfn in_a_file() {}\n")
    ledger = """finished phases: 1
phase 1: packages/core/tests/**, packages/cli/tests/test_a.py

packages/core/tests/test_a.py::test_compat -> compat:test_x.py::test_real
packages/core/tests/test_a.py::test_rust -> rust:memriver-core::a::tests::real_test
packages/core/tests/test_a.py::test_attributes -> rust:memriver-core::a::tests::panics
packages/core/tests/test_a.py::test_root -> rust:memriver-core::at_the_root
packages/core/tests/test_a.py::test_mod_rs -> rust:memriver-core::deep::in_mod_rs
packages/core/tests/test_a.py::test_file -> rust:memriver-core::tests/it.rs::in_a_file
packages/core/tests/test_a.py::test_file_and_module -> rust:memriver-core::src/a.rs::tests::real_test
packages/core/tests/test_a.py::test_nested -> rust:memriver-core::a::tests::inner::nested
packages/core/tests/test_a.py::test_file_and_nested -> rust:memriver-core::src/a.rs::tests::inner::nested
packages/core/tests/test_a.py::test_after -> rust:memriver-core::a::after_the_module
packages/core/tests/test_a.py::test_both -> compat:test_x.py::test_real + rust:memriver-core::a::tests::real_test
packages/core/tests/test_a.py::test_na -> n/a: an import boundary Cargo enforces
packages/core/tests/sub/test_b.py::test_missing ->
packages/cli/tests/test_a.py::test_no_case -> compat:test_x.py::test_gone
packages/cli/tests/test_a.py::test_no_file -> compat:test_y.py::test_real
packages/cli/tests/test_a.py::test_no_rust -> rust:memriver-core::a::tests::gone_test
packages/cli/tests/test_a.py::test_wrong_module -> rust:memriver-core::b::tests::real_test
packages/cli/tests/test_a.py::test_wrong_file -> rust:memriver-core::tests/other.rs::in_a_file
packages/cli/tests/test_a.py::test_wrong_crate -> rust:memriver::a::tests::real_test
packages/cli/tests/test_a.py::test_not_a_test -> rust:memriver-core::a::helper
packages/cli/tests/test_a.py::test_wrong_inline -> rust:memriver-core::src/a.rs::other::real_test
packages/cli/tests/test_a.py::test_no_inline -> rust:memriver-core::src/a.rs::real_test
packages/cli/tests/test_a.py::test_no_inline_by_module -> rust:memriver-core::a::real_test
packages/cli/tests/test_a.py::test_outer_only -> rust:memriver-core::a::tests::nested
packages/cli/tests/test_a.py::test_too_deep -> rust:memriver-core::a::tests::after_the_module
packages/cli/tests/test_a.py::test_no_reason -> n/a:
packages/cli/tests/test_a.py::test_odd -> somewhere
packages/cli/tests/test_later.py::test_unfinished ->
packages/cli/tests/test_later.py::test_unfinished_dangling -> compat:test_x.py::test_gone
"""
    assert problems(ledger, tmp_path) == [
        "packages/core/tests/sub/test_b.py::test_missing: no target",
        "packages/cli/tests/test_a.py::test_no_case: compat:test_x.py::test_gone does not exist",
        "packages/cli/tests/test_a.py::test_no_file: compat:test_y.py::test_real does not exist",
        "packages/cli/tests/test_a.py::test_no_rust: rust:memriver-core::a::tests::gone_test does not exist",
        "packages/cli/tests/test_a.py::test_wrong_module: rust:memriver-core::b::tests::real_test does not exist",
        "packages/cli/tests/test_a.py::test_wrong_file: rust:memriver-core::tests/other.rs::in_a_file does not exist",
        "packages/cli/tests/test_a.py::test_wrong_crate: rust:memriver::a::tests::real_test does not exist",
        "packages/cli/tests/test_a.py::test_not_a_test: rust:memriver-core::a::helper does not exist",
        "packages/cli/tests/test_a.py::test_wrong_inline: rust:memriver-core::src/a.rs::other::real_test does not exist",
        "packages/cli/tests/test_a.py::test_no_inline: rust:memriver-core::src/a.rs::real_test does not exist",
        "packages/cli/tests/test_a.py::test_no_inline_by_module: rust:memriver-core::a::real_test does not exist",
        "packages/cli/tests/test_a.py::test_outer_only: rust:memriver-core::a::tests::nested does not exist",
        "packages/cli/tests/test_a.py::test_too_deep: rust:memriver-core::a::tests::after_the_module does not exist",
        "packages/cli/tests/test_a.py::test_no_reason: n/a without a reason",
        "packages/cli/tests/test_a.py::test_odd: not a target: somewhere",
        "packages/cli/tests/test_later.py::test_unfinished_dangling: compat:test_x.py::test_gone does not exist",
    ]
    # a glob of a finished phase that matches no test file would demand nothing
    typo = ledger.replace("packages/cli/tests/test_a.py\n", "packages/cli/tests/test_a.py, packages/cil/**\n")
    assert problems(typo, tmp_path)[0] == "phase 1: packages/cil/** matches no test file"
    assert len(problems(typo, tmp_path)) == 17
    # a finished phase must say which files it covers
    assert problems("finished phases: 2\n", tmp_path) == ["phase 2 is finished but has no 'phase 2:' line"]


def test_the_documented_forms_name_a_test_of_this_repository():
    name = "a_variable_name_with_control_characters_is_escaped"
    lines = [f"x.py::test_{n} -> rust:memriver::{path}::{name}"
             for n, path in enumerate(["src/main.rs::tests", "main::tests", "src/main.rs", "main"])]
    assert problems("\n".join(lines) + "\n", REPO) == [
        f"x.py::test_2: rust:memriver::src/main.rs::{name} does not exist",
        f"x.py::test_3: rust:memriver::main::{name} does not exist",
    ]
    binary = "rust:memriver::tests/cli_version.rs::version_through_the_binary"
    assert problems(f"x.py::test_a -> {binary}\n", REPO) == []
    # a file is named inside its crate, never by a path from elsewhere
    elsewhere = f"rust:memriver-core::{REPO}/crates/memriver/src/main.rs::tests::{name}"
    assert problems(f"x.py::test_b -> {elsewhere}\n", REPO) == [f"x.py::test_b: {elsewhere} does not exist"]


# what `rustc --test` lists for this file: outer::only_inside_outer, outer::raw::inside_raw,
# outer::after_raw, at_the_root, later::qualified, later::tests::deep
LITERALS = r'''#![allow(dead_code, unused)]
mod outer {
    const A: &str = r#" embedded quote " } "#;
    #[test]
    fn only_inside_outer() {}
    mod raw {
        const B: &str = r##" "# } mod fake { "##;
        const C: &[u8] = br#" } "#;
        const D: &[u8] = b" } \" } ";
        const E: char = '}';
        const F: char = '\u{7d}';
        const G: u8 = b'}';
        const H: char = '\'';
        const I: char = '"';   // then a brace } in a comment
        /* a block /* nested } */ still a comment } " */
        fn lifetimes<'a, 'b>(x: &'a str, _y: &'b str) -> &'a str { x }
        #[test]
        fn inside_raw() { let _ = 'x'; 'label: loop { break 'label; } }
    }
    #[test]
    #[should_panic(expected = "a ] bracket")]
    fn after_raw() { panic!("a ] bracket") }
    fn not_a_test() {
        #[test]
        fn in_a_function() {}
    }
    struct S;
    impl S {
        fn method() {}
    }
}
/// a doc comment with a brace {
#[test]
fn at_the_root() { let _ = "/* not a comment"; let _ = "// nor this }"; }
mod later {
    #[test]
    pub(crate) fn qualified() {}
    #[cfg(test)]
    mod tests { #[test] fn deep() {} }
}
'''


def test_a_brace_in_a_literal_or_a_comment_does_not_move_a_test_to_another_module(tmp_path):
    source = tmp_path / "crates" / "memriver-core" / "src"
    source.mkdir(parents=True)
    # the reviewer's file
    (source / "lib.rs").write_text(
        'mod outer {\n    const S: &str = r#" embedded quote " } "#;\n'
        "    #[test]\n    fn only_inside_outer() {}\n}\n")
    (source / "literals.rs").write_text(LITERALS)
    ledger = """x.py::test_right -> rust:memriver-core::outer::only_inside_outer
x.py::test_wrong -> rust:memriver-core::only_inside_outer
x.py::test_raw -> rust:memriver-core::literals::outer::raw::inside_raw
x.py::test_fake -> rust:memriver-core::literals::outer::raw::fake::inside_raw
x.py::test_after -> rust:memriver-core::literals::outer::after_raw
x.py::test_after_wrong -> rust:memriver-core::literals::after_raw
x.py::test_inner_item -> rust:memriver-core::literals::outer::in_a_function
x.py::test_root -> rust:memriver-core::literals::at_the_root
x.py::test_root_wrong -> rust:memriver-core::literals::outer::at_the_root
x.py::test_deep -> rust:memriver-core::literals::later::tests::deep
"""
    assert problems(ledger, tmp_path) == [
        f"x.py::test_{name}: rust:memriver-core::{target} does not exist" for name, target in [
            ("wrong", "only_inside_outer"), ("fake", "literals::outer::raw::fake::inside_raw"),
            ("after_wrong", "literals::after_raw"), ("inner_item", "literals::outer::in_a_function"),
            ("root_wrong", "literals::outer::at_the_root")]]
    assert _rust_tests(LITERALS) == [
        ("outer", "only_inside_outer"), ("outer", "raw", "inside_raw"), ("outer", "after_raw"),
        ("at_the_root",), ("later", "qualified"), ("later", "tests", "deep")]


def test_a_target_names_only_files_of_this_repository(tmp_path):
    repo, outside = tmp_path / "repo", tmp_path / "outside"
    crate = repo / "crates" / "memriver-core"
    for directory in (crate / "src", repo / "compat", outside / "src"):
        directory.mkdir(parents=True)
    (crate / "src" / "lib.rs").write_text("#[test]\nfn real() {}\n")
    (repo / "compat" / "test_x.py").write_text("def test_real():\n    pass\n")
    (outside / "src" / "lib.rs").write_text("#[test]\nfn external_test() {}\n")
    (outside / "test_o.py").write_text("def test_out():\n    pass\n")
    (repo / "crates" / "linked").symlink_to(outside)
    (crate / "src" / "out.rs").symlink_to(outside / "src" / "lib.rs")
    (crate / "src" / "outdir").symlink_to(outside / "src")
    (repo / "compat" / "linked.py").symlink_to(outside / "test_o.py")
    refused = [
        f"rust:{outside}::external_test",                               # the reviewer's
        f"rust:memriver-core::{outside}/src/lib::external_test",        # an absolute module
        f"rust:memriver-core::{outside}/src/lib.rs::external_test",     # an absolute file
        "rust:linked::external_test",                                   # a crate that is a link out
        "rust:memriver-core::out::external_test",                       # a file that is a link out
        "rust:memriver-core::src/out.rs::external_test",
        "rust:memriver-core::outdir::lib::external_test",               # a directory that is one
        "rust:memriver-core::src/outdir/lib.rs::external_test",
        "rust:memriver-core/src::lib.rs::real",                         # not one crate's name
        "rust:.::memriver-core/src/lib.rs::real",
        "rust:::memriver-core/src/lib.rs::real",
        "rust:memriver-core::src/./lib::real",                          # not a module's name
        f"compat:{outside}/test_o.py::test_out",
        "compat:linked.py::test_out",
    ]
    accepted = ["rust:memriver-core::real", "rust:memriver-core::src/lib.rs::real", "compat:test_x.py::test_real"]
    ledger = "".join(f"x.py::test_{n} -> {target}\n" for n, target in enumerate(refused + accepted))
    assert problems(ledger, repo) == [f"x.py::test_{n}: {target} does not exist"
                                      for n, target in enumerate(refused)]


def test_a_line_put_off_until_a_phase_fails_when_that_phase_is_finished(tmp_path):
    ledger = """finished phases: 1, 3
phase 1: packages/a/**
phase 3: packages/c/**

packages/a/test_a.py::test_now -> n/a: never applies
packages/a/test_a.py::test_later -> n/a until phase 4: the installer's tests show it
packages/a/test_a.py::test_due -> n/a until phase 3: the server's tests show it
packages/a/test_a.py::test_due_already -> n/a until phase 1: this phase
packages/a/test_a.py::test_no_reason -> n/a until phase 4:
packages/a/test_a.py::test_no_phase -> n/a until phase: later
packages/a/test_a.py::test_no_number -> n/a until phase x: later
packages/a/test_a.py::test_other_words -> n/a until later: a reason
packages/c/test_c.py::test_c -> n/a until phase 4: later still
"""
    assert problems(ledger, tmp_path) == [
        "packages/a/test_a.py::test_due: put off until phase 3, which is finished: name its test",
        "packages/a/test_a.py::test_due_already: put off until phase 1, which is finished: name its test",
        "packages/a/test_a.py::test_no_reason: n/a without a reason",
        "packages/a/test_a.py::test_no_phase: n/a without a reason",
        "packages/a/test_a.py::test_no_number: n/a without a reason",
        "packages/a/test_a.py::test_other_words: n/a without a reason",
    ]
    # when the phase it waits for is finished, the line that was fine is a problem
    later = ledger.replace("finished phases: 1, 3", "finished phases: 1, 3, 4").replace(
        "phase 3: packages/c/**", "phase 3: packages/c/**\nphase 4: packages/c/**")
    assert [found for found in problems(later, tmp_path) if "phase 4, which" in found] == [
        "packages/a/test_a.py::test_later: put off until phase 4, which is finished: name its test",
        "packages/a/test_a.py::test_no_reason: put off until phase 4, which is finished: name its test",
        "packages/c/test_c.py::test_c: put off until phase 4, which is finished: name its test",
    ]


def test_the_ledger_has_lines_put_off_and_none_past_its_phase():
    targets = parse(LEDGER.read_text()).targets
    waiting = sorted(key.split("::")[1] for key, target in targets.items()
                     if target.startswith("n/a until phase "))
    assert waiting == ["test_a_server_built_before_bind_keeps_its_identity",
                       "test_find_git_root_climbs_past_a_looping_git_marker",
                       "test_find_git_root_is_kept_for_installers",
                       "test_find_git_root_raises_when_a_marker_cannot_be_checked"]
