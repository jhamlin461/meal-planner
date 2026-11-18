"""Web UI: pantry, scanning, recipes and recipe import (server-rendered; Grocy holds all data)."""
import logging
import math
import mimetypes
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import anthropic
import httpx
import jinja2
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError

from app.ai import AI, AIError
from app.barcode import Resolved, _generics, relink, resolve, resolve_from_photo
from app.cfe import CfeTable, Ingredient, Op, ingredient_text, layout, ncols, validate
from app.config import Settings, load_settings
from app.grocy import Grocy, GrocyError
from app.importer import (IngredientMatch, PendingStore, add_conversion, create_generic, process, propose_matches,
                          save_recipe, stock_amount_for)
from app.mealplan import plan_meal
from app.nutrition import recipe_macros
from app.setup_grocy import LOCATIONS, SECTIONS, ids

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
    """Number for an input's value= (no trailing .0)."""
    return "" if x is None else f"{float(x):g}"


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


def create_app(grocy, ai, http: httpx.Client, settings: Settings, store: PendingStore) -> FastAPI:
    app = FastAPI(title="Meal planner")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters["num"] = _fmt
    env.globals["next_op_id"] = _next_op_id
    templates = Jinja2Templates(env=env)

    def page(request: Request, name: str, status_code: int = 200, **ctx):
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    @app.exception_handler(GrocyError)
    @app.exception_handler(httpx.ConnectError)
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
    def home():
        return back("/plan")

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

    def apply_scan(r: Resolved, action: str) -> dict:
        """Add or use one package of a resolved product."""
        pkg = _num(grocy.userfields("products", r.product_id).get("package_amount")) or 1.0
        if action == "add":
            p = grocy.get_object("products", r.product_id) or {}
            days = int(float(p.get("default_best_before_days") or 0))  # -1 = never expires
            grocy.add_stock(r.product_id, pkg, date.today() + timedelta(days=days) if days > 0 else None,
                            _int(p.get("location_id")))
            amount = pkg
        else:
            amount = min(pkg, grocy.stock_amounts().get(r.product_id, 0.0))
            if amount > 0:
                grocy.consume(r.product_id, amount)
        return {"status": "ok", "product": r.name, "generic": r.generic_name, "created": r.created,
                "product_id": r.product_id, "action": action, "amount": amount}

    class ScanIn(BaseModel):
        code: str
        action: Literal["add", "use"] = "add"

    @app.post("/api/scan")
    def api_scan(body: ScanIn):
        try:
            r = resolve(grocy, ai, http, body.code.strip())
        except AIError as e:
            return {"status": "error", "message": str(e)}
        return apply_scan(r, body.action) if r else {"status": "unknown"}

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
        rs = sorted((r for r in grocy.objects("recipes") if (r.get("type") or "normal") == "normal"),
                    key=lambda r: r["name"].lower())  # Grocy keeps internal meal-plan recipes in the same table
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
        plan_meal(grocy, day, section, rid, _int(servings), settings)
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
        r = recipe_or_404(rid)
        form = await request.form()
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
            row["convert"] = prod is not None and row["amount"] is None and ing.qty is not None
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
        p = pending_or_404(pid)
        if p.status != "ready":
            return back(f"/import/{pid}")
        form = await request.form()
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
                if pid_ and row["amount"] is None and ing.qty is not None:
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
            draft = p.draft.model_copy(update={"table": table})
            source_url = p.source if p.source.startswith(("http://", "https://")) else None
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

    return app


def main_app() -> FastAPI:
    """uvicorn app.web:main_app --factory"""
    s = load_settings()
    return create_app(Grocy(s.grocy_url, s.grocy_api_key), AI(anthropic.Anthropic()), httpx.Client(timeout=20), s,
                      PendingStore(s.data_dir / "pending"))
