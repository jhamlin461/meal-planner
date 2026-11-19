"""Places: Grocy's own locations are the truth; nothing here caches them."""
from app.setup_grocy import LOCATIONS, ids


def places(grocy) -> dict[str, int]:
    """name -> id: the starting places first, in their usual order, then any others by name."""
    have = ids(grocy).location
    first = [n for n in LOCATIONS if n in have]
    return {n: have[n] for n in first + sorted((n for n in have if n not in first), key=str.lower)}


def default_place(grocy) -> str:
    p = places(grocy)
    return "Pantry" if "Pantry" in p else next(iter(p), "")


def _clean(grocy, name: str, own_id: int | None = None) -> str:
    name = name.strip()
    if not name:
        raise ValueError("Give the place a name.")
    if any(n.lower() == name.lower() and i != own_id for n, i in places(grocy).items()):
        raise ValueError(f"There's already a place called {name}.")
    return name


def add_place(grocy, name: str) -> int:
    return grocy.create("locations", {"name": _clean(grocy, name)})


def rename_place(grocy, id: int, name: str) -> None:
    grocy.update("locations", id, {"name": _clean(grocy, name, id)})


def remove_place(grocy, id: int, move_to: int) -> None:
    if any(int(e["location_id"]) == id and float(e["amount"]) > 0 for e in grocy.stock_entries()):
        label = next((n for n, i in places(grocy).items() if i == id), "that place")
        raise ValueError(f"Move or use up what's in {label} first.")
    for p in grocy.objects("products"):
        if p.get("location_id") is not None and int(p["location_id"]) == id:
            grocy.update("products", int(p["id"]), {"location_id": move_to})
    grocy.delete("locations", id)
