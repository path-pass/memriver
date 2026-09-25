from memriver_dream.schema_check import matches

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["kind", "items"],
          "properties": {"kind": {"type": "string", "enum": ["a", "b"]},
                         "items": {"type": "array", "items": {"type": "integer"}},
                         "flag": {"type": "boolean"}}}


def test_matches_the_subset_dream_uses():
    assert matches({"kind": "a", "items": [1, 2]}, SCHEMA)
    assert matches({"kind": "b", "items": [], "flag": True}, SCHEMA)


def test_refuses_what_the_schema_does_not_allow():
    for value in ({"kind": "c", "items": []}, {"kind": "a"}, {"kind": "a", "items": ["1"]},
                  {"kind": "a", "items": [True]}, {"kind": "a", "items": [], "extra": 1},
                  ["kind"], {"kind": "a", "items": [], "flag": 1}):
        assert not matches(value, SCHEMA), value
