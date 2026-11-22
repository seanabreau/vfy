# Returning handler cleanup

A handler that returns does not create a normal cleanup continuation.

```python
def close(enabled):
    try:
        raise ValueError
    except ValueError:
        return 1
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,4,5,7,8],"excluded":[],"possible_arcs":[],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7,8]}]}
```
