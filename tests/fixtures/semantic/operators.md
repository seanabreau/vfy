# Negation and chained comparisons

`not` changes decision polarity without creating an extra condition. Chained
comparisons expose one atomic condition for each comparison.

```python
def negated(left, right):
    if not (left or right):
        return "neither"
    return "some"


def ordered(first, middle, last):
    if first < middle < last:
        return "ordered"
    return "not ordered"


negated(False, False)
negated(False, True)
negated(True, False)
ordered(1, 2, 3)
ordered(3, 2, 1)
ordered(1, 3, 2)
```

```json
{
  "definitions": [
    {"kind": "if", "line": 2, "expression": "not (left or right)", "logic": "not(or(c0,c1))", "condition_ranges": [[2, 12, 2, 16], [2, 20, 2, 25]]},
    {"kind": "if", "line": 8, "expression": "first < middle < last", "logic": "and(c0,c1)", "condition_ranges": [[8, 7, 8, 28], [8, 7, 8, 28]]}
  ],
  "observations": [
    {"kind": "if", "line": 2, "expression": "not (left or right)", "vector": "00", "outcome": true},
    {"kind": "if", "line": 2, "expression": "not (left or right)", "vector": "01", "outcome": false},
    {"kind": "if", "line": 2, "expression": "not (left or right)", "vector": "1-", "outcome": false},
    {"kind": "if", "line": 8, "expression": "first < middle < last", "vector": "11", "outcome": true},
    {"kind": "if", "line": 8, "expression": "first < middle < last", "vector": "0-", "outcome": false},
    {"kind": "if", "line": 8, "expression": "first < middle < last", "vector": "10", "outcome": false}
  ]
}
```
