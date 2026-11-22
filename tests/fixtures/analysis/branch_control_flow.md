# Branch control-flow fixture

Branches cover conditionals, loop exits, and match cases. The marked conditional
retains both possible arcs but does not report a missing branch.

```python
def choose(value, items):
    if value:
        result = "yes"
    else:
        result = "no"
    for item in items:
        if item < 0:
            continue
        if item == value:
            break
    else:
        result = "missing"
    while True:
        if value:  # pragma: no branch
            break
        value += 1
    match result:
        case "yes":
            return items[0] if items else None
        case _:
            return None
```

The JSON block is the reviewed oracle when `collect_arcs=True`.

```json
{
  "executable": [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
  "excluded": [],
  "possible_arcs": [[2, 3], [2, 5], [6, 7], [6, 12], [7, 8], [7, 9], [9, 6], [9, 10], [14, 15], [14, 16], [18, 19], [18, 20]],
  "no_branch": [13, 14],
  "regions": [{"kind": "function", "name": "choose", "start": 1, "lines": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]}]
}
```
