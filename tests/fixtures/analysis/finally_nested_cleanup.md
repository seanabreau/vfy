# Nested abrupt cleanup

An inner finally with no normal entry does not create a normal exit for the outer try.
Its explicit cleanup destinations remain reportable.

```python
def close(enabled):
    try:
        try:
            return 1
        finally:
            if enabled:
                save()
            cleanup()
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,4,6,7,8,10,11],"excluded":[],"possible_arcs":[[6,7],[6,8]],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7,8,9,10,11]}]}
```
