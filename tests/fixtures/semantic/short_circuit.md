# Short-circuit decisions

The vectors are the observable truth-table rows, including `-` for an operand
that Python did not evaluate.

```python
def in_bounds(x):
    if x >= 0 and x < 10:
        return True
    return False


in_bounds(-1)
in_bounds(10)
in_bounds(5)
```

```json
{
  "definitions": [
    {"kind": "if", "line": 2, "expression": "x >= 0 and x < 10", "logic": "and(c0,c1)", "condition_ranges": [[2, 7, 2, 13], [2, 18, 2, 24]]}
  ],
  "observations": [
    {"kind": "if", "line": 2, "expression": "x >= 0 and x < 10", "vector": "0-", "outcome": false},
    {"kind": "if", "line": 2, "expression": "x >= 0 and x < 10", "vector": "10", "outcome": false},
    {"kind": "if", "line": 2, "expression": "x >= 0 and x < 10", "vector": "11", "outcome": true}
  ]
}
```
