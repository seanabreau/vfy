# Nested with statements

Coverage.py rewrites the arcs that leave a `with` statement through its header.
When a `with` statement ends the suite of a `with` statement that itself ends
an enclosing `with` suite, the rewrite keeps an arc from the innermost header
(line 11) to the statement after the outermost one, so that header has two
branches. Two levels (lines 2-3) and a chain broken by a following statement
(lines 17-19) have none.

```python
def two_levels():
    with first():
        with second():
            work()
    done()


def three_levels():
    with first():
        with second():
            with third():
                work()
    done()


def inner_followed_by_statement():
    with first():
        with second():
            with third():
                work()
            work()
    done()
```

```json
{"executable":[1,2,3,4,5,8,9,10,11,12,13,16,17,18,19,20,21,22],"excluded":[],"possible_arcs":[[11,12],[11,13]],"no_branch":[],"regions":[{"kind":"function","name":"two_levels","start":1,"lines":[2,3,4,5]},{"kind":"function","name":"three_levels","start":8,"lines":[9,10,11,12,13]},{"kind":"function","name":"inner_followed_by_statement","start":16,"lines":[17,18,19,20,21,22]}]}
```
