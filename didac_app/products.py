"""Matches what a form or a message says about products to the admin's list."""
from __future__ import annotations

import re
from difflib import get_close_matches


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def match_products(value, listed: list[str]) -> tuple[list[str], list[str]]:
    """(names from the list, anything else) for what the reader returned: a list, or text split at commas, slashes and 'and'. A near spelling ("Tiqer", "Math-Max") counts as the listed name."""
    items = value if isinstance(value, list) else re.split(r"[,;/\n]| and | & ", str(value or ""))
    by_key = {_key(name): name for name in listed}
    matched, other = [], []
    for item in (str(i).strip() for i in items):
        if not item:
            continue
        hit = by_key.get(_key(item)) or next(iter(by_key[k] for k in get_close_matches(_key(item), by_key, n=1, cutoff=0.8)), "")
        if hit:
            if hit not in matched:
                matched.append(hit)
        else:
            other.append(item)
    return matched, other
