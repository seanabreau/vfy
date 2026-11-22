# Excluded alternate clauses

Pragmas on both branches exclude the complete conditional, including its
multiline import suites.

```python
import sys

if sys.version_info >= (3, 13):  # pragma: no cover
    from inspect import iscoroutinefunction
else:  # pragma: no cover
    from asyncio import iscoroutinefunction
```

```json
{"executable":[1],"excluded":[3,4,5,6],"possible_arcs":[],"no_branch":[],"regions":[]}
```
