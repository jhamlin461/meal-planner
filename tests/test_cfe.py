from pathlib import Path

import jinja2

from app.cfe import CfeTable, Ingredient, Op, layout, ncols, validate

BROWNIE = CfeTable(
    pre=["Butter and flour an 8x8-in pan", "Preheat oven to 350°F (170°C)"],
    ingredients=[Ingredient(qty=4, unit="oz", name="unsalted butter"), Ingredient(qty=1, unit="cup", name="sugar"),
                 Ingredient(qty=0.25, unit="tsp", name="vanilla extract"), Ingredient(qty=4, unit="tbsp", name="espresso"),
                 Ingredient(qty=2, unit="piece", name="large eggs"), Ingredient(qty=0.5, unit="cup", name="all-purpose flour"),
                 Ingredient(qty=1/3, unit="cup", name="cocoa powder"), Ingredient(qty=0.25, unit="tsp", name="baking soda"),
                 Ingredient(qty=0.25, unit="tsp", name="table salt")],
    ops=[Op(id="o1", label="melt", inputs=["i0"]), Op(id="o2", label="mix", inputs=["o1", "i1", "i2", "i3"]),
         Op(id="o3", label="mix", inputs=["o2", "i4"]), Op(id="o4", label="fold in", inputs=["o3", "i5", "i6", "i7", "i8"]),
         Op(id="o5", label="bake 350°F 30 to 40 min", inputs=["o4"])])


def shape(grid): return [[(c.text, c.rowspan, c.colspan) for c in row] for row in grid]


def test_brownie_layout():
    g = shape(layout(BROWNIE))
    assert g[0] == [("4 oz unsalted butter", 1, 1), ("melt", 1, 1), ("mix", 4, 1), ("mix", 5, 1), ("fold in", 9, 1), ("bake 350°F 30 to 40 min", 9, 1)]
    assert g[1] == [("1 cup sugar", 1, 1), ("", 3, 1)]
    assert g[4] == [("2 large eggs", 1, 1), ("", 1, 2)]
    assert g[5] == [("1/2 cup all-purpose flour", 1, 1), ("", 4, 3)]
    assert g[8] == [("1/4 tsp table salt", 1, 1)]


def test_layout_scales_quantities():
    assert layout(BROWNIE, scale=2)[0][0].text == "8 oz unsalted butter"


def test_validate_catches_unused_and_noncontiguous():
    bad = BROWNIE.model_copy(update={"ops": [Op(id="o1", label="mix", inputs=["i0", "i2"])]})
    errs = validate(bad)
    assert any("contiguous" in e for e in errs) and any("i1" in e for e in errs)


def test_repeated_ingredient_rows_are_valid():
    t = CfeTable(pre=[], ingredients=[Ingredient(qty=1, unit="cup", name="sugar"), Ingredient(qty=2, unit="piece", name="eggs"),
                                      Ingredient(qty=1, unit="tbsp", name="sugar")],
                 ops=[Op(id="a", label="whip", inputs=["i0", "i1"]), Op(id="b", label="sprinkle", inputs=["a", "i2"])])
    assert validate(t) == []


def test_partial_renders_brownie():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(Path(__file__).parent.parent / "app" / "templates"))
    html = env.get_template("_cfe_table.html").render(pre=BROWNIE.pre, grid=layout(BROWNIE), ncols=ncols(BROWNIE))
    assert 'rowspan="9"' in html and f'colspan="{ncols(BROWNIE)}"' in html and "Preheat oven" in html
