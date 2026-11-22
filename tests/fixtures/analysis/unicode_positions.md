# Unicode source positions

Non-ASCII identifiers and string literals do not shift line-based branch
positions.

```python
def café(value):
    if value == "é":
        return "✓"
    return "—"
```

```json
{"executable":[1,2,3,4],"excluded":[],"possible_arcs":[[2,3],[2,4]],"no_branch":[],"regions":[{"kind":"function","name":"café","start":1,"lines":[2,3,4]}]}
```
