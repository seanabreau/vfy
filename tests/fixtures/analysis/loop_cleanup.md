# Loop cleanup control flow

`continue` and `break` from a loop nested in a `try` suite converge on the
`finally` block.

```python
def choose(value, items):
    try:
        for item in items:
            if item == value:
                continue
            if item < 0:
                break
    finally:
        if value:
            cleanup()
    return "other"
```

```json
{"executable":[1,2,3,4,5,6,7,9,10,11],"excluded":[],"possible_arcs":[[3,4],[3,9],[4,5],[4,6],[6,3],[6,7],[9,10],[9,11]],"no_branch":[],"regions":[{"kind":"function","name":"choose","start":1,"lines":[2,3,4,5,6,7,8,9,10,11]}]}
```
