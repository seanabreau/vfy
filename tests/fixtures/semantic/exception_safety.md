# Exception safety

vfy records the result of the program's truth test; it never calls
`__bool__` itself. If evaluating a condition raises, that incomplete vector is
discarded rather than reported as a false outcome.

```python
class Probe:
    calls = 0

    def __bool__(self):
        Probe.calls += 1
        return True


class Bomb:
    def __bool__(self):
        raise RuntimeError("condition failed")


probe = Probe()
if probe:
    pass
if Probe.calls != 1:
    raise AssertionError("vfy repeated __bool__")
try:
    if Bomb():
        pass
except RuntimeError:
    pass
```

```json
{
  "definitions": [
    {"kind": "if", "line": 15, "expression": "probe", "logic": "c0", "condition_ranges": [[15, 3, 15, 8]]},
    {"kind": "if", "line": 17, "expression": "Probe.calls != 1", "logic": "c0", "condition_ranges": [[17, 3, 17, 19]]},
    {"kind": "if", "line": 20, "expression": "Bomb()", "logic": "c0", "condition_ranges": [[20, 7, 20, 13]]}
  ],
  "observations": [
    {"kind": "if", "line": 15, "expression": "probe", "vector": "1", "outcome": true},
    {"kind": "if", "line": 17, "expression": "Probe.calls != 1", "vector": "0", "outcome": false}
  ],
  "path_definitions": [
    {
      "kind": "try",
      "line": 19,
      "expression": "try:\n    if Bomb():\n        pass\nexcept RuntimeError:\n    pass",
      "outcomes": [
        {"label": "normal completion", "range": [19, 0, 23, 8]},
        {"label": "handler 1 RuntimeError", "range": [22, 0, 23, 8]},
        {"label": "unhandled exception", "range": [19, 0, 23, 8]}
      ]
    }
  ],
  "path_observations": [
    {"kind": "try", "line": 19, "outcome": 1}
  ]
}
```
