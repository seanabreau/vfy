# Compiler-removed try else

CPython removes the outer `else` assignment when the try suite's constant loop
returns. The loop's alternative exit leads directly to cleanup.

```python
def close(enabled):
    try:
        while True:
            return 1
        else:
            value = 2
    except ValueError:
        raise
    else:
        value = 3
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,4,7,8,12,13],"excluded":[],"possible_arcs":[[3,4],[3,12],[12,-1],[12,13]],"no_branch":[3],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7,8,9,10,11,12,13]}]}
```
