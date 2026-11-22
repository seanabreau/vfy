# Match case outcomes

Patterns are reported as case-selection outcomes. A guard remains a separate
Boolean decision, and an unguarded wildcard makes `no case matched` impossible.
The second match is non-exhaustive, so its unmatched outcome is observable.

```python
def classify(value, allow):
    match value:
        case 1 | 2:
            return "small"
        case {"kind": "ok", "code": code} if allow and code > 0:
            return "mapping"
        case [first, *rest]:
            return "sequence"
        case _:
            return "other"


def maybe(value, enabled):
    match value:
        case int() if enabled:
            return "integer"
        case str():
            return "string"
    return "none"


class Value:
    __match_args__ = ("code",)

    def __init__(self, code):
        self.code = code


classify(1, False)
classify({"kind": "ok", "code": 3}, True)
classify({"kind": "ok", "code": 3}, False)
classify([1, 2], False)
classify(Value(3), False)
maybe(1, True)
maybe(1, False)
maybe("text", False)
maybe(1.5, False)
```

```json
{
  "definitions": [
    {"kind": "match_guard", "line": 5, "expression": "allow and code > 0", "logic": "and(c0,c1)", "condition_ranges": [[5, 45, 5, 50], [5, 55, 5, 63]]},
    {"kind": "match_guard", "line": 15, "expression": "enabled", "logic": "c0", "condition_ranges": [[15, 22, 15, 29]]}
  ],
  "observations": [
    {"kind": "match_guard", "line": 5, "expression": "allow and code > 0", "vector": "0-", "outcome": false},
    {"kind": "match_guard", "line": 5, "expression": "allow and code > 0", "vector": "11", "outcome": true},
    {"kind": "match_guard", "line": 15, "expression": "enabled", "vector": "0", "outcome": false},
    {"kind": "match_guard", "line": 15, "expression": "enabled", "vector": "1", "outcome": true}
  ],
  "path_definitions": [
    {
      "kind": "match",
      "line": 2,
      "expression": "match value:\n        case 1 | 2:\n            return \"small\"\n        case {\"kind\": \"ok\", \"code\": code} if allow and code > 0:\n            return \"mapping\"\n        case [first, *rest]:\n            return \"sequence\"\n        case _:\n            return \"other\"",
      "outcomes": [
        {"label": "case 1 selected: 1 | 2", "range": [3, 13, 3, 18]},
        {"label": "case 2 selected: {\"kind\": \"ok\", \"code\": code}", "range": [5, 13, 5, 41]},
        {"label": "case 3 selected: [first, *rest]", "range": [7, 13, 7, 27]},
        {"label": "case 4 selected: _", "range": [9, 13, 9, 14]}
      ]
    },
    {
      "kind": "match",
      "line": 14,
      "expression": "match value:\n        case int() if enabled:\n            return \"integer\"\n        case str():\n            return \"string\"",
      "outcomes": [
        {"label": "case 1 selected: int()", "range": [15, 13, 15, 18]},
        {"label": "case 2 selected: str()", "range": [17, 13, 17, 18]},
        {"label": "no case matched", "range": [14, 4, 18, 27]}
      ]
    }
  ],
  "path_observations": [
    {"kind": "match", "line": 2, "outcome": 0},
    {"kind": "match", "line": 2, "outcome": 1},
    {"kind": "match", "line": 2, "outcome": 2},
    {"kind": "match", "line": 2, "outcome": 3},
    {"kind": "match", "line": 14, "outcome": 0},
    {"kind": "match", "line": 14, "outcome": 1},
    {"kind": "match", "line": 14, "outcome": 2}
  ]
}
```
