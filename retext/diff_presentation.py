"""Small, bounded display projections for version-comparison text cells."""


def difference_spans(old: str, new: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """The differing middle, with identical prefixes/suffixes left unbolded.

    Linear even for newspaper-size/repetitive strings. This is display only,
    never an alignment rule used by the patch writer.
    """
    if old == new:
        return (0, 0), (0, 0)
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    old_end, new_end = len(old), len(new)
    while old_end > prefix and new_end > prefix and old[old_end - 1] == new[new_end - 1]:
        old_end -= 1
        new_end -= 1
    return (prefix, old_end), (prefix, new_end)
