from app.qty import format_qty, is_packaging, normalize_unit, parse_qty, to_stock_amount


def test_parse_qty():
    assert parse_qty("1 1/2") == 1.5 and parse_qty("1½") == 1.5 and parse_qty("1/4") == 0.25 and parse_qty("") is None
    assert parse_qty("2") == 2 and parse_qty("0.25") == 0.25 and parse_qty("½") == 0.5 and parse_qty("abc") is None


def test_format_qty():
    assert format_qty(1.3333) == "1 1/3" and format_qty(0.25) == "1/4" and format_qty(2.5) == "2 1/2" and format_qty(113.4) == "113"
    assert format_qty(None) == "" and format_qty(4) == "4" and format_qty(0.33) == "1/3" and format_qty(2.45) == "2.45"


def test_normalize_unit():
    assert normalize_unit("Tbs") == "tbsp" and normalize_unit("large") == "piece" and normalize_unit("Cups") == "cup"
    assert normalize_unit("T") == "tbsp" and normalize_unit("t") == "tsp" and normalize_unit("mL") == "ml"
    assert normalize_unit("") == "piece" and normalize_unit("Pounds") == "lb" and normalize_unit("Bunch") == "bunch"


def test_to_stock_amount_two_hops():
    conv = [("cup", "ml", 236.6), ("ml", "g", 0.53)]   # product-specific ml→g for flour
    assert round(to_stock_amount(1, "cup", "g", conv), 1) == 125.4


def test_to_stock_amount_reverse_and_missing():
    assert to_stock_amount(500, "g", "kg", [("kg", "g", 1000)]) == 0.5
    assert to_stock_amount(1, "can", "g", [("cup", "ml", 236.6)]) is None
    assert to_stock_amount(3, "g", "g", []) == 3


def test_alias_targets_are_canonical_units():
    from app.qty import _ALIASES
    from app.setup_grocy import UNITS
    assert set(_ALIASES.values()) <= set(UNITS)


def test_is_packaging():
    assert all(is_packaging(u) for u in ("packet", "Packets", "pouch"))
    assert not any(is_packaging(u) for u in ("can", "g", "tbsp"))
