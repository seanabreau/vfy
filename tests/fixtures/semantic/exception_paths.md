# Exception dispatch paths

A `try` decision distinguishes normal completion, each handler body, and an
exception that propagates to the caller. Handler matching follows Python: a
subclass reaches its base-class handler.

```python
from contextlib import suppress


class ChildValueError(ValueError):
    pass


def dispatch(mode):
    try:
        if mode == "value":
            raise ChildValueError()
        if mode == "type":
            raise TypeError()
        if mode == "key":
            raise KeyError()
    except ValueError:
        return "value"
    except TypeError:
        return "type"
    return "normal"


dispatch("normal")
dispatch("value")
dispatch("type")
with suppress(KeyError):
    dispatch("key")
```

```json
{
  "definitions": [
    {"kind": "if", "line": 10, "expression": "mode == \"value\"", "logic": "c0", "condition_ranges": [[10, 11, 10, 26]]},
    {"kind": "if", "line": 12, "expression": "mode == \"type\"", "logic": "c0", "condition_ranges": [[12, 11, 12, 25]]},
    {"kind": "if", "line": 14, "expression": "mode == \"key\"", "logic": "c0", "condition_ranges": [[14, 11, 14, 24]]}
  ],
  "observations": [
    {"kind": "if", "line": 10, "expression": "mode == \"value\"", "vector": "0", "outcome": false},
    {"kind": "if", "line": 10, "expression": "mode == \"value\"", "vector": "1", "outcome": true},
    {"kind": "if", "line": 12, "expression": "mode == \"type\"", "vector": "0", "outcome": false},
    {"kind": "if", "line": 12, "expression": "mode == \"type\"", "vector": "1", "outcome": true},
    {"kind": "if", "line": 14, "expression": "mode == \"key\"", "vector": "0", "outcome": false},
    {"kind": "if", "line": 14, "expression": "mode == \"key\"", "vector": "1", "outcome": true}
  ],
  "path_definitions": [
    {
      "kind": "try",
      "line": 9,
      "expression": "try:\n        if mode == \"value\":\n            raise ChildValueError()\n        if mode == \"type\":\n            raise TypeError()\n        if mode == \"key\":\n            raise KeyError()\n    except ValueError:\n        return \"value\"\n    except TypeError:\n        return \"type\"",
      "outcomes": [
        {"label": "normal completion", "range": [9, 4, 19, 21]},
        {"label": "handler 1 ValueError", "range": [16, 4, 17, 22]},
        {"label": "handler 2 TypeError", "range": [18, 4, 19, 21]},
        {"label": "unhandled exception", "range": [9, 4, 19, 21]}
      ]
    }
  ],
  "path_observations": [
    {"kind": "try", "line": 9, "outcome": 0},
    {"kind": "try", "line": 9, "outcome": 1},
    {"kind": "try", "line": 9, "outcome": 2},
    {"kind": "try", "line": 9, "outcome": 3}
  ]
}
```
