# Nested cleanup control flow

A return inside a `try` suite preserves the `finally` conditional without
introducing synthetic destinations.

```python
def choose(value):
    try:
        if value == 1:
            return "one"
        if value == 2:
            break_here = True
    finally:
        if value:
            cleanup()
    return "other"
```

```json
{"executable":[1,2,3,4,5,6,8,9,10],"excluded":[],"possible_arcs":[[3,4],[3,5],[5,6],[5,8],[8,9],[8,10]],"no_branch":[],"regions":[{"kind":"function","name":"choose","start":1,"lines":[2,3,4,5,6,7,8,9,10]}]}
```
