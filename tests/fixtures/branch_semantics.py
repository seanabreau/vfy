def decisions(value, items):
    if value > 10:
        result = "large"
    elif value:
        result = "small"
    else:
        result = "zero"

    for item in items:
        if item < 0:
            continue
        if item == value:
            break
    else:
        result = "absent"

    while value > 0:
        value -= 1

    match result:
        case "large" if items:
            return [item for item in items if item > 0]
        case "small":
            return items[0] if items else None
        case _:
            return None


def exceptions(callable_):
    try:
        return callable_()
    except ValueError:
        return 0
    finally:
        callable_ = None


def structural_partials(value):
    while True:
        if value:  # pragma: no branch
            break
        value += 1
    if 0:
        value += 2
    return value
