"""Page numbers for the long lists: show everything, a page at a time."""
from __future__ import annotations

PER_PAGE = (10, 25, 50, 100)


def paginate(items: list, page: int = 1, per: int = 10) -> dict:
    """One page of items plus what the page controls need: totals, 'Showing 1-10 of 312' and the page numbers (None marks a gap)."""
    per = per if per in PER_PAGE else PER_PAGE[0]
    total = len(items)
    pages = max(1, -(-total // per))
    page = min(max(page, 1), pages)
    start = (page - 1) * per
    wanted = sorted({1, pages, *range(page - 1, page + 2)} & set(range(1, pages + 1)))
    numbers = [n for i, current in enumerate(wanted) for n in ([None, current] if i and current - wanted[i - 1] > 1 else [current])]
    return {"items": items[start:start + per], "page": page, "pages": pages, "total": total, "per": per, "first": start + 1 if total else 0,
            "last": min(start + per, total), "numbers": numbers}
