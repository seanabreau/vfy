# Exclusions and annotation fixture

Excluded suites and bare function-local annotations do not contribute
statements. Class annotation behavior differs before Python 3.14.

```python
module_value: int


class Model:
    field: int


def function():
    local: int
    function.attribute: int


if False:  # pragma: no cover
    unreachable = 1

live = 2
```

```json
{
  "executable": [1, 4, 8, 10, 16],
  "excluded": [13, 14],
  "possible_arcs": [],
  "no_branch": [13],
  "regions": [
    {"kind": "class", "name": "Model", "start": 4, "lines": []},
    {"kind": "function", "name": "function", "start": 8, "lines": [9, 10]}
  ],
  "before_314": {
    "executable": [1, 4, 5, 8, 10, 16],
    "class_annotations_before_314": true
  }
}
```
