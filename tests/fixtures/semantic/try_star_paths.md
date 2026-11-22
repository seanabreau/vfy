# Exception-group dispatch paths

One exception group can enter multiple `except*` handlers. The path model
records each entered handler independently and retains the unhandled outcome as
an obligation when the group is only partially matched.

```python
try:
    raise ExceptionGroup("mixed", [ValueError(), TypeError()])
except* ValueError:
    value = 1
except* TypeError:
    value = 2
```

```json
{
  "definitions": [],
  "observations": [],
  "path_definitions": [
    {
      "kind": "try_star",
      "line": 1,
      "expression": "try:\n    raise ExceptionGroup(\"mixed\", [ValueError(), TypeError()])\nexcept* ValueError:\n    value = 1\nexcept* TypeError:\n    value = 2",
      "outcomes": [
        {"label": "normal completion", "range": [1, 0, 6, 13]},
        {"label": "handler 1 ValueError", "range": [3, 0, 4, 13]},
        {"label": "handler 2 TypeError", "range": [5, 0, 6, 13]},
        {"label": "unhandled exception", "range": [1, 0, 6, 13]}
      ]
    }
  ],
  "path_observations": [
    {"kind": "try_star", "line": 1, "outcome": 1},
    {"kind": "try_star", "line": 1, "outcome": 2}
  ]
}
```
