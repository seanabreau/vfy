# Decorated ellipsis bodies

The configured ellipsis exclusion removes the protocol method and decorated
overload declarations from executable lines. The import, protocol class, and
runtime implementation remain executable. Arc collection is disabled; this
fixture checks executable and excluded lines and the retained code regions.

```python
from typing import Protocol, overload


class Reader(Protocol):
    def read(self, size: int = ...) -> bytes: ...


@overload
def convert(value: str) -> bytes: ...
@overload
def convert(value: bytes) -> str: ...


def convert(value):
    return value
```

```json
{"executable":[1,4,14,15],"excluded":[5,6,7,8,9,10,11,12,13],"possible_arcs":[],"no_branch":[],"collect_arcs":false,"excludes":["^\\s*(((async )?def .*?)?[\\])]+(\\s*->.*?)?:\\s*)?\\.\\.\\.\\s*(#|$)"],"regions":[{"kind":"class","name":"Reader","start":4,"lines":[5]},{"kind":"function","name":"Reader.read","start":5,"lines":[5]},{"kind":"function","name":"convert","start":9,"lines":[9]},{"kind":"function","name":"convert","start":11,"lines":[11]},{"kind":"function","name":"convert","start":14,"lines":[15]}]}
```
