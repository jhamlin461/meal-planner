"""Cooking-for-Engineers table: model, validation and grid layout."""
from typing import Literal

from pydantic import BaseModel

from app.qty import format_qty


class Ingredient(BaseModel):
    qty: float | None
    unit: str
    name: str
    note: str = ""  # prep words ("minced") and/or the row's purpose in parentheses ("(for the chicken)")
    packets: float | None = None  # meal-kit packets for the recipe's base servings; None: not from the box


class Op(BaseModel):
    id: str
    label: str
    inputs: list[str]  # "i<index>" (ingredient) or another op's id
    where: str = ""  # the bowl, pot or pan it happens in ("in a large pot", "same pot")


class CfeTable(BaseModel):
    pre: list[str]
    ingredients: list[Ingredient]
    ops: list[Op]


class Cell(BaseModel):
    text: str
    rowspan: int
    colspan: int
    kind: Literal["ingredient", "op", "filler"]
    ref: str = ""  # the ingredient ("i<index>") or op id the cell shows; fillers have none
    where: str = ""
    amount: str = ""  # ingredient cells: the amount, name, note (as note_text joins it) and packet count apart
    name: str = ""
    note: str = ""
    packets: str = ""


def amount_text(ing: Ingredient, scale: float) -> str:
    qty = format_qty(ing.qty * scale) if ing.qty is not None else ""
    unit = "" if ing.unit == "piece" else ing.unit
    return " ".join(p for p in (qty, unit) if p)


def ingredient_text(ing: Ingredient, scale: float) -> str:
    return " ".join(p for p in (amount_text(ing, scale), ing.name) if p)


def note_text(note: str) -> str:
    """The note as it follows the name: "(for the chicken)" after a space, prep words after a comma."""
    return "" if not note else f" {note}" if note.startswith("(") else f", {note}"


def line_text(ing: Ingredient, scale: float) -> str:
    """The ingredient in words, as the recipe card reads: "4 oz grape tomatoes, halved (raw, added later)"."""
    return ingredient_text(ing, scale) + note_text(ing.note)


def packet_text(n: float) -> str:
    """1 -> "1 packet", 0.5 -> "half a packet", 1.5 -> "1 1/2 packets"."""
    q = format_qty(n)
    return "half a packet" if q == "1/2" else f"{q} packet{'s' if n > 1 and q != '1' else ''}"


def packets_of(ing: Ingredient, scale: float = 1.0) -> str:
    """The packet count shown beside an ingredient ("1 1/2 packets" at 1.5x a 1-packet card), blank when it isn't
    from the box. Every place that shows packets asks this; layout's `packets` flag says whether the table does."""
    return packet_text(ing.packets * scale) if ing.packets else ""


def _is_ing(ref: str) -> bool:
    return ref.startswith("i") and ref[1:].isdigit()


def _rows(t: CfeTable, ref: str, seen: frozenset = frozenset()) -> set[int]:
    """Rows covered by a ref; unknown refs and cycles yield nothing."""
    if _is_ing(ref):
        return {int(ref[1:])}
    ops = {o.id: o for o in t.ops}
    if ref not in ops or ref in seen:
        return set()
    return set().union(*(_rows(t, r, seen | {ref}) for r in ops[ref].inputs))


def validate(t: CfeTable) -> list[str]:
    errs: list[str] = []
    ids = [o.id for o in t.ops]
    for i in {x for x in ids if ids.count(x) > 1}:
        errs.append(f"op id {i} is duplicated")
    refs = {f"i{k}" for k in range(len(t.ingredients))} | set(ids)
    used: dict[str, list[str]] = {}
    for o in t.ops:
        if not o.inputs:
            errs.append(f"{o.id} has no inputs")
        for r in o.inputs:
            if r not in refs:
                errs.append(f"{o.id} input {r} does not exist")
            used.setdefault(r, []).append(o.id)
    for r in sorted(refs):
        n = len(used.get(r, []))
        if r in ids and n == 0:
            continue  # root candidate, counted below
        if _is_ing(r) and n != 1:
            errs.append(f"{r} is not used by any op" if n == 0 else f"{r} is used by {n} ops")
        elif n > 1:
            errs.append(f"{r} is an input of {n} ops")
    roots = [i for i in ids if i not in used]
    if len(roots) != 1:
        errs.append(f"expected exactly one root op, found {len(roots)}: {roots}")
    for o in t.ops:
        rows = sorted(_rows(t, o.id))
        if rows and rows != list(range(rows[0], rows[-1] + 1)):
            errs.append(f"{o.id} covers non-contiguous rows {rows}")
    return errs


def _cols(t: CfeTable) -> dict[str, int]:
    ops = {o.id: o for o in t.ops}
    memo: dict[str, int] = {}

    def col(ref: str) -> int:
        if _is_ing(ref):
            return 0
        if ref not in memo:
            memo[ref] = 1 + max(col(r) for r in ops[ref].inputs)
        return memo[ref]

    for o in t.ops:
        col(o.id)
    return memo


def ncols(t: CfeTable) -> int:
    """Number of grid columns (1 + the root op's column); call on a valid table."""
    return 1 + max(_cols(t).values(), default=0)


def layout(t: CfeTable, scale: float = 1.0, packets: bool = True) -> list[list[Cell]]:
    """One list per ingredient row holding only the cells that start in that row, left to right. `packets`: whether
    ingredient cells carry their packet count (the recipe page shows it only when cooking from a meal kit).

    Raises ValueError if validate() reports errors. Use ncols(t) for the table width.
    """
    errs = validate(t)
    if errs:
        raise ValueError("; ".join(errs))
    cols = _cols(t)
    col = lambda r: 0 if _is_ing(r) else cols[r]  # noqa: E731
    starts: list[list[tuple[int, Cell]]] = [[(0, Cell(
        text=ingredient_text(g, scale), rowspan=1, colspan=1, kind="ingredient", ref=f"i{n}",
        amount=amount_text(g, scale), name=g.name, note=note_text(g.note),
        packets=packets_of(g, scale) if packets else ""))] for n, g in enumerate(t.ingredients)]
    for o in t.ops:
        rows = sorted(_rows(t, o.id))
        starts[rows[0]].append((col(o.id), Cell(text=o.label, rowspan=len(rows), colspan=1, kind="op", ref=o.id,
                                                       where=o.where)))
        run: list[str] = []  # consecutive ingredient inputs awaiting one merged filler

        def flush():
            if run:
                rr = sorted(int(r[1:]) for r in run)
                starts[rr[0]].append((1, Cell(text="", rowspan=len(rr), colspan=col(o.id) - 1, kind="filler")))
                run.clear()

        for r in sorted(o.inputs, key=lambda r: min(_rows(t, r))):
            if col(r) >= col(o.id) - 1:
                flush()
            elif _is_ing(r):
                run.append(r)
            else:
                flush()
                rr = sorted(_rows(t, r))
                starts[rr[0]].append((col(r) + 1, Cell(text="", rowspan=len(rr), colspan=col(o.id) - 1 - col(r), kind="filler")))
        flush()
    return [[c for _, c in sorted(row, key=lambda p: p[0])] for row in starts]
