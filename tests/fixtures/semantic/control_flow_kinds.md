# Control-flow expression kinds

Each supported syntax form gets one named definition. Calls exercise both
outcomes where Python control flow makes them observable.

```python
def conditional(flag):
    return "yes" if flag else "no"


def filtered(values, flag):
    return [value for value in values if value and flag]


def guarded(value, flag):
    match value:
        case int() if flag:
            return "integer"
        case _:
            return "other"


def looping(value, flag):
    while value and flag:
        value -= 1
    return value


def asserted(flag):
    assert flag


conditional(False)
conditional(True)
filtered([0, 1], False)
filtered([0, 1], True)
guarded(1, False)
guarded(1, True)
looping(1, False)
looping(1, True)
try:
    asserted(False)
except AssertionError:
    pass
asserted(True)
```

```json
{
  "definitions": [
    {"kind": "assert", "line": 24, "expression": "flag", "logic": "c0", "condition_ranges": [[24, 11, 24, 15]]},
    {"kind": "comprehension", "line": 6, "expression": "value and flag", "logic": "and(c0,c1)", "condition_ranges": [[6, 41, 6, 46], [6, 51, 6, 55]]},
    {"kind": "if_expr", "line": 2, "expression": "\"yes\" if flag else \"no\"", "logic": "c0", "condition_ranges": [[2, 20, 2, 24]]},
    {"kind": "match_guard", "line": 11, "expression": "flag", "logic": "c0", "condition_ranges": [[11, 22, 11, 26]]},
    {"kind": "while", "line": 18, "expression": "value and flag", "logic": "and(c0,c1)", "condition_ranges": [[18, 10, 18, 15], [18, 20, 18, 24]]}
  ],
  "observations": [
    {"kind": "assert", "line": 24, "expression": "flag", "vector": "0", "outcome": false},
    {"kind": "assert", "line": 24, "expression": "flag", "vector": "1", "outcome": true},
    {"kind": "comprehension", "line": 6, "expression": "value and flag", "vector": "0-", "outcome": false},
    {"kind": "comprehension", "line": 6, "expression": "value and flag", "vector": "10", "outcome": false},
    {"kind": "comprehension", "line": 6, "expression": "value and flag", "vector": "11", "outcome": true},
    {"kind": "if_expr", "line": 2, "expression": "\"yes\" if flag else \"no\"", "vector": "0", "outcome": false},
    {"kind": "if_expr", "line": 2, "expression": "\"yes\" if flag else \"no\"", "vector": "1", "outcome": true},
    {"kind": "match_guard", "line": 11, "expression": "flag", "vector": "0", "outcome": false},
    {"kind": "match_guard", "line": 11, "expression": "flag", "vector": "1", "outcome": true},
    {"kind": "while", "line": 18, "expression": "value and flag", "vector": "0-", "outcome": false},
    {"kind": "while", "line": 18, "expression": "value and flag", "vector": "10", "outcome": false},
    {"kind": "while", "line": 18, "expression": "value and flag", "vector": "11", "outcome": true}
  ],
  "path_definitions": [
    {
      "kind": "match",
      "line": 10,
      "expression": "match value:\n        case int() if flag:\n            return \"integer\"\n        case _:\n            return \"other\"",
      "outcomes": [
        {"label": "case 1 selected: int()", "range": [11, 13, 11, 18]},
        {"label": "case 2 selected: _", "range": [13, 13, 13, 14]}
      ]
    },
    {
      "kind": "try",
      "line": 35,
      "expression": "try:\n    asserted(False)\nexcept AssertionError:\n    pass",
      "outcomes": [
        {"label": "normal completion", "range": [35, 0, 38, 8]},
        {"label": "handler 1 AssertionError", "range": [37, 0, 38, 8]},
        {"label": "unhandled exception", "range": [35, 0, 38, 8]}
      ]
    }
  ],
  "path_observations": [
    {"kind": "match", "line": 10, "outcome": 0},
    {"kind": "match", "line": 10, "outcome": 1},
    {"kind": "try", "line": 35, "outcome": 1}
  ]
}
```
