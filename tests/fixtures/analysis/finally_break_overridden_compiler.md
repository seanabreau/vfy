# Compiler-removed else after overridden break

The inner cleanup raises before a `break` can leave the constant loop. CPython
removes the outer `else` assignment, while Coverage.py retains the loop's
syntactic break exit for branch analysis.

```python
def close(enabled):
    try:
        while True:
            try:
                break
            finally:
                raise ValueError
    except ValueError:
        raise
    else:
        value = 3
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,4,5,7,8,9,13,14],"excluded":[],"possible_arcs":[[13,-1],[13,14]],"no_branch":[3],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7,8,9,10,11,12,13,14]}]}
```
