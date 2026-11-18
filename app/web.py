"""Web UI: pantry, scanning, recipes and recipe import (server-rendered; Grocy holds all data)."""
import logging
import math
import mimetypes
import threading
from datetime import date, timedelta
from urllib.parse import urlencode, urlsplit
from pathlib import Path
from typing import Literal

import anthropic
import httpx
import jinja2
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

from app.ai import AI, AIError
from app.barcode import Resolved, _generics, relink, resolve, resolve_from_photo
from app.cfe import CfeTable, Ingredient, Op, ingredient_text, layout, ncols, validate
from app.config import Settings, load_settings
from app.grocy import Grocy, GrocyError
from app.ha import HA, HAError
from app.house import HouseStore, Person
from app.importer import (IngredientMatch, PendingStore, add_conversion, create_generic, process, propose_matches,
                          save_recipe, stock_amount_for)
from app.qty import is_packaging
from app.mealplan import cook, plan_meal, unplan_meal, week_view
from app.nutrition import recipe_macros
from app.setup_grocy import LOCATIONS, SECTIONS, ids
from app.shopping import rebuild_meal_plan_list, walmart_items
from app.suggest import suggest, user_recipes
from app.walmart import cart_url, packages_needed, parse_item_id, search_url

HERE = Path(__file__).parent
NEVER = "2999-12-31"  # Grocy's best-before for "never expires"
log = logging.getLogger(__name__)
mimetypes.add_type("application/manifest+json", ".webmanifest")


def _num(s) -> float | None:
    """A positive finite number from a form field, else None."""
    try:
        x = float(s)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _int(s) -> int | None:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _fmt(x) -> str:
    """Number for display or a value= the user edits (no trailing .0; 6 significant digits)."""
    return "" if x is None else f"{float(x):g}"


def _exact(x) -> str:
    """Number the app reads back (hidden inputs, max=): never rounded up past stock, which Grocy refuses."""
    return "" if x is None else f"{float(x):.12g}"


def _servings(s: str) -> int | None:
    """Blank -> None (use the household default); otherwise a positive whole number or a 400."""
    if not s.strip():
        return None
    n = _int(s.strip())
    if n is None or n < 1:
        raise HTTPException(400, "Servings must be a whole number above 0")
    return n


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _factor(s: str) -> float:
    f = float(s)  # ValueError for non-numbers; its message is shown to the reviewer
    if not (math.isfinite(f) and f > 0):
        raise ValueError(f"conversion factor must be a finite number above 0, got {s}")
    return f


def _next_op_id(table: CfeTable) -> str:
    taken = {o.id for o in table.ops}
    return next(f"o{n}" for n in range(1, len(taken) + 2) if f"o{n}" not in taken)


def _table_from_form(form, old: CfeTable) -> CfeTable:
    """Rebuild the ops from the editor's parallel op_id/op_label/op_inputs fields; fully blank lines are dropped."""
    ops = [Op(id=i.strip(), label=lab.strip(), inputs=[r.strip() for r in inp.split(",") if r.strip()])
           for i, lab, inp in zip(form.getlist("op_id"), form.getlist("op_label"), form.getlist("op_inputs"))
           if lab.strip() or inp.strip()]
    return old.model_copy(update={"ops": ops})


def _cfe(table: CfeTable | None, scale: float = 1.0) -> dict | None:
    """Context for _cfe_table.html, or None when the table is missing or invalid (caller shows the fallback)."""
    if table is None or validate(table):
        return None
    try:
        return {"pre": table.pre, "grid": layout(table, scale), "ncols": ncols(table)}
    except ValueError:
        return None


def create_app(grocy, ai, http: httpx.Client, settings: Settings, store: PendingStore, *,
               house: HouseStore | None = None, ha=None) -> FastAPI:
    app = FastAPI(title="Meal planner")
    house = house or HouseStore(settings.data_dir / "house.db")
    if ha is None and settings.ha_url:
        ha = HA(settings.ha_url, settings.ha_token)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters["num"] = _fmt
    env.filters["exact"] = _exact
    env.globals["next_op_id"] = _next_op_id
    templates = Jinja2Templates(env=env)
    plan_lock = threading.Lock()  # plan add/remove, Bought, Cooked it, list rebuild: never interleaved or repeated
    last_picks: dict[date, list[dict]] = {}  # latest suggestions per week, until accepted or dismissed
    save_lock = threading.Lock()  # ponytail: one lock for all import saves; they are rare and per-household

    @app.middleware("http")
    async def no_cross_site_writes(request: Request, call_next):
        """No login, so refuse writes a browser says came from another site (a missing header is allowed: curl, HA)."""
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"status": "error", "message": "cross-site request refused"}, status_code=403)
        return await call_next(request)

    def page(request: Request, name: str, status_code: int = 200, **ctx):
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    @app.exception_handler(GrocyError)
    @app.exception_handler(httpx.TransportError)  # refused, timed out, dropped mid-response
    async def grocy_down(request: Request, exc: Exception):
        log.warning("Grocy unavailable: %s", exc)
        return page(request, "down.html", 503)

    def unit_names() -> dict[int, str]:
        return {v: k for k, v in ids(grocy).unit.items()}

    def recipe_or_404(rid: int) -> dict:
        r = grocy.get_object("recipes", rid)
        if r is None:
            raise HTTPException(404, "No such recipe")
        return r

    def load_table(uf: dict) -> CfeTable | None:
        try:
            return CfeTable.model_validate_json(uf["cfe_table"]) if uf.get("cfe_table") else None
        except ValidationError:
            return None

    def position_ingredients(positions: list[dict], products: dict[int, dict]) -> list[Ingredient]:
        units = unit_names()
        return [Ingredient(qty=float(p["amount"]), unit=units.get(_int(p.get("qu_id")), ""),
                           name=products.get(int(p["product_id"]), {}).get("name", f"product {p['product_id']}"))
                for p in positions]

    @app.get("/")
    def today(request: Request):
        return page(request, "today.html")

    @app.get("/home")
    def home(request: Request):
        return page(request, "home.html")

    # ---- people and device identity -----------------------------------------------------------------------------
    def current_who(request: Request) -> Person | str | None:
        """The person this device belongs to, "kitchen" for the shared screen, or None when not chosen yet."""
        v = request.cookies.get("who")
        return "kitchen" if v == "kitchen" else house.person(_int(v)) if _int(v) is not None else None

    @app.get("/who")
    def who_get(request: Request):
        return page(request, "who.html", people=house.people(), who=current_who(request))

    @app.post("/who")
    def who_post(who: str = Form()):
        if who != "kitchen" and house.person(_int(who) or 0) is None:
            raise HTTPException(400, "No such person")
        r = back("/")
        r.set_cookie("who", who, max_age=400 * 24 * 3600, samesite="lax")
        return r

    def ha_choices() -> dict:
        """HA persons, phones and each person's own phone; empty (and `ha_ok` False) when HA is missing or down."""
        if ha is None:
            return {"ha_ok": False, "ha_persons": {}, "ha_services": [], "phone_of": {}}
        try:
            persons, services, trackers = ha.persons(), ha.notify_services(), ha.person_trackers()
        except HAError as e:
            log.warning("Home Assistant unavailable: %s", e)
            return {"ha_ok": False, "ha_persons": {}, "ha_services": [], "phone_of": {}}
        phone_of = {p: next((s for t in ts for s in services if s == t.replace("device_tracker.", "mobile_app_", 1)), "")
                    for p, ts in trackers.items()}
        return {"ha_ok": True, "ha_persons": persons, "ha_services": services, "phone_of": phone_of}

    def match_person(name: str, persons: dict[str, str]) -> str:
        n = name.strip().lower()
        return next((e for e, f in persons.items() if n and (n in f.lower() or n in e.lower())), "")

    @app.get("/home/people")
    def people_get(request: Request, name: str = "", ha_person: str = ""):
        c = ha_choices()
        ha_person = ha_person or match_person(name, c["ha_persons"])
        return page(request, "people.html", people=house.people(), who=current_who(request), typed=name,
                    sel_person=ha_person, sel_service=c["phone_of"].get(ha_person, ""), **c)

    @app.post("/home/people")
    def people_post(name: str = Form(), ha_person: str = Form(""), notify_service: str = Form("")):
        name = name.strip()
        if not name:
            raise HTTPException(400, "Type a name")
        c = ha_choices()
        ha_person = ha_person or match_person(name, c["ha_persons"])
        notify_service = notify_service or c["phone_of"].get(ha_person, "")
        username = name.lower().replace(" ", "")
        uid = next((int(u["id"]) for u in grocy.users() if u["username"] == username), None)             or grocy.create_user(username, name)
        house.save_person(name, uid, ha_person, notify_service)
        return back("/home/people")

    @app.post("/home/people/{pid}/delete")
    def people_delete(pid: int):
        house.delete_person(pid)  # the Grocy user stays: it may own history there
        return back("/home/people")

    @app.post("/settings/theme")
    def set_theme(request: Request, theme: Literal["dark", "light"] = Form()):
        """Per-device theme; back to the page it was switched on (same-site paths only)."""
        ref = urlsplit(request.headers.get("referer", ""))
        ok = ref.path.startswith("/") and not ref.path.startswith(("//", "/\\"))
        r = back(ref.path + (f"?{ref.query}" if ref.query else "") if ok else "/")
        r.set_cookie("theme", theme, max_age=400 * 24 * 3600, samesite="lax")
        return r

    # ---- pantry -------------------------------------------------------------------------------------------------
    @app.get("/pantry")
    def pantry(request: Request):
        products = {int(p["id"]): p for p in grocy.objects("products")}
        i = ids(grocy)
        units, locs = unit_names(), {v: k for k, v in i.location.items()}
        name = lambda pid: products.get(int(pid), {}).get("name", f"product {pid}")  # noqa: E731
        vol = grocy.volatile(settings.expiry_warn_days)
        soon = [{"name": name(r["product_id"]), "date": r.get("best_before_date"), "kind": kind}
                for key, kind in (("expired_products", "expired"), ("overdue_products", "overdue"),
                                  ("due_products", "use soon")) for r in vol.get(key) or []]
        rows: dict[tuple[int, int], dict] = {}
        totals: dict[int, float] = {}
        for e in grocy.stock_entries():
            pid, loc = int(e["product_id"]), _int(e.get("location_id")) or 0
            row = rows.setdefault((pid, loc), {"pid": pid, "loc": loc, "name": name(pid), "amount": 0.0, "bb": NEVER,
                                               "unit": units.get(_int(products.get(pid, {}).get("qu_id_stock")), "")})
            row["amount"] += float(e["amount"])
            row["bb"] = min(row["bb"], str(e.get("best_before_date") or NEVER)[:10])
            totals[pid] = totals.get(pid, 0.0) + float(e["amount"])
        groups: dict[str, list[dict]] = {}
        for row in sorted(rows.values(), key=lambda r: (r["bb"], r["name"].lower())):
            if row["amount"] > 0:
                groups.setdefault(locs.get(row["loc"], "Other"), []).append(row)
        order = {n: k for k, n in enumerate(LOCATIONS)}
        groups = dict(sorted(groups.items(), key=lambda kv: order.get(kv[0], len(order))))
        return page(request, "pantry.html", soon=soon, groups=groups, totals=totals, locations=i.location,
                    never=NEVER)

    @app.post("/pantry/{pid}/consume")
    def pantry_consume(pid: int, amount: float = Form(gt=0)):
        grocy.consume(pid, amount)
        return back("/pantry")

    @app.post("/pantry/{pid}/amount")
    def pantry_amount(pid: int, amount: float = Form(ge=0)):
        grocy.set_amount(pid, amount)
        return back("/pantry")

    @app.post("/pantry/{pid}/move")
    def pantry_move(pid: int, from_location: int = Form(), to_location: int = Form(), amount: float = Form(gt=0)):
        if from_location != to_location:
            grocy.transfer(pid, amount, from_location, to_location)
        return back("/pantry")

    # ---- scanning -----------------------------------------------------------------------------------------------
    @app.get("/scan")
    def scan(request: Request):
        return page(request, "scan.html", generics=sorted(p["name"] for p in _generics(grocy)))

    def apply_scan(r: Resolved, action: str, amount: float | None = None) -> dict:
        """Add or use `amount`, else one package; with no known package size, ask for the amount first."""
        p = grocy.get_object("products", r.product_id) or {}
        info = {"product": r.name, "generic": r.generic_name, "created": r.created, "product_id": r.product_id,
                "action": action}
        pkg = amount or _num(grocy.userfields("products", r.product_id).get("package_amount"))
        if pkg is None:
            return {"status": "need_amount", **info, "unit": unit_names().get(_int(p.get("qu_id_stock")), "")}
        if action == "add":
            days = int(float(p.get("default_best_before_days") or 0))  # -1 = never expires
            grocy.add_stock(r.product_id, pkg, date.today() + timedelta(days=days) if days > 0 else None,
                            _int(p.get("location_id")))
            done = pkg
        else:
            done = min(pkg, grocy.stock_amounts().get(r.product_id, 0.0))
            if done > 0:
                grocy.consume(r.product_id, done)
        return {"status": "ok", **info, "amount": done}

    class ScanIn(BaseModel):
        code: str
        action: Literal["add", "use"] = "add"
        amount: float | None = Field(default=None, gt=0, allow_inf_nan=False)  # typed when the size is unknown

    @app.post("/api/scan")
    def api_scan(body: ScanIn):
        try:
            r = resolve(grocy, ai, http, body.code.strip())
        except AIError as e:
            return {"status": "error", "message": str(e)}
        return apply_scan(r, body.action, body.amount) if r else {"status": "unknown"}

    @app.post("/api/scan/photo")
    def api_scan_photo(code: str = Form(), image: UploadFile = File(), action: Literal["add", "use"] = Form("add")):
        try:
            r = resolve_from_photo(grocy, ai, http, code.strip(), image.file.read())
        except AIError as e:
            return {"status": "error", "message": str(e)}
        return apply_scan(r, action)

    class RelinkIn(BaseModel):
        product_id: int
        generic_name: str

    @app.post("/api/relink")
    def api_relink(body: RelinkIn):
        try:
            if not body.generic_name.strip():
                raise ValueError("type a generic name")
            relink(grocy, body.product_id, body.generic_name.strip())
        except ValueError as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
        return {"status": "ok"}

    # ---- recipes ------------------------------------------------------------------------------------------------
    @app.get("/recipes")
    def recipes(request: Request):
        rs = sorted(user_recipes(grocy), key=lambda r: r["name"].lower())
        waiting = sum(p.status != "processing" for p in store.all())
        return page(request, "recipes.html", recipes=rs, waiting=waiting)

    @app.get("/recipes/{rid}")
    def recipe(request: Request, rid: int, servings: int | None = None):
        r = recipe_or_404(rid)
        base = float(r.get("base_servings") or 2)
        servings = max(1, servings or round(base))
        scale = servings / base
        uf = grocy.userfields("recipes", rid)
        positions = grocy.objects("recipes_pos", recipe_id=rid)
        products = {int(p["id"]): p for p in grocy.objects("products")}
        info = {pid: {**grocy.userfields("products", pid), "name": products[pid]["name"]}
                for pid in {int(p["product_id"]) for p in positions} if pid in products}
        cfe = _cfe(load_table(uf), scale)
        fallback = [] if cfe else [ingredient_text(i, scale) for i in position_ingredients(positions, products)]
        return page(request, "recipe.html", r=r, servings=servings, cfe=cfe, fallback=fallback,
                    macros=recipe_macros(positions, info, base) if positions else None,
                    source_url=uf.get("source_url"), sections=SECTIONS, today=date.today().isoformat())

    @app.post("/recipes/{rid}/plan")
    def recipe_plan(rid: int, day: date = Form(), section: Literal["Lunch", "Dinner"] = Form("Dinner"),
                    servings: str = Form("")):
        recipe_or_404(rid)
        plan_meal(grocy, day, section, rid, _servings(servings), settings)
        return back("/plan")

    def table_for(rid: int) -> CfeTable:
        """The saved table, or a starting point built from the recipe's positions (no ops yet)."""
        t = load_table(grocy.userfields("recipes", rid))
        if t is None:
            products = {int(p["id"]): p for p in grocy.objects("products")}
            t = CfeTable(pre=[], ingredients=position_ingredients(grocy.objects("recipes_pos", recipe_id=rid),
                                                                  products), ops=[])
        return t

    @app.get("/recipes/{rid}/table")
    def table_edit(request: Request, rid: int):
        return page(request, "table.html", r=recipe_or_404(rid), table=table_for(rid), errors=[])

    @app.post("/recipes/{rid}/table")
    async def table_save(request: Request, rid: int):
        form = await request.form()
        return await run_in_threadpool(save_table, request, rid, form)

    def save_table(request: Request, rid: int, form):
        r = recipe_or_404(rid)
        table = _table_from_form(form, table_for(rid))
        errors = [] if form.get("action") == "addop" else validate(table)
        if form.get("action") == "addop" or errors:
            return page(request, "table.html", r=r, table=table, errors=errors)
        grocy.set_userfields("recipes", rid, {"cfe_table": table.model_dump_json()})
        return back(f"/recipes/{rid}")

    # ---- import -------------------------------------------------------------------------------------------------
    @app.get("/import")
    def import_list(request: Request):
        pend = store.all()
        return page(request, "import.html", pending=pend, busy=any(p.status == "processing" for p in pend))

    @app.post("/import")
    def import_post(bg: BackgroundTasks, files: list[UploadFile] = File(default=[]), urls: str = Form("")):
        for f in files:
            data = f.file.read()
            if data:
                p = store.new(f.filename or "upload.pdf", pdf=data)
                bg.add_task(process, store, ai, http, p.id, None, data)
        for url in filter(None, (u.strip() for u in urls.splitlines())):
            p = store.new(url)
            bg.add_task(process, store, ai, http, p.id, url, None)
        return back("/import")

    def pending_or_404(pid: str):
        p = store.get(pid)
        if p is None:
            raise HTTPException(404, "No such import")
        return p

    def review_page(request: Request, p, table: CfeTable, rows: list[dict], errors: list[str]):
        gens = sorted(_generics(grocy), key=lambda g: g["name"].lower())
        by_id, units = {int(g["id"]): g for g in gens}, unit_names()
        for ing, row in zip(table.ingredients, rows):
            prod = by_id.get(row["product_id"])
            row["text"] = ingredient_text(ing, 1) + (f", {ing.note}" if ing.note else "")
            row["unit"] = ing.unit or "piece"
            row["stock_unit"] = units.get(_int(prod.get("qu_id_stock")), "") if prod else ""
            row["packaging"] = is_packaging(row["unit"])
            row["convert"] = prod is not None and row["amount"] is None and ing.qty is not None and not row["packaging"]
        return page(request, "review.html", p=p, rows=rows, generics=gens, table=table, cfe=_cfe(table),
                    table_errors=validate(table), errors=errors)

    @app.get("/import/{pid}")
    def review(request: Request, pid: str):
        p = pending_or_404(pid)
        if p.status != "ready":
            return page(request, "review.html", p=p)
        rows = [{"product_id": m.product_id, "amount": m.stock_amount, "prev": m.product_id, "factor": "",
                 "new_name": ing.name, "error": None, "note": None}
                for m, ing in zip(propose_matches(grocy, ai, p.draft), p.draft.table.ingredients)]
        return review_page(request, p, p.draft.table, rows, [])

    @app.post("/import/{pid}")
    async def review_post(request: Request, pid: str):
        """One form for the whole review; `action` is save, create:<i>, suggest:<i> or addop (re-render)."""
        form = await request.form()
        return await run_in_threadpool(review_submit, request, pid, form)  # Grocy/Claude calls block for seconds

    def review_submit(request: Request, pid: str, form):
        p = pending_or_404(pid)
        if p.status != "ready":
            return back(f"/import/{pid}")
        action = form.get("action") or "save"
        table = _table_from_form(form, p.draft.table)
        rows: list[dict] = []
        for i, ing in enumerate(table.ingredients):
            f = lambda k: str(form.get(f"{k}_{i}") or "").strip()  # noqa: E731
            row = {"product_id": _int(f("product")), "amount": _num(f("amount")), "factor": f("factor"),
                   "new_name": f("new_name") or ing.name, "error": None, "note": None}
            try:
                if action == f"create:{i}":
                    row["product_id"] = create_generic(grocy, ai, http, settings, row["new_name"])
                pid_ = row["product_id"]
                if pid_ and pid_ != _int(f("prev")):  # product changed: amount must be in its stock unit
                    row["amount"] = stock_amount_for(grocy, pid_, ing.qty, ing.unit)
                    row["factor"] = ""  # a factor typed for the old product must never be stored for this one
                if pid_ and row["amount"] is None and ing.qty is not None and not is_packaging(ing.unit):
                    if action == f"suggest:{i}":
                        prod = grocy.get_object("products", pid_)
                        g = ai.guess_conversion(prod["name"], ing.unit or "piece",
                                                unit_names().get(_int(prod.get("qu_id_stock")), ""))
                        row["factor"], row["note"] = _fmt(g.factor), g.explanation
                    elif row["factor"]:
                        add_conversion(grocy, pid_, ing.unit, _factor(row["factor"]))
                        row["amount"] = stock_amount_for(grocy, pid_, ing.qty, ing.unit)
            except (ValueError, AIError) as e:
                row["error"] = str(e)
            row["prev"] = row["product_id"]
            rows.append(row)
        errors: list[str] = []
        if action == "save":
            units, ings = unit_names(), list(table.ingredients)
            for i, (ing, row) in enumerate(zip(ings, rows)):  # store packets as the real amount the reviewer typed
                if is_packaging(ing.unit) and row["product_id"] and row["amount"] is not None:
                    prod = grocy.get_object("products", row["product_id"]) or {}
                    ings[i] = ing.model_copy(update={"qty": row["amount"],
                                                     "unit": units.get(_int(prod.get("qu_id_stock")), ing.unit)})
            table = table.model_copy(update={"ingredients": ings})
            draft = p.draft.model_copy(update={"table": table})
            source_url = p.source if p.source.startswith(("http://", "https://")) else None
            with save_lock:  # a double-tapped Save must not create the recipe twice
                if store.get(pid) is None:
                    return back("/recipes")  # the other tap already saved it
                try:
                    rid = save_recipe(grocy, draft, [IngredientMatch(i, r["product_id"], r["amount"])
                                                     for i, r in enumerate(rows)], source_url, store.pdf_path(pid))
                except ValueError as e:
                    errors.append(str(e))
                else:
                    store.delete(pid)
                    return back(f"/recipes/{rid}")
        return review_page(request, p, table, rows, errors)

    @app.post("/import/{pid}/discard")
    def discard(pid: str):
        store.delete(pid)
        return back("/import")

    # ---- week plan ----------------------------------------------------------------------------------------------
    def plan_page(request: Request, monday: date, short: list[str] = (), picks: list[dict] | None = None,
                  error: str | None = None):
        recipes_all = grocy.objects("recipes")
        rnames = {int(r["id"]): r["name"] for r in recipes_all}
        choices = sorted(((r["id"], r["name"]) for r in user_recipes(grocy)), key=lambda t: t[1].lower())

        def entry(r: dict) -> dict:
            is_recipe = r.get("type") == "recipe"
            return {"id": int(r["id"]), "recipe": is_recipe, "done": bool(int(float(r.get("done") or 0))),
                    "text": rnames.get(_int(r.get("recipe_id")), "(deleted recipe)") if is_recipe
                    else r.get("note", ""),
                    "servings": _fmt(r.get("recipe_servings"))}

        days = [{"day": d.day, "lunch": [entry(r) for r in d.lunch], "dinner": [entry(r) for r in d.dinner]}
                for d in week_view(grocy, monday)]
        return page(request, "plan.html", days=days, monday=monday, prev=monday - timedelta(days=7),
                    next=monday + timedelta(days=7), recipes=choices, short=list(short), picks=picks, error=error)

    @app.get("/plan")
    def plan_get(request: Request, week: date | None = None, short: list[str] = Query(default=[])):
        monday = _monday(week or date.today())
        taken = {(str(r["day"])[:10], int(r["section_id"])) for r in grocy.objects("meal_plan")
                 if r.get("type") == "recipe"}
        sec = ids(grocy).section
        left = [p for p in last_picks.get(monday, []) if (p["day"], sec[p["section"]]) not in taken]
        return plan_page(request, monday, short, picks=left or None)

    @app.post("/plan/suggest/clear")
    def plan_suggest_clear(week: date = Form()):
        last_picks.pop(_monday(week), None)
        return back(f"/plan?week={_monday(week).isoformat()}")

    @app.post("/plan")
    def plan_post(day: date = Form(), section: Literal["Lunch", "Dinner"] = Form("Dinner"), recipe_id: int = Form(),
                  servings: str = Form("")):
        recipe_or_404(recipe_id)
        with plan_lock:
            plan_meal(grocy, day, section, recipe_id, _servings(servings), settings)
        return back(f"/plan?week={_monday(day).isoformat()}")

    def entry_week(entry_id: int) -> str:
        e = grocy.get_object("meal_plan", entry_id)
        if e is None:
            raise HTTPException(404, "No such plan entry")
        return _monday(date.fromisoformat(str(e["day"])[:10])).isoformat()

    @app.post("/plan/{entry_id}/remove")
    def plan_remove(entry_id: int):
        week = entry_week(entry_id)
        with plan_lock:
            unplan_meal(grocy, entry_id, settings)
        return back(f"/plan?week={week}")

    @app.post("/plan/{entry_id}/cook")
    def plan_cook(entry_id: int):
        week = entry_week(entry_id)
        with plan_lock:
            short = cook(grocy, entry_id)
        return back("/plan?" + urlencode([("week", week)] + [("short", n) for n in short]))

    @app.post("/plan/suggest")
    def plan_suggest(request: Request, week: date = Form()):  # plain def: Claude takes seconds, FastAPI threadpools it
        monday = _monday(week)
        try:
            picks = suggest(grocy, ai, monday)
        except AIError as e:
            return plan_page(request, monday, error=str(e))
        names = {int(r["id"]): r["name"] for r in grocy.objects("recipes")}
        shown = [{"day": p.day.isoformat(), "label": p.day.strftime("%a %b %d"), "section": p.section,
                  "recipe_id": p.recipe_id, "name": names.get(p.recipe_id, ""), "reason": p.reason} for p in picks]
        last_picks[monday] = shown
        return plan_page(request, monday, picks=shown)

    # ---- shopping -----------------------------------------------------------------------------------------------
    @app.get("/shopping")
    def shopping(request: Request, bad: int = 0):
        i = ids(grocy)
        with plan_lock:
            rebuild_meal_plan_list(grocy, date.today(), settings.shopping_weekday)
        products = {int(p["id"]): p for p in grocy.objects("products")}
        units = unit_names()

        def view(row: dict) -> dict:
            pid = _int(row.get("product_id"))
            p = products.get(pid)
            amount = float(row.get("amount") or 0)
            pkg = _num(grocy.userfields("products", pid).get("package_amount")) if p else None
            return {"id": int(row["id"]), "pid": pid if p else None, "name": p["name"] if p else row.get("note") or "(item)",
                    "amount": amount, "unit": units.get(_int(p.get("qu_id_stock")), "") if p else "",
                    "done": bool(int(float(row.get("done") or 0))),
                    "packs": packages_needed(amount, pkg) if pkg else None, "pkg": pkg}

        lists = {n: [view(r) for r in grocy.objects("shopping_list", shopping_list_id=i.list[n])]
                 for n in ("Meal plan", "Extras")}
        todo: dict[int, float] = {}  # ticked = already got it, so it stays out of the Walmart hand-off
        for r in lists["Meal plan"]:
            if r["pid"] and not r["done"]:
                todo[r["pid"]] = todo.get(r["pid"], 0.0) + r["amount"]
        cart, rest = walmart_items(grocy, todo)
        return page(request, "shopping.html", lists=lists, bad=bad,
                    cart_link=cart_url(cart, settings.walmart_store_id) if cart else None,
                    rest=[{"id": p["id"], "name": p["name"], "url": search_url(p["name"])} for p in rest])

    @app.post("/shopping/{row_id}/toggle")
    def shopping_toggle(row_id: int):
        row = grocy.get_object("shopping_list", row_id)
        if row is None:
            raise HTTPException(404, "No such row")
        grocy.update("shopping_list", row_id, {"done": 0 if int(float(row.get("done") or 0)) else 1})
        return back("/shopping")

    @app.post("/shopping/extra")
    def shopping_extra(name: str = Form()):
        if name.strip():
            grocy.create("shopping_list", {"shopping_list_id": ids(grocy).list["Extras"], "note": name.strip(),
                                           "amount": 1, "done": 0})
        return back("/shopping")

    @app.post("/shopping/bought")
    def shopping_bought():
        with plan_lock:  # rows are read inside the lock, so a second POST finds them already gone
            i = ids(grocy)
            for name in ("Meal plan", "Extras"):
                for row in grocy.objects("shopping_list", shopping_list_id=i.list[name]):
                    if not int(float(row.get("done") or 0)):
                        continue
                    pid = _int(row.get("product_id"))
                    p = grocy.get_object("products", pid) if pid else None
                    if p:  # free-text extras (no product) are just crossed off
                        pkg = _num(grocy.userfields("products", pid).get("package_amount"))
                        amount = float(row.get("amount") or 0)
                        if pkg:
                            amount = packages_needed(amount, pkg) * pkg
                        days = int(float(p.get("default_best_before_days") or 0))  # -1 = never expires
                        if amount > 0:
                            grocy.add_stock(pid, amount, date.today() + timedelta(days=days) if days > 0 else None,
                                            _int(p.get("location_id")))
                    grocy.delete("shopping_list", int(row["id"]))  # after add_stock: a failure never loses a purchase
            rebuild_meal_plan_list(grocy, date.today(), settings.shopping_weekday)
            return back("/shopping")

    @app.post("/products/{pid}/walmart")
    def product_walmart(pid: int, link: str = Form()):
        if grocy.get_object("products", pid) is None:
            raise HTTPException(404, "No such product")
        item = parse_item_id(link)
        if item is None:
            return back("/shopping?bad=1")
        grocy.set_userfields("products", pid, {"walmart_item_id": item})
        return back("/shopping")

    @app.get("/api/expiring")
    def api_expiring():
        names = {int(p["id"]): p["name"] for p in grocy.objects("products")}
        vol = grocy.volatile(settings.expiry_warn_days)
        def label(r: dict, with_day: bool) -> str:
            name = names.get(int(r["product_id"]), f"product {r['product_id']}")
            try:
                return name + (f" ({date.fromisoformat(str(r['best_before_date'])[:10]):%a})" if with_day else "")
            except (KeyError, ValueError):
                return name

        expired = {int(r["product_id"]): label(r, False) for r in vol.get("expired_products") or []}
        soon = {int(r["product_id"]): label(r, True)
                for key in ("overdue_products", "due_products") for r in vol.get(key) or []
                if int(r["product_id"]) not in expired}
        msg = [f"{k}: {', '.join(v)}" for k, v in (("Expired", expired.values()), ("Use soon", soon.values())) if v]
        return {"count": len(expired) + len(soon), "message": "; ".join(msg)}

    return app


def main_app() -> FastAPI:
    """uvicorn app.web:main_app --factory"""
    s = load_settings()
    return create_app(Grocy(s.grocy_url, s.grocy_api_key), AI(anthropic.Anthropic()), httpx.Client(timeout=20), s,
                      PendingStore(s.data_dir / "pending"))
