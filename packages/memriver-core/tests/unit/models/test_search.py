"""Keyword search terms and ranks: the one rule both stores apply."""

import pytest
from memriver_core.models import Memory
from memriver_core.models.search import (
    MAX_TERMS,
    Rank,
    rank_key,
    rank_memories,
    search_terms,
)

SOURCE = {"harness": "test", "method": "agent"}
PROJECT = "zzzzzzzzzz"


def _memory(body: str, description: str = "") -> Memory:
    return Memory.new(body=body, type="project", project_id=PROJECT, source=SOURCE,
                      description=description)


# --- search_terms: splitting ---------------------------------------------------

@pytest.mark.parametrize("query", [
    "uv pip",
    "  uv\tpip\n",
    "uv\u3000pip",               # ideographic space
    "uv\u00a0pip",               # no-break space: Unicode whitespace too
    "uv,pip",
    "uv\uff0cpip",               # full-width comma
    "uv\u3001pip",               # ideographic comma
    "uv;pip",
    "uv\uff1bpip",               # full-width semicolon
    "uv , ;\u3001pip",
])
def test_whitespace_and_the_five_separators_split_terms(query):
    assert search_terms(query) == ("uv", "pip")


def test_duplicates_keep_their_first_occurrence():
    assert search_terms("uv pip UV Pip ruff uv") == ("uv", "pip", "ruff")


def test_at_most_sixteen_terms_the_rest_ignored():
    words = [f"w{n}" for n in range(20)]
    assert MAX_TERMS == 16
    assert search_terms(" ".join(words)) == tuple(words[:16])
    # the cap counts distinct terms: repeats do not use it up
    assert search_terms(" ".join(["w0"] * 20 + words)) == tuple(words[:16])


@pytest.mark.parametrize("query", ["", "   ", "\u3000", ",;\u3001\uff0c\uff1b", "\x00", " \x00 "])
def test_a_query_with_no_term_has_no_terms(query):
    assert search_terms(query) == ()


def test_nul_is_dropped_not_a_separator():
    assert search_terms("u\x00v") == ("uv",)


def test_no_quoting_or_operators():
    assert search_terms('"uv pip" -ruff OR x') == ('"uv', 'pip"', "-ruff", "or", "x")


# --- search_terms / rank_key: folding ------------------------------------------

def test_full_width_letters_fold_to_ascii():
    assert search_terms("\uff35\uff36 \uff30\uff29\uff30") == ("uv", "pip")
    assert rank_key(("uv",), ["\uff35\uff36 manages python"]).terms == 1


def test_case_folding_goes_beyond_lower():
    assert search_terms("STRASSE") == search_terms("stra\u00dfe") == ("strasse",)
    assert rank_key(search_terms("STRASSE"), ["Die Stra\u00dfe"]).terms == 1
    assert rank_key(search_terms("\u00c4rger"), ["\u00c4RGER mit Umlauten"]).terms == 1


def test_cjk_two_character_terms_match_inside_a_sentence():
    terms = search_terms("executor \u5c42 classifier \u5e76\u56de memriver")
    assert terms == ("executor", "\u5c42", "classifier", "\u5e76\u56de", "memriver")
    rank = rank_key(terms, ["\u628a classifier \u5e76\u56de\u5230 memriver \u91cc"])
    assert rank.terms == 3 and not rank.whole


# --- rank_key -------------------------------------------------------------------

def test_no_term_found_is_rank_zero():
    assert rank_key(("uv", "pip"), ["ruff only"]) == Rank(False, 0, 0)
    assert rank_key((), ["anything"]) == Rank(False, 0, 0)


def test_the_whole_query_is_the_terms_joined_by_one_space():
    terms = search_terms("uv,  PIP")
    assert rank_key(terms, ["use uv pip install"]).whole
    assert not rank_key(terms, ["use uv, pip"]).whole          # both terms, not the phrase
    assert rank_key(terms, ["use uv, pip"]).terms == 2


def test_terms_are_counted_once_across_every_text():
    assert rank_key(("uv", "pip"), ["uv", "uv pip", "pip"]) == Rank(True, 2, 0)


def test_the_description_counts_its_own_terms():
    assert rank_key(("uv", "pip"), ["uv cue", "pip body"], description="uv cue") == \
        Rank(False, 2, 1)
    assert rank_key(("uv", "pip"), ["uv cue", "pip body"]).in_description == 0


# --- rank_memories: the order rules 1-4 --------------------------------------------

def test_rank_memories_drops_non_matches_and_orders_by_the_four_rules():
    terms = search_terms("uv pip")
    newest_one_term = _memory("uses uv")
    two_terms_body = _memory("uv, and pip")
    two_terms_cue = _memory("and pip", description="uv")
    whole_older = _memory("run uv pip install")
    whole_newer = _memory("uv pip everywhere")
    unrelated = _memory("ruff")
    # given newest first, as the stores pass them
    given = [newest_one_term, unrelated, whole_newer, two_terms_body, two_terms_cue,
             whole_older]
    assert rank_memories(terms, given) == [
        whole_newer, whole_older,        # rule 1, then recency (rule 4)
        two_terms_cue, two_terms_body,   # rule 2, then rule 3 beats recency
        newest_one_term]


def test_rank_memories_with_one_term_keeps_the_given_order_but_puts_cue_hits_first():
    older_body, cue_hit, newer_body = _memory("uv a"), _memory("x", "uv"), _memory("uv b")
    assert rank_memories(("uv",), [newer_body, cue_hit, older_body]) == \
        [cue_hit, newer_body, older_body]
