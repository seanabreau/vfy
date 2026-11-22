# Returning try else suite

Normal completion enters the else suite, whose return leaves no normal cleanup
continuation.

```python
def close(enabled):
    try:
        save()
    except ValueError:
        raise
    else:
        return 1
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,4,5,7,9,10],"excluded":[],"possible_arcs":[],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7,8,9,10]}]}
```
