"""Local demo for the visual check: the real app on FakeGrocy seed data. Run: uv run python -m tests.demo_server [port]"""
import socket
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn

from app.ai import Picks, RecipeDraft
from app.config import Settings
from app.home import add_starters
from app.house import HouseStore
from app.importer import IngredientMatch, PendingStore, put_photo, save_recipe, to_jpeg
from app.mealplan import plan_meal
from app.reminders import washer_finished
from app.setup_grocy import bootstrap, ids, supplies_group
from app.web import create_app
from tests.fakes import FakeAI, FakeGrocy, FakeHA
from tests.test_cfe import BROWNIE
from tests.test_web import soup_draft


def dish(color=(92, 58, 40)) -> bytes:
    """A stand-in dish photo: a warm plate on a darker table."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1200, 900), (40, 34, 30))
    d = ImageDraw.Draw(img)
    d.ellipse((200, 100, 1000, 850), fill=(230, 226, 218))
    d.ellipse((330, 230, 870, 720), fill=color)
    return to_jpeg(img)


def seed(data: Path, settings: Settings):
    g, today = FakeGrocy(), date.today()
    bootstrap(g)
    i = ids(g)
    house = HouseStore(data / "house.db")
    for uid, name in ((3, "Josh"), (4, "Cat")):
        g.create_user(name.lower(), name)
        house.save_person(name, uid, f"person.{name.lower()}", f"mobile_app_{name.lower()}")
    ha = FakeHA()
    ha.states = {"person.josh": "home", "person.cat": "home"}

    def product(name, unit="g", loc="Pantry", **kw):
        return g.create("products", {"name": name, "qu_id_stock": i.unit[unit], "location_id": i.location[loc],
                                     "default_best_before_days": 5, **kw})

    group = supplies_group(g)
    bags = product("Heavy duty drawstring kitchen trash bags, 13 gallon", "piece", "Garage", product_group_id=group,
                   min_stock_amount=2, default_best_before_days=-1)
    g.add_stock(bags, 1, None, i.location["Garage"])
    tp = product("Toilet paper", "piece", "Bathroom", product_group_id=group, min_stock_amount=4,
                 default_best_before_days=-1)
    g.add_stock(tp, 12, None, i.location["Bathroom"])
    g.volatile_result["missing_products"] = [{"id": bags, "name": g.get_object("products", bags)["name"],
                                              "amount_missing": 1}]
    add_starters(g, house, house.people())  # litter and trash chores, taking turns
    washer_finished(g, house, datetime.now() - timedelta(minutes=20))  # the laundry task

    pids = [product(ing.name) for ing in BROWNIE.ingredients]
    wheres = ["in a small pot", "same pot", "same pot", "in a large bowl", "in the 8x8 pan"]
    table = BROWNIE.model_copy(update={"ops": [o.model_copy(update={"where": w}) for o, w in zip(BROWNIE.ops, wheres)]})
    draft = RecipeDraft(title="Fudgy brownies", servings=4, steps=["Mix", "Bake"], table=table)
    rid = save_recipe(g, draft, [IngredientMatch(n, p, 100) for n, p in enumerate(pids)], None, None)
    put_photo(g, rid, dish())
    for name in ("Weeknight chili", "Sheet pan chicken with a rather long name and lemons"):
        g.create("recipes", {"name": name, "base_servings": 4})
    plan_meal(g, today, "Dinner", rid, 4, settings, today=today)

    soon = []
    for name, unit, loc, amount, days in (("Greek yogurt", "g", "Fridge", 500, 1), ("Eggs", "piece", "Fridge", 12, 3),
                                          ("Baby spinach", "g", "Fridge", 140, -1), ("Basmati rice", "g", "Pantry", 2000, 400),
                                          ("Frozen peas", "g", "Freezer", 900, 120)):
        pid = product(name, unit, loc)
        bb = today + timedelta(days=days)
        g.add_stock(pid, amount, bb, i.location[loc])
        if days <= 3:
            soon.append({"product_id": pid, "amount": amount, "best_before_date": bb.isoformat()})
    g.volatile_result["due_products"] = [s for s in soon if s["best_before_date"] >= today.isoformat()]
    g.volatile_result["expired_products"] = [s for s in soon if s["best_before_date"] < today.isoformat()]
    return g, house, ha


def main():
    data = Path(tempfile.mkdtemp(prefix="house-demo-"))
    settings = Settings("", "", "", "KEY", None, 6, data, 3)  # no ha_url: no reminder loop
    g, house, ha = seed(data, settings)
    store = PendingStore(data / "pending")  # one import ready to review, one that failed
    ready = store.new("https://example.com/a-very-long-recipe-address/that-keeps-going/soup-with-dragon-eggs")
    store.save(ready.model_copy(update={"status": "ready", "draft": soup_draft()}))
    store.save_photo(ready.id, dish((170, 120, 40)))
    failed = store.new("grandmas-cards.pdf")
    store.save(failed.model_copy(update={"status": "failed", "error": "No recipe found in this PDF."}))
    ai = FakeAI(draft=soup_draft(), picks=Picks(items=[]))
    app = create_app(g, ai, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
                     settings, store, house=house, ha=ha)
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    else:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
    print(f"demo on http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
