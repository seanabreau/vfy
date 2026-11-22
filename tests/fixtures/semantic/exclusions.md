# Explicit semantic exclusions

Coverage.py exclusions apply in semantic mode. The `no decision` pragma
excludes semantic decisions, while value-producing `or` remains a named path
decision outside control-flow exclusions.

```python
def excluded(value):
    if value:  # pragma: no decision
        return 1
    if value:  # pragma: no branch
        return 2
    if value:  # pragma: no cover
        return 3
    return 0


def value_or(left, right):
    return left or right


def excluded_value_or(left, right):
    return left or right  # pragma: no decision


def excluded_try():
    try:  # pragma: no decision
        return 1
    except RuntimeError:
        return 0


excluded(False)
value_or(False, True)
excluded_value_or(False, True)
excluded_try()
```

```json
{
  "definitions": [],
  "observations": [],
  "path_definitions": [
    {
      "kind": "value_or",
      "line": 12,
      "expression": "left or right",
      "outcomes": [
        {"label": "operand 1 selected", "range": [12, 11, 12, 15]},
        {"label": "operand 2 selected", "range": [12, 19, 12, 24]}
      ]
    }
  ],
  "path_observations": [
    {"kind": "value_or", "line": 12, "outcome": 1}
  ]
}
```
