# Exhaustive match removes try else

Both match cases return, so CPython removes the try suite's `else` assignment
and cleanup has no fallthrough branch.

```python
def close(flag):
    try:
        match flag:
            case True:
                return 1
            case _:
                return 2
    except ValueError:
        raise
    else:
        value = 3
    finally:
        if flag:
            pass
```

```json
{"executable":[1,2,3,4,5,6,7,8,9,13,14],"excluded":[],"possible_arcs":[[4,5],[4,6]],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7,8,9,10,11,12,13,14]}]}
```
