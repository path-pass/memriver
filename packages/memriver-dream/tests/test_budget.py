from memriver_dream.budget import cut, estimate_tokens


def test_estimate_counts_a_wide_character_as_one_token_and_four_others_as_one():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2                  # rounded up
    assert estimate_tokens(chr(0x4E2D) * 3) == 3          # CJK
    assert estimate_tokens(chr(0xFF21) + "ab") == 2       # fullwidth + two others


def test_cut_keeps_every_piece_within_the_budget_and_loses_nothing():
    text = "x" * 50 + chr(0x4E2D) * 30 + "y" * 7
    pieces = cut(text, 10)
    assert "".join(pieces) == text
    assert all(estimate_tokens(piece) <= 10 for piece in pieces)
    assert len(pieces) > 1
