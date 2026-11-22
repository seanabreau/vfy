# Nested short-circuit logic

Nested boolean operators keep source-order condition indexes. The runtime
vector records only operands that Python evaluates.

```python
def choose(first, second, third):
    if first and (second or third):
        return True
    return False


choose(False, False, False)
choose(True, False, False)
choose(True, False, True)
choose(True, True, False)
```

```json
{
  "definitions": [
    {"kind": "if", "line": 2, "expression": "first and (second or third)", "logic": "and(c0,or(c1,c2))", "condition_ranges": [[2, 7, 2, 12], [2, 18, 2, 24], [2, 28, 2, 33]]}
  ],
  "observations": [
    {"kind": "if", "line": 2, "expression": "first and (second or third)", "vector": "0--", "outcome": false},
    {"kind": "if", "line": 2, "expression": "first and (second or third)", "vector": "100", "outcome": false},
    {"kind": "if", "line": 2, "expression": "first and (second or third)", "vector": "101", "outcome": true},
    {"kind": "if", "line": 2, "expression": "first and (second or third)", "vector": "11-", "outcome": true}
  ]
}
```
