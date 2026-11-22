# Value-producing Boolean paths

Each `and` or `or` expression outside a control-flow test is an independent
path decision. Nested expressions retain their own outcomes, and a flat chain
attributes every short-circuit operand in source order.

```python
def nested(first, second, third):
    return (first and second) or third


def flat(first, second, third):
    return first or second or third


nested(0, 2, 3)
nested(1, 0, 3)
nested(1, 2, 3)
flat(1, 2, 3)
flat(0, 2, 3)
flat(0, 0, 3)
```

```json
{
  "definitions": [],
  "observations": [],
  "path_definitions": [
    {
      "kind": "value_and",
      "line": 2,
      "expression": "first and second",
      "outcomes": [
        {"label": "operand 1 selected", "range": [2, 12, 2, 17]},
        {"label": "operand 2 selected", "range": [2, 22, 2, 28]}
      ]
    },
    {
      "kind": "value_or",
      "line": 2,
      "expression": "(first and second) or third",
      "outcomes": [
        {"label": "operand 1 selected", "range": [2, 12, 2, 28]},
        {"label": "operand 2 selected", "range": [2, 33, 2, 38]}
      ]
    },
    {
      "kind": "value_or",
      "line": 6,
      "expression": "first or second or third",
      "outcomes": [
        {"label": "operand 1 selected", "range": [6, 11, 6, 16]},
        {"label": "operand 2 selected", "range": [6, 20, 6, 26]},
        {"label": "operand 3 selected", "range": [6, 30, 6, 35]}
      ]
    }
  ],
  "path_observations": [
    {"kind": "value_and", "line": 2, "outcome": 0},
    {"kind": "value_and", "line": 2, "outcome": 1},
    {"kind": "value_or", "line": 2, "outcome": 0},
    {"kind": "value_or", "line": 2, "outcome": 1},
    {"kind": "value_or", "line": 6, "outcome": 0},
    {"kind": "value_or", "line": 6, "outcome": 1},
    {"kind": "value_or", "line": 6, "outcome": 2}
  ]
}
```
