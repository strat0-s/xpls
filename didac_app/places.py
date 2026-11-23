"""Keeps city, state and district apart: a state never sits in the city field, and a city never in the state field."""
from __future__ import annotations

import re
from difflib import get_close_matches

STATES = {
    "Andhra Pradesh": "ap", "Arunachal Pradesh": "ar", "Assam": "", "Bihar": "", "Chhattisgarh": "cg chattisgarh", "Goa": "", "Gujarat": "gj", "Haryana": "hr",
    "Himachal Pradesh": "hp", "Jharkhand": "jh", "Karnataka": "ka", "Kerala": "kl", "Madhya Pradesh": "mp", "Maharashtra": "mh", "Manipur": "", "Meghalaya": "",
    "Mizoram": "", "Nagaland": "", "Odisha": "orissa od", "Punjab": "pb", "Rajasthan": "rj", "Sikkim": "", "Tamil Nadu": "tn tamilnadu", "Telangana": "ts tg",
    "Tripura": "", "Uttar Pradesh": "up", "Uttarakhand": "uttaranchal uk", "West Bengal": "wb", "Andaman and Nicobar Islands": "andaman nicobar",
    "Chandigarh": "", "Dadra and Nagar Haveli and Daman and Diu": "daman diu dnh", "Delhi": "nct of delhi new delhi dl", "Jammu and Kashmir": "jk j&k",
    "Ladakh": "", "Lakshadweep": "", "Puducherry": "pondicherry",
}
CITY_STATES = {"Delhi", "Chandigarh", "Puducherry", "Goa"}  # also a city: kept in the city field as well
_NAMES = {}
for _name, _alias in STATES.items():
    for _word in [_name, *_alias.split()]:
        _NAMES[re.sub(r"[^a-z&]", "", _word.lower())] = _name
_NAMES["nctofdelhi"] = _NAMES["newdelhi"] = "Delhi"  # multi-word aliases split above, rebuilt here
_NAMES.pop("nct", None), _NAMES.pop("of", None), _NAMES.pop("new", None)


def state_name(text: str) -> str:
    """The standard state or union territory for 'U.P.', 'Karnataka', 'KA', 'Tamilnadu'..., or '' when the text is not one."""
    key = re.sub(r"[^a-z&]", "", text.lower())
    if not key:
        return ""
    if key in _NAMES:
        return _NAMES[key]
    close = get_close_matches(key, [k for k in _NAMES if len(k) > 4], n=1, cutoff=0.88)  # a misspelling, never an abbreviation
    return _NAMES[close[0]] if close else ""


def tidy(city: str = "", state: str = "", district: str = "") -> tuple[str, str, str]:
    """(city, state, district) with each value in its own field. A state written after the city ("Bangalore Karnataka", "Pune, Maharashtra") moves to state;
    a state field holding something that is not a state is moved to city (or district when the city is taken). Several cities are kept, joined with ', '."""
    city, state, district = (re.sub(r"\s*[\n;]\s*", ", ", value or "").strip(" ,") for value in (city, state, district))
    found = state_name(state)
    if state and not found:
        tail = _state_suffix(state)  # "Bengaluru Karnataka" typed into the state field
        if tail:
            state, found = state[: -len(tail[0])].strip(" ,-"), tail[1]
            city = city or state
        elif not city:
            city = state
        elif not district and state.lower() != city.lower():
            district = state
        state = found
    state = found or state
    parts = []
    for part in (p.strip() for p in re.split(r",", city) if p.strip()):
        name = state_name(part)
        if name and name not in CITY_STATES and not state:  # the whole part is a state
            state = name
            continue
        if name and name not in CITY_STATES:
            continue  # a repeated state
        tail = _state_suffix(part)
        if tail and part != tail[0]:  # "Bangalore Karnataka"
            part = part[: -len(tail[0])].strip(" -")
            state = state or tail[1]
        elif name:
            state = state or name
        parts.append(part)
    return ", ".join(parts), state, district


def _state_suffix(text: str) -> tuple[str, str] | None:
    """(the words at the end of text, the state they name) when the last one to four words are a state."""
    words = text.split()
    for size in range(min(4, len(words)), 0, -1):
        tail = " ".join(words[-size:])
        name = state_name(tail) if size > 1 or len(words[-1]) > 2 else ""  # a lone 2-letter word at the end is more likely part of a name than "KA"
        if name and name not in CITY_STATES:  # "Old Goa", "North Delhi" are cities
            return tail, name
    return None
