"""Household measures: each product is shown and typed in a measure people use ("cups", "rolls", "eggs"); Grocy
keeps its stock unit (g, ml, piece) and every write converts with the product's factor (stock units per measure)."""
import math
from dataclasses import dataclass, field

from app.ai import AIError
from app.qty import format_qty, normalize_unit

KITCHEN = {"cup": ("vol", 236.6), "tbsp": ("vol", 14.79), "tsp": ("vol", 4.93), "fl oz": ("vol", 29.57),
           "oz": ("wt", 28.35), "lb": ("wt", 453.6)}  # ml / g per unit
BASE = {"vol": "ml", "wt": "g"}
STOCK_WORDS = {"g", "ml", "kg", "l"}  # never a measure: no gram value is shown or typed


@dataclass(frozen=True)
class Measure:
    word: str  # as shown for several: "cups", "rolls"; a stock unit name when the product has no measure yet
    factor: float  # stock units per 1 measure
    count: str = field(default="", compare=False)  # a count word it was in before a kitchen measure ("bottles")


def _key(word: str) -> str:
    """The kitchen unit a word names ("Cups" -> "cup"), else the word itself in lower case."""
    return normalize_unit(word)


def _shown(word: str) -> str:
    """Kitchen words as the select offers them ("cups", "tbsp"); count words as given."""
    k = _key(word)
    return "cups" if k == "cup" else k if k in KITCHEN else word.strip().lower()


def _one(word: str) -> str:
    """The singular: "cups" -> "cup", "boxes" -> "box", "rolls" -> "roll"; tbsp, oz, lb stay."""
    if _key(word) in KITCHEN:
        return _key(word)
    # ponytail: a short list of English endings; a word it gets wrong reads oddly only at exactly one
    for tail, keep in (("ches", "ch"), ("shes", "sh"), ("xes", "x"), ("sses", "ss"), ("rries", "rry"), ("aves", "af")):
        if word.endswith(tail):
            return word[: -len(tail)] + keep
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def label(m: Measure, n: float = 2) -> str:
    """The word beside an amount of n: "pieces" for a product counted in pieces without a measure yet."""
    if m.word == "piece":
        return "piece" if 0 < n <= 1 else "pieces"
    return _one(m.word) if 0 < n <= 1 else m.word


def _kitchen(x: float) -> float:
    """Nearest eighth or third (whole numbers from 10)."""
    if x >= 10:
        return round(x)
    whole = math.floor(x)
    return min((whole + n / d for d in (8, 3) for n in range(d + 1)), key=lambda v: abs(v - x))


def show(amount_stock: float, m: Measure) -> str:
    x = amount_stock / m.factor
    if 0 < x < 1 / 16:
        return f"less than 1/8 {label(m, 1)}"
    n = _kitchen(x)
    q = format_qty(n)
    return q if m.word == "piece" else f"{q} {label(m, n)}"


def value(amount_stock: float, m: Measure) -> str:
    """An amount in the measure for a number field ("2.5"), to 2 decimals."""
    return f"{round(amount_stock / m.factor, 2):g}"


def to_stock(amount: float, m: Measure) -> float:
    return amount * m.factor


def from_field(amount: float, m: Measure, shown: float) -> float:
    """A typed amount in stock units; `shown` (stock units) when the field still holds the value it was prefilled
    with, so saving an untouched form writes nothing new (the prefill is rounded)."""
    return shown if amount == float(value(shown, m)) else to_stock(amount, m)


def _ok(x) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def stock_unit(grocy, product: dict) -> str:
    qu = product.get("qu_id_stock")
    return next((u["name"] for u in grocy.objects("quantity_units") if str(u["id"]) == str(qu)), "")


def fixed(word: str, factor, stock: str) -> Measure | None:
    """Claude's pick made safe: a kitchen word in its own base unit gets the fixed factor; a stock unit word
    (grams) or a factor that isn't a positive number is no measure."""
    f, k = _ok(factor), _key(word or "")
    if not word or not word.strip() or k in STOCK_WORDS or f is None:
        return None
    if k in KITCHEN and BASE[KITCHEN[k][0]] == stock:
        f = KITCHEN[k][1]
    return Measure("pieces" if k == "piece" else _shown(word), f)


def fields(m: Measure, source: str) -> dict:
    return {"measure": m.word, "measure_factor": m.factor, "measure_source": source}


def of(grocy, ai, product: dict, stock: str | None = None) -> Measure:
    """The product's stored measure; else Claude's pick, asked once and stored (ai None: never asked); else the stock
    unit itself, until Claude is back. `stock`: the stock unit's name when the caller has it."""
    uf = grocy.userfields("products", int(product["id"]))
    if uf.get("measure") and _ok(uf.get("measure_factor")):
        return Measure(uf["measure"], float(uf["measure_factor"]), uf.get("measure_count") or "")
    stock = stock_unit(grocy, product) if stock is None else stock
    if ai is not None:
        try:
            g = ai.guess_measure(product["name"], stock)
            m = fixed(g.measure, g.factor, stock)
        except AIError:  # AIPaused in free mode: the stock unit until Claude is back
            m = None
        if m:
            grocy.set_userfields("products", int(product["id"]), fields(m, "estimate"))
            return m
    return Measure(stock, 1.0)


def choices(stock: str, m: Measure) -> list[str]:
    """The kitchen measures, plus the product's count word: the one it is in, or was in before a kitchen measure,
    else "pieces" for a product counted in pieces."""
    out = [_shown(k) for k in KITCHEN]
    count = m.word if m.word not in out and m.word != stock else m.count or ("pieces" if stock == "piece" else "")
    return out + [count] if count else out


def _per_base(grocy, product: dict, stock: str, kind: str) -> float | None:
    """Stock units per 1 ml (vol) or 1 g (wt) of this product: its own conversions, else its grams per unit."""
    units = {str(u["id"]): u["name"] for u in grocy.objects("quantity_units")}
    for c in grocy.objects("quantity_unit_conversions", product_id=product["id"]):
        k, to, f = _key(units.get(str(c["from_qu_id"]), "")), units.get(str(c["to_qu_id"])), _ok(c.get("factor"))
        if to == stock and f and k in KITCHEN and KITCHEN[k][0] == kind:
            return f / KITCHEN[k][1]
        if to == stock and f and k == BASE[kind]:
            return f
    gpu = _ok(grocy.userfields("products", int(product["id"])).get("g_per_unit"))
    return 1 / gpu if kind == "wt" and gpu and stock != "g" else None


def _factor(grocy, product: dict, stock: str, old: Measure, new: str) -> float | None:
    k, o = _key(new), _key(old.word)
    if k not in KITCHEN:
        if new == old.count:  # back to the count word it had
            return _ok(grocy.userfields("products", int(product["id"])).get("measure_count_factor"))
        return 1.0 if stock == "piece" else None  # a count word: one piece each
    kind, size = KITCHEN[k]
    if o in KITCHEN and KITCHEN[o][0] == kind:
        return old.factor * size / KITCHEN[o][1]
    if BASE[kind] == stock:
        return size
    per = _per_base(grocy, product, stock, kind)
    return size * per if per else None


def change(grocy, product: dict, word: str) -> Measure:
    """The person's measure ("owner": Claude and a link never replace it). The factor comes from fixed kitchen math
    within volume or weight, else the product's own conversion; a kitchen measure that carried the product's own
    weight-to-volume is kept as its conversion first, and a count word left for a kitchen measure is kept with its
    factor (measure_count), so switching back finds them. ValueError when unknown."""
    stock = stock_unit(grocy, product)
    old, new = of(grocy, None, product, stock), _shown(word)
    if new not in choices(stock, old):
        raise ValueError(f"Pick one of: {', '.join(choices(stock, old))}.")
    if new == old.word:
        return old
    f = _factor(grocy, product, stock, old, new)
    if f is None:
        raise ValueError(f"I can't tell how much {product['name']} is in {new} yet.")
    o = _key(old.word)
    if o in KITCHEN and BASE[KITCHEN[o][0]] != stock and _per_base(grocy, product, stock, KITCHEN[o][0]) is None:
        from app.importer import add_conversion  # importer imports this module's users
        add_conversion(grocy, int(product["id"]), o, old.factor)
    m = Measure(new, f)
    counted = o not in KITCHEN and old.word != stock  # leaving a count word (not the bare stock unit)
    kept = {"measure_count": old.word, "measure_count_factor": old.factor} if counted else {}
    grocy.set_userfields("products", int(product["id"]), fields(m, "owner") | kept)
    return m
