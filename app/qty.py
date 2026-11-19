import re
import unicodedata


_ALIASES = {
    "tbs": "tbsp", "tbsp": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "cups": "cup", "grams": "g", "gram": "g",
    "ounce": "oz", "ounces": "oz", "pound": "lb", "pounds": "lb", "lbs": "lb",
    "milliliter": "ml", "milliliters": "ml",
    **dict.fromkeys(["", "each", "large", "medium", "small", "whole"], "piece"),  # not cloves: garlic is stocked in bulbs
}


PACKAGING_UNITS = {"packet", "pouch", "sachet", "container", "package", "bag", "box"}


def is_packaging(unit: str) -> bool:
    """Meal-kit packaging words (never a real measure); 'can' is deliberately absent."""
    u = unit.strip().lower()
    return u in PACKAGING_UNITS or u.removesuffix("es") in PACKAGING_UNITS or u.removesuffix("s") in PACKAGING_UNITS


def normalize_unit(text: str) -> str:
    t = text.strip()
    if t == "T":  # case matters: T = tablespoon, t = teaspoon
        return "tbsp"
    if t == "t":
        return "tsp"
    low = t.lower()
    return _ALIASES.get(low, low)


def _num(s: str) -> float:
    if "/" in s:
        a, b = s.split("/")
        return int(a) / int(b)
    return float(s)


def parse_qty(text: str) -> float | None:
    s = "".join(f" {unicodedata.numeric(c)}" if unicodedata.category(c) == "No" and unicodedata.numeric(c, None) is not None
                else c for c in text).strip()
    if not s:
        return None
    try:
        return sum(_num(p) for p in s.split())
    except (ValueError, ZeroDivisionError):
        return None


def format_qty(x: float | None) -> str:
    if x is None:
        return ""
    if x >= 10:
        return str(round(x))
    whole = int(x)
    best = None
    for d in (8, 3):
        for n in range(d + 1):
            err = abs(x - (whole + n / d))
            if err <= 0.02 and (best is None or err < best[0]):
                best = (err, n, d)
    if best:
        _, n, d = best
        if n == d:
            return str(whole + 1)
        if n == 0:
            return str(whole)
        from math import gcd
        g = gcd(n, d)
        frac = f"{n // g}/{d // g}"
        return f"{whole} {frac}" if whole else frac
    return f"{x:.2f}".rstrip("0").rstrip(".")


def to_stock_amount(qty: float, unit: str, stock_unit: str,
                    conversions: list[tuple[str, str, float]]) -> float | None:
    if unit == stock_unit:
        return qty
    edges: dict[str, list[tuple[str, float]]] = {}
    for a, b, f in conversions:
        edges.setdefault(a, []).append((b, f))
        edges.setdefault(b, []).append((a, 1 / f))
    for mid, f1 in edges.get(unit, []):  # direct (and reverse), then one intermediate hop
        if mid == stock_unit:
            return qty * f1
    for mid, f1 in edges.get(unit, []):
        for end, f2 in edges.get(mid, []):
            if end == stock_unit:
                return qty * f1 * f2
    return None


# US kitchen units -> metric (unit, factor). Volumes go to ml; the AI prompt weighs solids in g instead.
_METRIC = {"cup": ("ml", 240), "tbsp": ("ml", 15), "tsp": ("ml", 5), "oz": ("g", 28.35), "lb": ("g", 453.6),
           "stick": ("g", 113)}


def to_metric(qty: float | None, unit: str) -> tuple[float | None, str]:
    """('3/4', cup) -> (180, ml); metric, counts and unknown units pass through unchanged."""
    to = _METRIC.get(normalize_unit(unit))
    if to is None:
        return qty, unit
    return (None if qty is None else round(qty * to[1], 1)), to[0]


def celsius(text: str) -> str:
    """'bake 350°F 30 min' -> 'bake 175°C 30 min' (nearest 5 degrees); '350°F (180°C)' keeps the card's own °C."""
    return re.sub(r"(\d{2,3})\s*(?:°\s*|degrees\s+)F\b(?:\s*\((\d{2,3})\s*°\s*C\))?",
                  lambda m: f"{m[2] or round((int(m[1]) - 32) * 5 / 9 / 5) * 5}°C", text)
