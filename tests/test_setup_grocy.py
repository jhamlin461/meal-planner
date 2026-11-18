from app.setup_grocy import UNITS, bootstrap, ids
from tests.fakes import FakeGrocy


def counts(g):
    return {e: len(t) for e, t in g.entities.items()}


def test_bootstrap_creates_everything():
    g = FakeGrocy()
    bootstrap(g)
    assert len(g.objects("quantity_units")) == 13
    convs = g.objects("quantity_unit_conversions")
    assert len(convs) == 8 and all(c["product_id"] is None for c in convs)
    assert len(g.objects("locations")) == 10
    assert len(g.objects("meal_plan_sections")) == 2
    assert len(g.objects("shopping_lists")) == 3
    assert len(g.objects("userfields")) == 11
    i = ids(g)
    assert set(i.unit) == set(UNITS) and set(i.list) == {"Meal plan", "Extras", "Running low"}
    kg_g = [c for c in convs if c["from_qu_id"] == i.unit["kg"] and c["to_qu_id"] == i.unit["g"]]
    assert kg_g[0]["factor"] == 1000


def test_bootstrap_is_idempotent():
    g = FakeGrocy()
    bootstrap(g)
    before = counts(g)
    bootstrap(g)
    assert counts(g) == before
