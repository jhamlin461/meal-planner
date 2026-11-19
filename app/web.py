"""Web UI: inventory, scanning, recipes and recipe import (server-rendered; Grocy holds all data)."""
import hashlib
import itertools
import json
import logging
import math
import mimetypes
import re
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urlsplit
from pathlib import Path
from typing import Literal

import anthropic
import httpx
import jinja2
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

from app.ai import AI, AIError, AIPaused, budget_mode, month
from app.barcode import NeedsDetails, Resolved, _generics, create_manual, relink, resolve, resolve_from_photo
from app.cfe import CfeTable, Ingredient, Op, ingredient_text, layout, line_text, ncols, packets_of, validate
from app.config import Settings, load_settings
from app.grocy import Grocy, GrocyError
from app.ha import HA, HAError
from app import ha_live
from app import measure as msr
from app import home as hm
from app.house import LOCK, HouseStore, Person, by_grocy_user
from app import reminders as rm
from app.reminders import start_loop
from app.importer import (BATCH_MIN, FILL_RETRY, IngredientMatch, PendingStore, add_conversion, create_generic,
                          estimate_amount, estimate_key, make_table, needs_estimate, page_text, process,
                          propose_matches, put_photo, save_recipe, start_worker, stock_amount_for, submit_batch,
                          to_jpeg)
from app.mealplan import cook, plan_meal, unplan_meal, week_view
from app.qty import is_packaging, parse_qty
from app.nutrition import recipe_macros
from app.inventory import (add_place, clear_stock, default_place, kits, move_family, places, remove_place,
                           remove_product, rename_place, reserved, rows, stock_lines, use, why_not_remove)
from app.setup_grocy import SECTIONS, bootstrap, ids, supplies_group
from app.shopping import rebuild_lists, walmart_items
from app.suggest import suggest, user_recipes
from app.today import expiring, summary, today_view
from app.walmart import cart_url, packages_needed, parse_item_id, refine, search_url

HERE = Path(__file__).parent
UPLOAD_MAX = 10 * 1024 * 1024  # a recipe photo from the phone
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


def _table_from_form(form, old: CfeTable) -> CfeTable:
    """Rebuild the ops from the editor: per step row an op_id (hidden), an op_label, an op_where and the ticked chips
    op_in_<row> (ingredient ids or earlier steps' op_ids). Blank rows are dropped, action remove:<row> drops that step and the
    ticks pointing at it, and steps are renumbered o1, o2, ... in row order. A tick that names no ingredient and no
    earlier step (unknown, later, itself) is kept as-is so validate() rejects it. `packets` (one per ingredient row,
    blank or not a number above 0 for none) sets each ingredient's packets; a form without them keeps the old ones.
    A doubted row's amount fields (qty_<n>, unit_<n>, packets_<n>) set that row's amount; each is taken only when it
    differs from the value it was shown with, so an untouched field never rounds an amount."""
    wheres = form.getlist("op_where")
    rows = [(i.strip(), lab.strip(), [r.strip() for r in form.getlist(f"op_in_{k}") if r.strip()],
             wheres[k].strip() if k < len(wheres) else "")
            for k, (i, lab) in enumerate(zip(form.getlist("op_id"), form.getlist("op_label")))]
    act = str(form.get("action") or "")
    k = _int(act.removeprefix("remove:")) if act.startswith("remove:") else None
    gone = {rows.pop(k)[0]} if k is not None and 0 <= k < len(rows) else set()
    gone |= {r[0] for r in rows if not (r[1] or r[2])}  # a blanked step is removed too
    rows = [r for r in rows if r[1] or r[2]]
    ings = {f"i{n}" for n in range(len(old.ingredients))}
    earlier: dict[str, int] = {}  # old step id -> its row
    picked = []
    for n, (oid, _, refs, _) in enumerate(rows):
        picked.append([r if r in ings else earlier.get(r, r) for r in refs if r in ings or r not in gone])
        if oid:
            earlier[oid] = n
    unknown = {r for refs in picked for r in refs if isinstance(r, str)}
    fresh = (f"o{n}" for n in itertools.count(1) if f"o{n}" not in unknown)
    new = [next(fresh) for _ in rows]
    packets = form.getlist("packets")
    if len(packets) == len(old.ingredients):
        old = old.model_copy(update={"ingredients": [g.model_copy(update={"packets": _num(n)})
                                                     for g, n in zip(old.ingredients, packets)]})
    ings = []
    for n, g in enumerate(old.ingredients):
        q, u, k = (form.get(f"{f}_{n}") for f in ("qty", "unit", "packets"))
        up = {}
        if q is not None and q.strip() != _shown(g.qty):
            x = parse_qty(q)
            if not q.strip() or (x is not None and math.isfinite(x) and x > 0):  # blank: to taste; junk: unchanged
                up["qty"] = x
        if u is not None and not is_packaging(u):  # refused on the row (_packet_units)
            up["unit"] = u.strip()
        if k is not None and k.strip() != _shown(g.packets):
            up["packets"] = _num(k)
        ings.append(g.model_copy(update=up))
    old = old.model_copy(update={"ingredients": ings})
    return old.model_copy(update={"ops": [Op(id=new[n], label=lab, where=where,
                                             inputs=[new[r] if isinstance(r, int) else r for r in refs])
                                          for n, ((_, lab, _, where), refs) in enumerate(zip(rows, picked))]})


PACKET_UNIT = "Use a measure like tbsp or cup; packets go in the packets box."


def _packet_units(form, n: int) -> list[int]:
    """Rows whose amount field names a packaging unit: refused (a packet is no measure; the packets box counts them)."""
    return [i for i in range(n) if is_packaging(str(form.get(f"unit_{i}") or ""))]


def _shown(x: float | None) -> str:
    """How an amount field shows a number (the template's '%g')."""
    return "" if x is None else f"{x:g}"


def _recipe_doubts(uf: dict) -> list[dict]:
    """The check's doubts kept on a saved recipe (its `doubts` userfield) until the table editor saves."""
    try:
        got = json.loads(uf.get("doubts") or "[]")
    except ValueError:
        return []
    ok = isinstance(got, list) and all(isinstance(d, dict) and isinstance(d.get("ref"), str)
                                       and isinstance(d.get("reason"), str) for d in got)
    return got if ok else []  # anything else (edited in Grocy): no doubts


def _standing(doubts: list[dict], old: CfeTable, new: CfeTable) -> list[dict]:
    """The check's doubts still standing: one about a row or step the reviewer has changed has been looked at."""
    def parts(t: CfeTable) -> dict:
        return {f"i{n}": g for n, g in enumerate(t.ingredients)} | {o.id: o for o in t.ops}
    a, b = parts(old), parts(new)
    return [d for d in doubts if a.get(d["ref"]) == b.get(d["ref"])]


def _doubt_ctx(doubts: list[dict], stored: list[dict] | None = None) -> dict:
    """Template context for the doubts: the numbered list, each doubtful cell's numbers (ref -> [n, ...]), and the
    ingredient rows that keep their amount fields: every row doubted in `stored` (the doubts as saved, default
    `doubts`), so a correction, whose doubt has cleared, is still posted back on the next Update or Save."""
    marks: dict[str, list[int]] = {}
    for n, d in enumerate(doubts, 1):
        if d["ref"]:
            marks.setdefault(d["ref"], []).append(n)
    fixable = {d["ref"] for d in (doubts if stored is None else stored) if re.fullmatch(r"i\d+", d["ref"])}
    return {"doubts": doubts, "marks": marks, "fixable": fixable}


def _chip_names(table: CfeTable) -> dict[str, str]:
    """What the UI calls each ingredient and step id: ingredient names (with the amount when a name repeats) and
    step labels."""
    names = [g.name for g in table.ingredients]
    out = {}
    for n, g in enumerate(table.ingredients):
        amount = ingredient_text(g.model_copy(update={"name": ""}), 1) or g.note
        out[f"i{n}"] = f"{g.name}, {amount}" if names.count(g.name) > 1 and amount else g.name
    labels = [o.label for o in table.ops]
    return out | {o.id: f"{o.label}, step {n}" if labels.count(o.label) > 1 and o.label else o.label or f"step {n}"
                  for n, o in enumerate(table.ops, 1)}


def _chip_users(table: CfeTable) -> dict[str, set[int]]:
    """For each ingredient or step id, the step rows that tick it (the editor hides it from the others)."""
    users: dict[str, set[int]] = {}
    for n, o in enumerate(table.ops):
        for r in o.inputs:
            users.setdefault(r, set()).add(n)
    return users


_PLAIN = [  # cfe.validate() messages, in the editor's words; {0} is the step or ingredient as its chip names it
    (r"op id (\S+) is duplicated", "{0} appears twice in the steps; press Update to number them again."),
    (r"(\S+) has no inputs", "{0} doesn't combine anything yet; tick what goes into it."),
    (r"(\S+) input (\S+) does not exist", "{0} combines something that isn't an ingredient or an earlier step."),
    (r"(\S+) is not used by any op", "{0} isn't in any step yet; tick it in the step it goes into."),
    (r"(\S+) is used by (\d+) ops", "{0} is ticked in {1} steps; each ingredient goes into just one."),
    (r"(\S+) is an input of (\d+) ops", "{0} feeds into {1} steps; each step goes into just one."),
    (r"(\S+) covers non-contiguous rows .*", "{0} combines ingredients that aren't next to each other in the list. "
                                              "Tick neighbouring ones, or split it into more steps."),
]


def _named(errors: list[str], table: CfeTable) -> list[str]:
    """Table validation messages in plain words, naming ingredients and steps as their chips do (never by id)."""
    names = _chip_names(table)
    q = lambda ref: f"“{names.get(ref, ref)}”"  # noqa: E731
    out = []
    for e in (part for err in errors for part in err.split("; ")):  # save_recipe joins its messages
        if roots := re.fullmatch(r"expected exactly one root op, found \d+: \[(.*)\]", e):
            ids = re.findall(r"'([^']*)'", roots[1])
            out += ([f"Every step except the last must feed into a later step; {q(r)} doesn't." for r in ids[:-1]]
                    or ["Add at least one step." if not table.ops else "Each step feeds into another, so none is last."])
            continue
        for pat, msg in _PLAIN:
            if m := re.fullmatch(pat, e):
                out.append(msg.format(q(m[1]), *m.groups()[1:]))
                break
        else:
            out.append(re.sub(r"[\w-]+", lambda m: q(m[0]) if m[0] in names else m[0], e))
    return out


def _resume_day() -> str:
    """When free mode ends: the 1st of next month, 'December 1'."""
    first = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    return f"{first:%B} 1"


def _cfe(table: CfeTable | None, scale: float = 1.0, packets: bool = True) -> dict | None:
    """Context for _cfe_table.html, or None when the table is missing or invalid (caller shows the fallback).
    `packets`: whether ingredients show their packet count (cfe.layout)."""
    if table is None or validate(table):
        return None
    try:
        return {"pre": table.pre, "grid": layout(table, scale, packets), "ncols": ncols(table)}
    except ValueError:
        return None


def create_app(grocy, ai, http: httpx.Client, settings: Settings, store: PendingStore, *,
               house: HouseStore | None = None, ha=None) -> FastAPI:
    app = FastAPI(title="Meal planner")
    house = house or HouseStore(settings.data_dir / "house.db")
    if ha is None and settings.ha_url:
        ha = HA(settings.ha_url, settings.ha_token)
    if settings.ha_url:
        start_loop(lambda: (grocy, house, ha, settings), also=ha_live.publish)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters["num"] = _fmt
    env.filters["exact"] = _exact
    env.globals["chip_names"] = _chip_names
    env.globals["chip_users"] = _chip_users
    env.filters["show"] = msr.show  # an amount in stock units, shown in a product's measure: "2 1/2 cups"
    env.filters["in_measure"] = msr.value  # the same for a number field: "2.5"
    env.globals["label"] = msr.label
    env.globals["measure_choices"] = msr.choices
    templates = Jinja2Templates(env=env)
    plan_lock = threading.Lock()  # plan add/remove, Bought, Cooked it, list rebuild: never interleaved or repeated
    last_picks: dict[date, list[dict]] = {}  # latest suggestions per week, until accepted or dismissed
    save_lock = threading.Lock()  # ponytail: one lock for all import saves; they are rare and per-household
    measure_lock = threading.Lock()  # one measure fill at a time, so no product is asked about twice
    measure_tried: dict[int, float] = {}  # product id -> when Claude last gave no usable measure for it

    @app.middleware("http")
    async def no_cross_site_writes(request: Request, call_next):
        """No login, so refuse writes a browser says came from another site (a missing header is allowed: curl, HA)."""
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"status": "error", "message": "cross-site request refused"}, status_code=403)
        response = await call_next(request)
        if request.method == "POST":
            rm.WAKE.set()  # Done, Snooze, Bought, an edit: the reminder loop pushes HA's sensors now
        return response

    def ai_mode() -> str:
        """This month's budget mode, from the meter (as app.ai.AI decides it)."""
        return budget_mode(house.ai_spent(month()), settings.ai_monthly_budget, settings.ai_economy_at)

    def page(request: Request, name: str, status_code: int = 200, **ctx):
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    @app.exception_handler(GrocyError)
    @app.exception_handler(httpx.TransportError)  # refused, timed out, dropped mid-response
    async def grocy_down(request: Request, exc: Exception):
        status = getattr(exc, "status", None)
        if status is not None and 400 <= status < 500:  # Grocy answered and said no: nothing was saved
            log.warning("Grocy refused: %s", exc)
            return page(request, "down.html", 502, refused=getattr(exc, "reason", "") or str(exc))
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
        out = []
        for p in positions:  # one waiting for an estimate shows the recipe's own amount; 0 alone is to taste
            qty, unit = float(p["amount"]) or None, units.get(_int(p.get("qu_id")), "")
            q, _, u = str(p.get("variable_amount") or "").partition(" ")
            if qty is None and parse_qty(q) is not None:  # a marker edited in Grocy may not parse: shown as to taste
                qty, unit = parse_qty(q), u
            out.append(Ingredient(qty=qty, unit=unit, name=products.get(int(p["product_id"]), {}).get(
                "name", f"product {p['product_id']}")))
        return out

    @app.get("/")
    def today(request: Request, msg: str = ""):
        who = current_who(request)
        if who is None:
            return back("/who")
        now = datetime.now()
        return page(request, "today.html", msg=msg, date=f"{now:%A, %B} {now.day}", ai_paused=ai_mode() == "free",
                    **today_view(grocy, house, who, now, ha=ha, warn_days=settings.expiry_warn_days))

    # ---- chores, tasks and upkeep (logic in app/home.py) ---------------------------------------------------------
    def home_back(msg: str) -> RedirectResponse:
        return back("/home?" + urlencode({"msg": msg}))

    @app.get("/home")
    def home(request: Request, msg: str = ""):
        people, today = house.people(), date.today()
        return page(request, "home.html", msg=msg, chores=hm.chore_rows(grocy, people, today),
                    tasks=hm.task_rows(grocy, people, today), upkeep=hm.upkeep_rows(grocy, today))

    @app.post("/home/starters")
    def starters():
        n = hm.add_starters(grocy, house, house.people())
        return home_back(f"Added {n} starter chore{'' if n == 1 else 's'}." if n else "The starter chores are already there.")

    def credit(request: Request, assigned) -> Person | None:
        """Whoever's device this is gets the credit; the kitchen screen credits the person it was assigned to."""
        who = current_who(request)
        if who == "kitchen":
            return by_grocy_user(house.people(), assigned)
        return who if isinstance(who, Person) else None

    def done(request: Request, kind: str, id: int, due: str | None, skipped: bool = False) -> bool:
        """In-app Done (or a chore's Skip) for the occurrence the page showed (`due`); a page older than the item's last
        Done or Skip is a no-op."""
        key = f"{kind}:{id}"
        with LOCK:  # one read of Grocy, and two taps can't both pass the due check
            open_ = rm.open_items(grocy)
            who = credit(request, open_.get(key, (None, None, None))[2])
            return rm.complete(grocy, house, ha, key, who, due, open_, skipped)

    def done_msg(ran: bool) -> str:
        return "Done." if ran else "Already done."

    @app.post("/home/chores/{cid}/done")
    def chore_done_post(request: Request, cid: int, due: str | None = Form(None)):
        return home_back(done_msg(done(request, "chore", cid, due)))

    @app.post("/home/chores/{cid}/skip")
    def chore_skip_post(request: Request, cid: int, due: str | None = Form(None)):
        return home_back("Skipped." if done(request, "chore", cid, due, skipped=True) else "Already done.")

    @app.post("/home/tasks/{tid}/done")
    def task_done_post(request: Request, tid: int, due: str | None = Form(None)):
        return home_back(done_msg(done(request, "task", tid, due)))

    @app.post("/home/upkeep/{bid}/done")
    def upkeep_done_post(request: Request, bid: int, due: str | None = Form(None)):
        return home_back(done_msg(done(request, "battery", bid, due)))

    @app.post("/today/{kind}/{id}/done")
    def today_done(request: Request, kind: Literal["chore", "task", "battery"], id: int, due: str | None = Form(None)):
        return back("/?" + urlencode({"msg": done_msg(done(request, kind, id, due))}))

    @app.post("/today/{kind}/{id}/snooze")
    def today_snooze(kind: Literal["chore", "task", "battery"], id: int):
        rm.snooze(house, ha, f"{kind}:{id}", datetime.now())
        return back("/")

    kinds = {  # kind -> (grocy entity, form template, what to call it)
        "chores": ("chores", "chore", "chore_form.html", "chore"),
        "tasks": ("tasks", "task", "task_form.html", "task"),
        "upkeep": ("batteries", "battery", "upkeep_form.html", "upkeep item"),
    }

    def form_page(request: Request, kind: str, v: dict, id: int | None, status_code: int = 200, error: str | None = None):
        return page(request, kinds[kind][2], status_code, v=v, id=id, error=error, people=house.people(),
                    supplies=hm.supplies(grocy) if kind == "chores" else [], days=hm.DAYS)

    def existing(kind: str, id: int) -> dict:
        row = grocy.get_object(kinds[kind][0], id)
        if row is None:
            raise HTTPException(404, f"No such {kinds[kind][3]}")
        return row

    def values_of(kind: str, id: int) -> dict:
        row, rem = existing(kind, id), house.reminder(f"{kinds[kind][1]}:{id}")
        common = {"remind_at": rem.remind_at}
        if kind == "chores":
            return hm.chore_values(row, rem, house.people())
        if kind == "tasks":
            return {"title": row["name"], "note": row.get("description") or "", "due": row.get("due_date") or "",
                    "who": rem.audience.removeprefix("person:") if rem.audience else "home", **common}
        return {"name": row["name"], "every": row.get("charge_interval_days") or "", **common}

    def form_get(request: Request, kind: str, id: int | None):
        return form_page(request, kind, values_of(kind, id) if id else {}, id)

    async def form_post(request: Request, kind: str, id: int | None):
        return await run_in_threadpool(save_form, request, kind, id, await request.form())

    def form_values(form) -> dict:
        return {k: form.get(k) for k in form} | {"days": form.getlist("days")}

    def save_form(request: Request, kind: str, id: int | None, form):
        entity, key, _, noun = kinds[kind]
        people = house.people()
        try:
            if id is not None:
                existing(kind, id)
            if kind == "chores":
                row, rem = hm.chore_fields(form, people)
            elif kind == "tasks":
                row, rem = hm.task_fields(form, people)
                cat = next((c["id"] for c in grocy.objects("task_categories") if c["name"] == "Household"), None)
                row["category_id"] = cat
            else:
                row, rem = hm.upkeep_fields(form)
        except ValueError as e:
            return form_page(request, kind, form_values(form), id, 400, str(e))
        try:
            hm.save(grocy, house, entity, key, row, rem, id)
        except GrocyError as e:
            return form_page(request, kind, form_values(form), id, 400, f"Grocy refused it: {e}")
        return home_back(f"Saved {noun}.")

    def register(kind: str):
        entity, key, _, noun = kinds[kind]

        @app.get(f"/home/{kind}/new", name=f"{kind}_new")
        def new_get(request: Request):
            return form_get(request, kind, None)

        @app.post(f"/home/{kind}/new", name=f"{kind}_create")
        async def new_post(request: Request):
            return await form_post(request, kind, None)

        @app.get(f"/home/{kind}/{{id}}", name=f"{kind}_edit")
        def edit_get(request: Request, id: int):
            return form_get(request, kind, id)

        @app.post(f"/home/{kind}/{{id}}", name=f"{kind}_save")
        async def edit_post(request: Request, id: int):
            return await form_post(request, kind, id)

        @app.post(f"/home/{kind}/{{id}}/delete", name=f"{kind}_delete")
        def delete_post(id: int):
            existing(kind, id)
            hm.remove(grocy, house, entity, key, id)
            return home_back(f"Deleted {noun}.")

    for k in kinds:
        register(k)

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
        return next((e for e, f in persons.items() if n and (n in f.lower() or n in e.removeprefix("person.").lower())), "")

    def people_page(request: Request, status_code: int = 200, typed: str = "", error: str | None = None):
        return page(request, "people.html", status_code, people=house.people(), who=current_who(request), typed=typed,
                    error=error, **ha_choices())

    @app.get("/home/people")
    def people_get(request: Request):
        return people_page(request)

    @app.post("/home/people")
    def people_post(request: Request, name: str = Form(), ha_person: str = Form(""), notify_service: str = Form("")):
        name = name.strip()
        if not name:
            raise HTTPException(400, "Type a name")
        if any(p.name.lower() == name.lower() for p in house.people()):
            return people_page(request, 400, name, f"{name} is already on the list.")
        if notify_service not in ("", "none") and not re.fullmatch(r"mobile_app_[a-z0-9_]+", notify_service):
            return people_page(request, 400, name, "Pick a phone from the list.")  # it ends up in an HA service URL
        c = ha_choices()
        ha_person = ha_person or match_person(name, c["ha_persons"])
        # blank = use the person's own phone; "none" = deliberately no phone
        notify_service = "" if notify_service == "none" else notify_service or c["phone_of"].get(ha_person, "")
        username = name.lower().replace(" ", "")
        uid = next((int(u["id"]) for u in grocy.users() if u["username"] == username), None)
        if uid is None:
            uid = grocy.create_user(username, name)
        house.save_person(name, uid, ha_person, notify_service)
        return back("/home/people")

    @app.post("/home/people/{pid}/delete")
    def people_delete(pid: int):
        house.delete_person(pid)  # the Grocy user stays: it may own history there
        return back("/home/people")

    @app.get("/me")
    def me():
        return back("/settings")

    @app.get("/settings")
    def settings_page(request: Request):
        return page(request, "settings.html", who=current_who(request), ai_spent=house.ai_spent(month()),
                    ai_budget=settings.ai_monthly_budget, ai_economy_at=settings.ai_economy_at, ai_mode=ai_mode())

    # ---- household supplies (Grocy product group; keep-at-least is the generic's min stock amount, counting the stock
    # of every brand under it) ------------------------------------------------------------------------------------
    def product_or_404(pid: int) -> dict:
        p = grocy.get_object("products", pid)
        if p is None:
            raise HTTPException(404, "No such product")
        return p

    def generic_of(p: dict) -> dict:
        parent = grocy.get_object("products", int(p["parent_product_id"])) if p.get("parent_product_id") else None
        return parent or p

    def typed(p: dict, word: str | None) -> msr.Measure:
        """The measure an amount was typed in: the one its form showed, which must still be the product's (not a page
        from before a change, or from before Claude picked one)."""
        m = msr.of(grocy, None, p)
        if word is not None and word != m.word:
            raise ValueError(f"{p['name']} is now in {msr.label(m)}. Type the amount again.")
        return m

    def rebuild() -> None:
        rebuild_lists(grocy, date.today(), settings.shopping_weekday, house)

    def set_min(pid: int, amount: float) -> None:
        gid = generic_of(product_or_404(pid))["id"]
        grocy.update("products", gid, {"min_stock_amount": amount, "cumulate_min_stock_amount_of_sub_products": 1})
        with plan_lock:
            rebuild()

    def inventory_back(place: int | None, msg: str = "", err: str = "") -> RedirectResponse:
        q = urlencode({k: v for k, v in (("place", place), ("msg", msg), ("err", err)) if v})
        return back("/inventory" + (f"?{q}" if q else ""))

    @app.get("/home/supplies")
    def supplies(msg: str = ""):
        return inventory_back(None, msg)

    @app.post("/home/supplies/{pid}/used")
    def supply_used(pid: int, place: int | None = Form(None)):
        """One of its measure (a bag, a roll, a bottle), whatever the package size; never more than is in stock (Grocy
        refuses that)."""
        p = product_or_404(pid)  # a generic: Grocy takes the stock from its brands
        with plan_lock:
            use = min(msr.of(grocy, None, p).factor, grocy.stock_amounts().get(pid, 0.0))
            if use > 0:
                grocy.consume(pid, use)
            rebuild()
        return inventory_back(place, f"Used one {p['name']}." if use > 0 else f"No {p['name']} left to use.")

    GONE = "That place no longer exists."

    @app.post("/home/supplies/{pid}/min")
    def supply_min(pid: int, amount: float = Form(ge=0, allow_inf_nan=False), place: int | None = Form(None),
                   word: str | None = Form(None, alias="measure")):
        gen = generic_of(product_or_404(pid))
        try:
            m = typed(gen, word)
        except ValueError as e:
            return inventory_back(place, err=str(e))
        set_min(pid, msr.from_field(amount, m, float(gen.get("min_stock_amount") or 0)))  # 0 = never on Running low
        return inventory_back(place)

    @app.post("/home/supplies/{pid}/move")
    def supply_move(pid: int, from_location: int = Form(), to_location: int = Form(), place: int | None = Form(None)):
        """A supply row is its generic in one place: move its own and its brands' stock from there."""
        product_or_404(pid)
        if to_location not in places(grocy).values():
            return inventory_back(place, err=GONE)
        if from_location != to_location:
            move_family(grocy, pid, from_location, to_location)
        return inventory_back(place)

    @app.post("/settings/theme")
    def set_theme(request: Request, theme: Literal["dark", "black", "light", "auto"] = Form()):
        """Per-device theme; back to the page it was switched on (same-site paths only)."""
        ref = urlsplit(request.headers.get("referer", ""))
        ok = ref.path.startswith("/") and not ref.path.startswith(("//", "/\\"))
        r = back(ref.path + (f"?{ref.query}" if ref.query else "") if ok else "/")
        r.set_cookie("theme", theme, max_age=400 * 24 * 3600, samesite="lax")
        return r

    # ---- inventory ----------------------------------------------------------------------------------------------
    @app.get("/pantry")
    def pantry(msg: str = ""):
        return inventory_back(None, msg)

    def fill_measures(pids: list[int]) -> None:
        """Claude's measure for products shown without one, after the page is sent: they show it from the next visit.
        A fill already running takes them (nothing piles up); one Claude gave no usable measure for waits a day.
        ponytail: that wait is kept in memory, so a restart asks again."""
        if not measure_lock.acquire(blocking=False):
            return
        try:
            for pid in pids:
                if ai.mode() == "free":
                    return
                if pid in measure_tried and time.monotonic() - measure_tried[pid] < FILL_RETRY:
                    continue
                try:
                    if p := grocy.get_object("products", pid):
                        msr.of(grocy, ai, p)
                        if not grocy.userfields("products", pid).get("measure"):  # failed, or not a measure
                            measure_tried[pid] = time.monotonic()
                except Exception:  # one product's failure never stops the rest
                    log.warning("could not find a measure for product %s", pid, exc_info=True)
                    measure_tried[pid] = time.monotonic()
        finally:
            measure_lock.release()

    @app.get("/inventory")
    def inventory(request: Request, background: BackgroundTasks, place: int | None = None, msg: str = "",
                  err: str = ""):
        inv, have = rows(grocy, settings, date.today(), house), places(grocy)
        bare = sorted({r[k] for rs in inv.groups.values() for r in rs
                       for k, m in (("pid", "m"), ("gid", "km")) if r[m].word == r["unit"]}
                      | {i["pid"] for k in inv.kits for i in k["items"] if i["m"].word == i["unit"]})
        if bare and ai.mode() != "free":
            background.add_task(fill_measures, bare)
        chips = {n: have[n] for n in inv.groups if n in have}  # places holding something, in places() order
        place = place if place in chips.values() else None  # an empty or unknown place shows everything
        groups = {n: r for n, r in inv.groups.items() if place is None or chips.get(n) == place}
        return page(request, "inventory.html", soon=inv.soon if place is None else [], groups=groups,
                    kits=inv.kits if place is None else [], totals=inv.totals, chips=chips, place=place,
                    locations=have, msg=msg, err=err)

    @app.post("/pantry/{pid}/consume")
    def pantry_consume(pid: int, amount: float = Form(gt=0), place: int | None = Form(None),
                       word: str | None = Form(None, alias="measure"), kit: int | None = Form(None)):
        """Used some: a place row's from the stock no meal kit holds, a kit item's (`kit`: its recipe) from the kit
        first; never more than there is ("all of it" may round past it)."""
        try:
            m = typed(product_or_404(pid), word)
        except ValueError as e:
            return inventory_back(place, err=str(e))
        with plan_lock:
            use(grocy, house, pid, msr.to_stock(amount, m), kit)
        return inventory_back(place)

    @app.post("/pantry/{pid}/amount")
    def pantry_amount(pid: int, amount: float = Form(ge=0), place: int | None = Form(None),
                      word: str | None = Form(None, alias="measure")):
        try:
            m = typed(product_or_404(pid), word)
        except ValueError as e:
            return inventory_back(place, err=str(e))
        held = reserved(grocy, house).get(pid, 0.0)  # the total shown and typed leaves meal kits out
        own = sum(float(e["amount"]) for e in grocy.stock_entries() if int(e["product_id"]) == pid) - held
        grocy.set_amount(pid, msr.from_field(amount, m, own) + held)
        return inventory_back(place)

    @app.post("/inventory/{pid}/measure")
    def inventory_measure(pid: int, word: str = Form("", alias="measure"), place: int | None = Form(None)):
        """The person's own measure for this product: every amount of it is shown and typed in it from now on."""
        try:
            p = product_or_404(pid)
            msr.change(grocy, p, word)
            if p.get("parent_product_id"):  # a brand and its generic are one item to the person
                msr.change(grocy, generic_of(p), word)
        except ValueError as e:
            return inventory_back(place, err=str(e))
        return inventory_back(place)

    @app.post("/pantry/{pid}/move")
    def pantry_move(pid: int, from_location: int = Form(), to_location: int = Form(), amount: float = Form(gt=0),
                    place: int | None = Form(None)):
        if to_location not in places(grocy).values():
            return inventory_back(place, err=GONE)
        if from_location != to_location:
            grocy.transfer(pid, amount, from_location, to_location)
        return inventory_back(place)

    @app.post("/inventory/{pid}/clear")
    def inventory_clear(pid: int, place: int | None = Form(None)):
        p = product_or_404(pid)
        with plan_lock:
            clear_stock(grocy, pid)
            rebuild()
        return inventory_back(place, f"Cleared {p['name']}.")

    @app.get("/inventory/{pid}/remove")
    def inventory_remove_confirm(request: Request, pid: int, place: int | None = None):
        """The confirmation step (works without JavaScript); a product that can't go shows why instead."""
        p = product_or_404(pid)
        return page(request, "inventory_remove.html", p=p, place=place, refusal=why_not_remove(grocy, pid))

    @app.post("/inventory/{pid}/remove")
    def inventory_remove(pid: int, place: int | None = Form(None)):
        p = product_or_404(pid)
        try:
            with plan_lock:
                remove_product(grocy, pid)
                rebuild()
        except ValueError as e:
            return inventory_back(place, err=str(e))
        return inventory_back(place, f"Removed {p['name']}.")

    # ---- places -------------------------------------------------------------------------------------------------
    def places_page(request: Request, status_code: int = 200, error: str | None = None):
        return page(request, "places.html", status_code, places=places(grocy), error=error)

    @app.get("/inventory/places")
    def places_get(request: Request):
        return places_page(request)

    def place_change(request: Request, change):
        try:
            change()
        except ValueError as e:
            return places_page(request, 400, str(e))
        return back("/inventory/places")

    @app.post("/inventory/places")
    def places_add(request: Request, name: str = Form("")):
        return place_change(request, lambda: add_place(grocy, name))

    @app.post("/inventory/places/{pid}/rename")
    def places_rename(request: Request, pid: int, name: str = Form("")):
        return place_change(request, lambda: rename_place(grocy, pid, name))

    @app.post("/inventory/places/{pid}/remove")
    def places_remove(request: Request, pid: int, move_to: int = Form()):
        return place_change(request, lambda: remove_place(grocy, pid, move_to))

    # ---- scanning -----------------------------------------------------------------------------------------------
    @app.get("/scan")
    def scan(request: Request):
        return page(request, "scan.html", generics=sorted(p["name"] for p in _generics(grocy)), locations=list(places(grocy)),
                    default_location=default_place(grocy))

    def is_supply(p: dict) -> bool:
        return bool(p.get("product_group_id")) and str(p["product_group_id"]) == str(supplies_group(grocy))

    def needs_min(r: Resolved, p: dict) -> bool:
        """A new supply: its keep-at-least is asked once per generic, not per brand."""
        return r.created and is_supply(p) and not float(generic_of(p).get("min_stock_amount") or 0)

    def resolve_code(code: str) -> Resolved | dict:
        """A scanned code's product, else what the page shows instead (unknown, the product form, an error)."""
        try:
            r = resolve(grocy, ai, http, code)
        except NeedsDetails as e:  # free mode: the scan screen's product form, name filled in
            return {"status": "manual", "code": code, "name": e.name}
        except AIError as e:
            return {"status": "error", "message": str(e)}
        if r is None:
            return {"status": "manual", "code": code, "name": ""} if ai.mode() == "free" else {"status": "unknown"}
        return r

    def best_before(p: dict) -> date | None:
        """The product's default best-before from today; None (Grocy's own) for never or none."""
        days = int(float(p.get("default_best_before_days") or 0))  # -1 = never expires
        return date.today() + timedelta(days=days) if days > 0 else None

    def place_of(p: dict) -> str:
        """The product's own place by name (the fallback place if its location is gone)."""
        loc = _int(p.get("location_id"))
        return next((n for n, i in places(grocy).items() if i == loc), default_place(grocy))

    def found(r: Resolved) -> dict:
        """What the scan sheet shows before anything is booked. Amounts are in the product's measure: `measure` is the
        word typed amounts go back with, `label` the word shown beside them, `keep` a new supply's suggested
        keep-at-least (one package); `package` stays in stock units (it is only ever multiplied, never shown)."""
        p = product_or_404(r.product_id)
        m = msr.of(grocy, ai, p)
        package = _num(grocy.userfields("products", r.product_id).get("package_amount"))
        left = grocy.stock_amounts().get(r.product_id, 0.0)
        return {"status": "found", "product_id": r.product_id, "product": r.name, "generic": r.generic_name,
                "measure": m.word, "label": msr.label(m), "package": package,
                "left": f"{msr.show(left, m)} left" if left > 0 else "None left",
                "keep": float(msr.value(package, m)) if package else 1,
                "place": place_of(p), "places": list(places(grocy)), "kind": "supply" if is_supply(p) else "food",
                "photo": None, "created": r.created, "need_min": needs_min(r, p)}

    class LookupIn(BaseModel):
        code: str

    @app.post("/api/scan/lookup")
    def api_scan_lookup(body: LookupIn):
        r = resolve_code(body.code.strip())
        return r if isinstance(r, dict) else found(r)

    class ConfirmIn(BaseModel):
        product_id: int
        action: Literal["add", "use"]
        packs: int = Field(default=1, ge=1)  # whole packages
        amount: float | None = Field(default=None, gt=0, allow_inf_nan=False)  # in `measure`, when the size is unknown
        place: str | None = None  # add only; the product's own place when not given
        min: float | None = Field(default=None, gt=0, allow_inf_nan=False)  # a new supply's keep-at-least, in `measure`
        measure: str | None = None  # the measure the sheet showed (lookup's); the product's own when not given

    @app.post("/api/scan/confirm")
    def api_scan_confirm(body: ConfirmIn):
        """Book what the sheet confirmed. The place applies to this booking only; supplies are used one at a time."""
        pid = body.product_id
        p = product_or_404(pid)
        try:
            m = typed(p, body.measure)
            if body.measure is None and (body.amount or body.min) and m.word != msr.stock_unit(grocy, p):
                raise ValueError(f"Say which measure the amount is in ({msr.label(m)}).")
        except ValueError as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
        known = _num(grocy.userfields("products", pid).get("package_amount"))
        amount = msr.to_stock(body.amount, m) if body.amount else None
        if body.action == "use" and is_supply(p):
            want = body.packs * m.factor  # one of its measure each
        else:
            want = amount or (body.packs * known if known else None)
        if not want:
            return JSONResponse({"status": "error", "message": "Type how much."}, status_code=400)
        have = places(grocy)
        place, tid = None, None
        if body.action == "add":
            place = body.place or place_of(p)
            if place not in have:
                return JSONResponse({"status": "error", "message": f"There's no place called {place}."},
                                    status_code=400)
            if amount and not known:  # remember the answer for this product's next scan
                size = {"package_amount": amount, "package_source": "owner"}
                grocy.set_userfields("products", pid, size)
                gid = _int(p.get("parent_product_id"))  # and for its generic, which Running low and Bought use
                if gid and not _num(grocy.userfields("products", gid).get("package_amount")):
                    grocy.set_userfields("products", gid, size)
            tid = grocy.add_stock(pid, want, best_before(p), have[place])
            done = want
        else:
            done = min(want, grocy.stock_amounts().get(pid, 0.0))  # Grocy refuses more than is in stock
            if done > 0:
                tid = grocy.consume(pid, done)
        if body.min:
            set_min(pid, msr.from_field(body.min, m, known) if known else msr.to_stock(body.min, m))  # keep: 1 package
        return {"status": "ok", "action": body.action, "product": p["name"], "amount": done, "place": place,
                "transaction_id": tid, "shown": msr.show(done, m), "short": done < want}

    class UndoIn(BaseModel):
        transaction_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")  # goes into Grocy's URL path
        action: Literal["add", "use"]  # the trip line's, to word a refusal

    @app.post("/api/scan/undo")
    def api_scan_undo(body: UndoIn):
        """Reverse only the stock booking: a remembered size, keep-at-least or relink stays."""
        try:
            grocy.undo(body.transaction_id)
        except GrocyError as e:
            if e.status != 400:
                raise
            return {"status": "error", "message": "Can't undo: already used." if body.action == "add" else "Can't undo that one."}
        return {"status": "ok"}

    @app.post("/api/scan/photo")
    def api_scan_photo(code: str = Form(), image: UploadFile = File()):
        try:
            r = resolve_from_photo(grocy, ai, http, code.strip(), image.file.read())
        except NeedsDetails:
            return {"status": "manual", "code": code.strip(), "name": ""}
        except AIError as e:
            return {"status": "error", "message": str(e)}
        return found(r)

    @app.post("/api/scan/manual")
    def api_scan_manual(code: str = Form(), name: str = Form(""),
                        generic: str = Form(""), stock_unit: Literal["g", "ml", "piece"] = Form("g"),
                        package_amount: str = Form(""), location: str = Form(""),
                        category: Literal["food", "supply"] = Form("food")):
        """Free mode: a new product from the scan screen's form, created as Claude's guess would have been."""
        location = location or default_place(grocy)
        if not (code.strip() and name.strip() and generic.strip()) or location not in places(grocy):
            msg = "Fill in the name, what it counts as and where it's kept."
            return JSONResponse({"status": "error", "message": msg}, status_code=400)
        if package_amount.strip() and _num(package_amount) is None:
            return JSONResponse({"status": "error", "message": "The package size must be a number above 0."},
                                status_code=400)
        r = create_manual(grocy, http, code.strip(), name.strip(), generic.strip(), stock_unit, _num(package_amount),
                          location, category)
        return found(r)

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
        waiting = sum(p.status in ("ready", "failed") for p in store.all())
        return page(request, "recipes.html", recipes=rs, waiting=waiting)

    @app.get("/recipes/{rid}")
    def recipe(request: Request, rid: int, servings: int | None = None, made: str = "",
               kit: int | None = Query(None, ge=0, le=1), msg: str = "", undo: list[str] = Query([])):
        """`kit`: the table's packets switch (1 Meal kit, 0 Our pantry; else whichever inventory matches). `msg` and
        `undo` (transaction ids): after adding the ingredients to inventory, with an Undo for that add."""
        note = "Made from the saved recipe text — the original page couldn't be reached." if made == "text" else msg
        return recipe_page(request, rid, servings, note=note or None, kit=kit, undo=undo if msg else [])

    def recipe_page(request: Request, rid: int, servings: int | None = None, error: str | None = None,
                    status_code: int = 200, note: str | None = None, kit: int | None = None, undo: list[str] = ()):
        r = recipe_or_404(rid)
        base = float(r.get("base_servings") or 2)
        servings = max(1, servings or round(base))
        scale = servings / base
        uf = grocy.userfields("recipes", rid)
        positions = grocy.objects("recipes_pos", recipe_id=rid)
        products = {int(p["id"]): p for p in grocy.objects("products")}
        info = {pid: {**grocy.userfields("products", pid), "name": products[pid]["name"]}
                for pid in {int(p["product_id"]) for p in positions} if pid in products}
        table = load_table(uf)
        has_kit = rid in kits(grocy, house)
        packets = has_kit if kit is None else bool(kit)  # packets only when cooking from the box
        cfe = _cfe(table, scale, packets)
        table_hash = hashlib.sha1(uf.get("cfe_table", "").encode()).hexdigest()[:8]  # an edited table starts clean
        fallback = [] if cfe else [ingredient_text(i, scale) for i in position_ingredients(positions, products)]
        return page(request, "recipe.html", status_code, r=r, servings=servings, cfe=cfe, fallback=fallback,
                    error=error, cross=f"cross:{rid}:{table_hash}",
                    macros=recipe_macros(positions, info, base) if positions else None,
                    source_url=uf.get("source_url"), sections=SECTIONS, today=date.today().isoformat(),
                    ai_free=ai_mode() == "free", note=note, undo=undo, packets=packets, kit=kit,
                    boxed=bool(cfe) and any(i.packets for i in table.ingredients),
                    **_doubt_ctx(_recipe_doubts(uf)))

    @app.post("/recipes/{rid}/make-table")
    def make_table_post(request: Request, rid: int):  # plain def: Claude takes a while, FastAPI threadpools it
        """Structure a recipe that has no table (its kept PDF, else its page, else its saved text) and save it."""
        r = recipe_or_404(rid)
        if ai.mode() == "free":  # before fetching anything
            return recipe_page(request, rid, error="The table can be made after the 1st.", status_code=409)
        uf = grocy.userfields("recipes", rid)
        pdf = settings.data_dir / "sources" / f"{rid}.pdf"  # where save_recipe keeps it; never a path from Grocy
        positions = grocy.objects("recipes_pos", recipe_id=rid)
        products = {int(p["id"]): p for p in grocy.objects("products")}
        lines = [p.get("note") or ingredient_text(i, 1) for p, i in zip(positions, position_ingredients(positions,
                                                                                                         products))]
        text = "\n".join([r["name"], "", "Ingredients:", *(f"- {x}" for x in lines), "", "Instructions:",
                          page_text(r.get("description") or "")])
        try:
            table, fell_back, doubts = make_table(ai, http, text, uf.get("source_url") or None,
                                                  pdf.read_bytes() if uf.get("source_pdf") and pdf.is_file() else None)
        except AIPaused:
            return recipe_page(request, rid, error="The table can be made after the 1st.", status_code=409)
        except (AIError, ValueError) as e:
            return recipe_page(request, rid, error=f"Couldn't make the table: {e}", status_code=502)
        grocy.set_userfields("recipes", rid, {"cfe_table": table.model_dump_json(),
                                              "doubts": json.dumps([d.model_dump() for d in doubts]) if doubts else ""})
        return back(f"/recipes/{rid}" + ("?made=text" if fell_back else ""))

    @app.get("/recipes/{rid}/photo")
    def recipe_photo(rid: int, w: int = 800):
        name = recipe_or_404(rid).get("picture_file_name")
        try:
            got = grocy.recipe_picture(name, min(max(w, 200), 1600)) if name else None
        except GrocyError as e:  # a missing or unreadable file: the page shows no photo rather than Grocy's error
            log.warning("recipe %s picture: %s", rid, e)
            got = None
        if got is None:
            raise HTTPException(404, "No photo")
        return Response(got[0], media_type=got[1], headers={"Cache-Control": "max-age=86400",
                                                                "X-Content-Type-Options": "nosniff"})

    @app.post("/recipes/{rid}/photo")
    def recipe_photo_post(request: Request, rid: int, photo: UploadFile = File()):
        recipe_or_404(rid)
        not_photo = lambda: recipe_page(request, rid, error="That file isn't a photo.", status_code=400)  # noqa: E731
        if not (photo.content_type or "").startswith("image/"):
            return not_photo()
        data = photo.file.read(UPLOAD_MAX + 1)
        if len(data) > UPLOAD_MAX:
            return recipe_page(request, rid, error="That photo is bigger than 10 MB.", status_code=413)
        try:
            jpeg = to_jpeg(data)
        except Exception:
            return not_photo()
        put_photo(grocy, rid, jpeg)
        return back(f"/recipes/{rid}")

    @app.post("/recipes/{rid}/plan")
    def recipe_plan(rid: int, day: date = Form(), section: Literal["Lunch", "Dinner"] = Form("Dinner"),
                    servings: str = Form("")):
        recipe_or_404(rid)
        with plan_lock:
            plan_meal(grocy, day, section, rid, _servings(servings), settings, house=house)
        return back("/plan")

    # ---- a recipe's ingredients added to inventory, as its meal kit ----------------------------------------------
    def kit_scale(r: dict, servings: int | None) -> tuple[int, float]:
        base = float(r.get("base_servings") or 2)  # as the recipe page reads it
        servings = max(1, servings or round(base))
        return servings, servings / base

    @app.get("/recipes/{rid}/stock")
    def recipe_stock(request: Request, rid: int, servings: int | None = None):
        """The confirm step: what will be added, in the table's words, and where; nothing is booked here."""
        r = recipe_or_404(rid)
        servings, scale = kit_scale(r, servings)
        lines, skipped = stock_lines(grocy, rid, scale)
        return page(request, "recipe_stock.html", r=r, servings=servings, lines=lines, skipped=skipped)

    @app.post("/recipes/{rid}/stock")
    def recipe_stock_post(rid: int, servings: int = Form(ge=1)):
        """Book each ingredient on its product, in its place with its best-before, kept as the recipe's meal kit
        (added again: it joins the same kit). Each booking is kept as soon as it is made, so a failure part way
        leaves every booked one in the kit and its Undo."""
        r = recipe_or_404(rid)
        servings, scale = kit_scale(r, servings)
        products = {int(p["id"]): p for p in grocy.objects("products")}
        tids = []
        with plan_lock:
            for line in stock_lines(grocy, rid, scale)[0]:
                tid, entries = grocy.add_stock_entries(line["pid"], line["amount"], best_before(products[line["pid"]]),
                                                       line["loc"])
                house.add_kit(rid, line["pid"], tid, entries)
                tids += [tid] if tid else []
            rebuild()
        n = len(tids)
        msg = f"Added {n} ingredient{'s' if n != 1 else ''}." if n else "Nothing to add."
        return back(f"/recipes/{rid}?" + urlencode([("servings", servings), ("msg", msg)] + [("undo", t) for t in tids]))

    @app.post("/recipes/{rid}/stock/undo")
    def recipe_stock_undo(rid: int, tid: list[str] = Form([])):
        """Take an add back out, kit and all; only that recipe's kit bookings, so no other booking can be undone here.
        One already used (in part) stays, in the kit, and is named."""
        recipe_or_404(rid)
        names = {int(p["id"]): p["name"] for p in grocy.objects("products")}
        mine = {k["transaction_id"]: int(k["product_id"]) for k in house.kit_items() if int(k["recipe_id"]) == rid}
        done, kept = 0, []
        with plan_lock:
            for t in dict.fromkeys(tid):
                if t not in mine:
                    continue
                try:
                    grocy.undo(t)
                except GrocyError as e:
                    if e.status != 400:
                        raise
                    kept.append(names.get(mine[t], "an ingredient"))
                    continue
                house.drop_kit(t)
                done += 1
            rebuild()
        if not kept:
            msg = f"Took {done} ingredient{'s' if done != 1 else ''} back out." if done else "Nothing to undo."
        else:
            which = kept[0] if len(kept) == 1 else f"{', '.join(kept[:-1])} and {kept[-1]}"
            msg = (f"Took {done} back out. " if done else "") + f"Couldn't undo {which}: some was already used."
        return back(f"/recipes/{rid}?" + urlencode({"msg": msg}))

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
        return page(request, "table.html", r=recipe_or_404(rid), table=table_for(rid), errors=[],
                    **_doubt_ctx(_recipe_doubts(grocy.userfields("recipes", rid))))

    @app.post("/recipes/{rid}/table")
    async def table_save(request: Request, rid: int):
        form = await request.form()
        return await run_in_threadpool(save_table, request, rid, form)

    def save_table(request: Request, rid: int, form):
        r = recipe_or_404(rid)
        old = table_for(rid)
        table = _table_from_form(form, old)
        action = str(form.get("action") or "")
        editing = action == "addop" or action.startswith("remove:")  # re-render only
        errors = [f"{table.ingredients[i].name}: {PACKET_UNIT}" for i in _packet_units(form, len(table.ingredients))]
        errors += [] if editing else _named(validate(table), table)
        if editing or errors:
            stored = _recipe_doubts(grocy.userfields("recipes", rid))
            return page(request, "table.html", r=r, table=table, errors=errors,
                        **_doubt_ctx(_standing(stored, old, table), stored))
        grocy.set_userfields("recipes", rid, {"cfe_table": table.model_dump_json(), "doubts": ""})  # all looked at
        follow_amounts(rid, old, table)
        return back(f"/recipes/{rid}")

    def follow_amounts(rid: int, old: CfeTable, new: CfeTable) -> None:
        """Rows whose amount the editor changed: their positions take it, in the stock unit through a known
        conversion, else saved without one for the estimate to fill in (importer.fill_estimates). A row finds its
        position by the line saved with it (save_recipe's note), never by order: Make the table may order rows
        differently. A row no position or several match is left alone."""
        positions = grocy.objects("recipes_pos", recipe_id=rid)
        for a, b in zip(old.ingredients, new.ingredients):
            if (a.qty, a.unit) != (b.qty, b.unit):
                same = [p for p in positions if p.get("note") == line_text(a, 1)]
                if len(same) != 1:
                    log.info("recipe %s: no single position for %r; its stock amount is left", rid, line_text(a, 1))
                    continue
                pos = same[0]
                amount = stock_amount_for(grocy, int(pos["product_id"]), b.qty, b.unit)
                grocy.update("recipes_pos", int(pos["id"]), {
                    "amount": amount or 0, "note": line_text(b, 1),
                    "variable_amount": "" if amount is not None else needs_estimate(b).get("variable_amount", "")})

    # ---- import -------------------------------------------------------------------------------------------------
    @app.get("/import")
    def import_list(request: Request):
        pend = store.all()
        refresh = 5 if any(p.status == "processing" for p in pend) else \
            30 if any(p.status == "reading" for p in pend) else None  # a batch takes minutes: check less often
        return page(request, "import.html", pending=pend, resume=_resume_day(), refresh=refresh)

    @app.post("/import")
    def import_post(bg: BackgroundTasks, files: list[UploadFile] = File(default=[]), urls: str = Form("")):
        pdfs = [(store.new(f.filename or "upload.pdf", pdf=data), data) for f in files if (data := f.file.read())]
        if not (len(pdfs) >= BATCH_MIN and ai.mode() == "economy" and submit_batch(store, ai, [p for p, _ in pdfs])):
            for p, data in pdfs:
                bg.add_task(process, store, ai, http, p.id, None, data)
        for url in filter(None, (u.strip() for u in urls.splitlines())):
            p = store.new(url)
            bg.add_task(process, store, ai, http, p.id, url, None)
        return back("/import")

    def manual_details(m: dict) -> dict:
        """The review row's new-product fields (free mode), checked like the scan screen's."""
        if not m["stock_unit"]:  # no default: Create all must not count every new product in grams
            raise ValueError("Choose what it's counted in.")
        if m["stock_unit"] not in ("g", "ml", "piece") or m["location"] not in places(grocy) \
                or m["category"] not in ("food", "supply"):
            raise ValueError("Pick how it's counted, where it's kept and what kind it is.")
        if m["package"] and _num(m["package"]) is None:
            raise ValueError("The package size must be a number above 0.")
        return {"generic": m["generic"], "stock_unit": m["stock_unit"], "package_amount": _num(m["package"]),
                "location": m["location"], "category": m["category"]}

    def pending_or_404(pid: str):
        p = store.get(pid)
        if p is None:
            raise HTTPException(404, "No such import")
        return p

    def review_page(request: Request, p, table: CfeTable, rows: list[dict], errors: list[str],
                    doubts: list[dict] = ()):
        gens = sorted(_generics(grocy), key=lambda g: g["name"].lower())
        for ing, row in zip(table.ingredients, rows):
            row["text"], row["packets"] = line_text(ing, 1), packets_of(ing)
        tableless = p.tableless and not table.ops  # read without Claude: the table can be made later
        return page(request, "review.html", p=p, rows=rows, generics=gens, table=table, cfe=_cfe(table),
                    photo=store.photo_path(p.id) is not None, tableless=tableless, ai_free=ai.mode() == "free",
                    pdf=store.pdf_path(p.id) is not None, missing=sum(not r["product_id"] for r in rows),
                    source_url=p.source if p.source.startswith(("http://", "https://")) else None,
                    locations=list(places(grocy)), default_location=default_place(grocy),
                    table_errors=[] if tableless else _named(validate(table), table), errors=_named(errors, table),
                    **_doubt_ctx(doubts, p.doubts))

    def next_review(none: str) -> RedirectResponse:
        """The oldest import ready for review, else `none`."""
        p = next((p for p in store.all() if p.status == "ready"), None)
        return back(f"/import/{p.id}" if p else none)

    @app.get("/import/next")  # before /import/{pid}, which would take "next" as an id
    def review_next():
        return next_review("/import")

    @app.get("/import/{pid}")
    def review(request: Request, pid: str):
        p = pending_or_404(pid)
        if p.status != "ready":
            return page(request, "review.html", p=p, resume=_resume_day())
        known = dict(p.ai_picks)
        matches = propose_matches(grocy, ai, p.draft, p.ai_picks)
        if p.ai_picks != known:
            store.save(p)  # Claude's picks kept: reloading this page costs nothing
        rows = [{"product_id": m.product_id, "new_name": ing.name, "error": None}
                for m, ing in zip(matches, p.draft.table.ingredients)]
        return review_page(request, p, p.draft.table, rows, [], p.doubts)

    @app.get("/import/{pid}/photo")
    def review_photo(pid: str):
        f = store.photo_path(pid)
        if f is None:
            raise HTTPException(404, "No photo")
        return FileResponse(f, media_type="image/jpeg")

    @app.get("/import/{pid}/source")
    def review_source(pid: str):
        f = store.pdf_path(pid)  # the store only accepts its own ids, so no path tricks
        if f is None:
            raise HTTPException(404, "No PDF for this import")
        return FileResponse(f, media_type="application/pdf", filename=f.name, content_disposition_type="inline")

    @app.post("/import/{pid}")
    async def review_post(request: Request, pid: str):
        """One form for the whole review; `action` is save, create:<i>, create_all, addop or remove:<step> (re-render)."""
        form = await request.form()
        return await run_in_threadpool(review_submit, request, pid, form)  # Grocy/Claude calls block for seconds

    def review_submit(request: Request, pid: str, form):
        p = pending_or_404(pid)
        if p.status != "ready":
            return back(f"/import/{pid}")
        action = form.get("action") or "save"
        table = _table_from_form(form, p.draft.table)
        refused = _packet_units(form, len(table.ingredients))
        rows: list[dict] = []
        for i, ing in enumerate(table.ingredients):
            f = lambda k: str(form.get(f"{k}_{i}") or "").strip()  # noqa: E731
            row = {"product_id": _int(f("product")), "new_name": f("new_name") or ing.name, "error": None,
                   "manual": {k: f(k) for k in ("generic", "stock_unit", "package", "location", "category")}}
            if action == f"create:{i}" or (action == "create_all" and not row["product_id"]):
                try:
                    row["product_id"] = create_generic(grocy, ai, http, settings, row["new_name"],
                                                       manual_details(row["manual"]) if ai.mode() == "free" else None)
                except (ValueError, AIError) as e:
                    row["error"] = str(e)
            if i in refused:
                row["error"] = PACKET_UNIT
            rows.append(row)
        errors: list[str] = []
        if action == "save" and not refused:
            known = dict(p.estimates)
            amounts = [estimate_amount(grocy, ai, p, r["product_id"], ing.qty, ing.unit) if r["product_id"] else None
                       for ing, r in zip(table.ingredients, rows)]  # Claude's guesses, never shown or typed
            if p.estimates != known:
                store.save(p)  # kept: a failed Save doesn't ask again
            draft = p.draft.model_copy(update={"table": table})
            source_url = p.source if p.source.startswith(("http://", "https://")) else None
            with save_lock:  # a double-tapped Save must not create the recipe twice
                if store.get(pid) is None:
                    return next_review("/recipes")  # the other tap already saved it
                try:
                    save_recipe(grocy, draft, [IngredientMatch(i, r["product_id"], a)
                                               for i, (r, a) in enumerate(zip(rows, amounts))], source_url,
                                store.pdf_path(pid), photo=store.photo_path(pid), need_table=not p.tableless,
                                doubts=_standing(p.doubts, p.draft.table, table))
                except ValueError as e:
                    errors.append(str(e))
                else:
                    store.delete(pid)  # first: the recipe is saved even if a conversion below fails
                    for ing, r in zip(table.ingredients, rows):  # the estimates used become the products' conversions
                        if (factor := p.estimates.get(estimate_key(r["product_id"], ing.unit))) is not None:
                            try:  # the recipe stands; a refused one is simply not kept
                                add_conversion(grocy, r["product_id"], ing.unit, factor)
                            except Exception:
                                log.warning("could not store the estimate for product %s", r["product_id"],
                                            exc_info=True)
                    return next_review("/recipes")
        return review_page(request, p, table, rows, errors, _standing(p.doubts, p.draft.table, table))

    @app.post("/import/{pid}/discard")
    def discard(pid: str, then: str = Form("")):
        store.delete(pid)
        return next_review("/recipes") if then == "next" else back("/import")  # the review goes on; the list stays

    # ---- week plan ----------------------------------------------------------------------------------------------
    def plan_page(request: Request, monday: date, short: list[str] = (), picks: list[dict] | None = None,
                  error: str | None = None):
        recipes_all = grocy.objects("recipes")
        rnames = {int(r["id"]): r["name"] for r in recipes_all}
        choices = sorted(((r["id"], r["name"]) for r in user_recipes(grocy)), key=lambda t: t[1].lower())

        def entry(r: dict) -> dict:
            is_recipe = r.get("type") == "recipe"
            rid = _int(r.get("recipe_id"))
            return {"id": int(r["id"]), "recipe": is_recipe, "done": bool(int(float(r.get("done") or 0))),
                    "rid": rid if rid in rnames else None,
                    "text": rnames.get(rid, "(deleted recipe)") if is_recipe
                    else r.get("note", ""),
                    "servings": _fmt(r.get("recipe_servings"))}

        days = [{"day": d.day, "lunch": [entry(r) for r in d.lunch], "dinner": [entry(r) for r in d.dinner]}
                for d in week_view(grocy, monday)]
        return page(request, "plan.html", days=days, monday=monday, prev=monday - timedelta(days=7),
                    next=monday + timedelta(days=7), recipes=choices, short=list(short), picks=picks, error=error,
                    today=date.today())

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
            plan_meal(grocy, day, section, recipe_id, _servings(servings), settings, house=house)
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
            unplan_meal(grocy, entry_id, settings, house=house)
        return back(f"/plan?week={week}")

    @app.post("/plan/{entry_id}/cook")
    def plan_cook(entry_id: int):
        week = entry_week(entry_id)
        with plan_lock:
            short = cook(grocy, entry_id, house)
        return back("/plan?" + urlencode([("week", week)] + [("short", n) for n in short]))

    @app.post("/plan/suggest")
    def plan_suggest(request: Request, week: date = Form()):  # plain def: Claude takes seconds, FastAPI threadpools it
        monday = _monday(week)
        try:
            picks = suggest(grocy, ai, monday)
        except AIError as e:
            return plan_page(request, monday, error=str(e))
        names = {int(r["id"]): r["name"] for r in grocy.objects("recipes")}
        shown = [{"day": p.day.isoformat(), "label": p.day.strftime("%A"), "section": p.section,
                  "recipe_id": p.recipe_id, "name": names.get(p.recipe_id, ""), "reason": p.reason} for p in picks]
        last_picks[monday] = shown
        return plan_page(request, monday, picks=shown)

    # ---- shopping -----------------------------------------------------------------------------------------------
    @app.get("/shopping")
    def shopping(request: Request, background: BackgroundTasks, bad: int = 0):
        i = ids(grocy)
        with plan_lock:
            rebuild()
        products = {int(p["id"]): p for p in grocy.objects("products")}
        units = unit_names()

        def view(row: dict) -> dict:
            pid = _int(row.get("product_id"))
            p = products.get(pid)
            amount = float(row.get("amount") or 0)
            pkg = _num(grocy.userfields("products", pid).get("package_amount")) if p else None
            unit = units.get(_int(p.get("qu_id_stock")), "") if p else ""
            return {"id": int(row["id"]), "pid": pid if p else None, "name": p["name"] if p else row.get("note") or "(item)",
                    "amount": amount, "unit": unit, "m": msr.of(grocy, None, p, unit) if p else None,
                    "done": bool(int(float(row.get("done") or 0))),
                    "packs": packages_needed(amount, pkg) if pkg else None, "pkg": pkg}

        lists = {n: [view(r) for r in grocy.objects("shopping_list", shopping_list_id=i.list[n])]
                 for n in ("Meal plan", "Running low", "Extras")}
        # ticked = already got it, so it stays out of the Walmart hand-off; a product is on one list only
        todo = {r["pid"]: r["amount"] for r in lists["Meal plan"] + lists["Running low"] if r["pid"] and not r["done"]}
        bare = sorted({r["pid"] for n in ("Meal plan", "Running low") for r in lists[n] if r["pid"] and r["m"].word == r["unit"]})
        if bare and ai.mode() != "free":
            background.add_task(fill_measures, bare)
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
            for name in ("Meal plan", "Running low", "Extras"):
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
            rebuild()
            return back("/shopping")

    @app.post("/products/{pid}/walmart")
    def product_walmart(pid: int, link: str = Form()):
        if grocy.get_object("products", pid) is None:
            raise HTTPException(404, "No such product")
        item = parse_item_id(link)
        if item is None:
            return back("/shopping?bad=1")
        grocy.set_userfields("products", pid, {"walmart_item_id": item})
        refine(grocy, ai, http, pid, link, item)
        return back("/shopping")

    @app.get("/api/expiring")
    def api_expiring():
        return expiring(grocy, settings.expiry_warn_days)

    @app.get("/api/summary")
    def api_summary():
        """Read-only numbers (what the app pushes to Home Assistant's sensor.house_* every minute)."""
        return summary(grocy, house, ha, datetime.now(), settings.expiry_warn_days)

    return app


def main_app() -> FastAPI:
    """uvicorn app.web:main_app --factory"""
    s = load_settings()
    house = HouseStore(s.data_dir / "house.db")
    ai = AI(anthropic.Anthropic(), house, s.ai_monthly_budget, s.ai_economy_at)
    http, store = httpx.Client(timeout=20), PendingStore(s.data_dir / "pending")
    grocy, ha = Grocy(s.grocy_url, s.grocy_api_key), HA(s.ha_url, s.ha_token) if s.ha_url else None
    try:  # an update brings new userfields (Grocy refuses unknown ones); bootstrap only adds what is missing
        bootstrap(grocy)
    except Exception:
        log.warning("could not set up Grocy; starting anyway (tried again on the next start)", exc_info=True)
    start_worker(store, ai, http, grocy)  # batch reads, queued imports after the 1st, amounts saved without one
    if ha is not None:
        ha_live.start(grocy, house, ha, s)  # phone taps and the washer, over HA's WebSocket
    return create_app(grocy, ai, http, s, store, house=house, ha=ha)
