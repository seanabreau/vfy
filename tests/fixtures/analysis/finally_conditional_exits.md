# Conditional abrupt exits

Both alternatives leave the try suite abruptly. The decision in the try suite keeps its
explicit destinations, while cleanup has no normal continuation.

```python
def close(enabled):
    try:
        if enabled:
            return 1
        else:
            raise ValueError
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,4,6,8,9],"excluded":[],"possible_arcs":[[3,4],[3,6]],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7,8,9]}]}
```
