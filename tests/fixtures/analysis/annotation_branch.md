# Annotation-only branch destination

A function-local annotation is not a branch destination.

```python
def normalize(value):
    if isinstance(value, str):
        return value
    else:
        normalized: object
        if value is None:
            return ""
        return value
```

```json
{"executable":[1,2,3,6,7,8],"excluded":[],"possible_arcs":[[2,3],[2,6],[6,7],[6,8]],"no_branch":[],"regions":[{"kind":"function","name":"normalize","start":1,"lines":[2,3,4,5,6,7,8]}]}
```
