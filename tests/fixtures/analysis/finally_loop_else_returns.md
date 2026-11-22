# Returning loop alternatives remove the try else

Both outcomes of the loop return. CPython omits the outer `else` assignment,
so the cleanup conditional has no normal exit from the try suite.

```python
def close(enabled):
    try:
        while enabled:
            return 1
        else:
            return 2
    except ValueError:
        raise
    else:
        value = 3
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,4,6,7,8,12,13],"excluded":[],"possible_arcs":[[3,4],[3,6]],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7,8,9,10,11,12,13]}]}
```
