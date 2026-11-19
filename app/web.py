"""Web UI: pantry, scanning, recipes and recipe import (server-rendered; Grocy holds all data)."""
import hmac
import itertools
import logging
import math
import mimetypes
import re
import threading
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urlsplit
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import anthropic
import httpx
import jinja2
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
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
from app import home as hm
from app.house import LOCK, HouseStore, Person, by_grocy_user
from app import reminders as rm
from app.reminders import start_loop
from app.importer import (IngredientMatch, PendingStore, add_conversion, create_generic, process, propose_matches,
                          put_photo, save_recipe, stock_amount_for, to_jpeg)
from app.qty import is_packaging
from app.mealplan import cook, plan_meal, unplan_meal, week_view
from app.nutrition import recipe_macros
from app.setup_grocy import LOCATIONS, SECTIONS, ids, supplies_group
from app.shopping import rebuild_meal_plan_list, rebuild_running_low, walmart_items
from app.suggest import suggest, user_recipes
from app.today import _day, today_view
from app.walmart import cart_url, packages_needed, parse_item_id, search_url

HERE = Path(__file__).parent
NEVER = "2999-12-31"  # Grocy's best-before for "never expires"
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


def _factor(s: str) -> float:
    f = float(s)  # ValueError for non-numbers; its message is shown to the reviewer
    if not (math.isfinite(f) and f > 0):
        raise ValueError(f"conversion factor must be a finite number above 0, got {s}")
    return f


def _table_from_form(form, old: CfeTable) -> CfeTable:
    """Rebuild the ops from the editor: per step row an op_id (hidden), an op_label, an op_where and the ticked chips
    op_in_<row> (ingredient ids or earlier steps' op_ids). Blank rows are dropped, action remove:<row> drops that step and the
    ticks pointing at it, and steps are renumbered o1, o2, ... in row order. A tick that names no ingredient and no
    earlier step (unknown, later, itself) is kept as-is so validate() rejects it."""
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
    return old.model_copy(update={"ops": [Op(id=new[n], label=lab, where=where,
                                             inputs=[new[r] if isinstance(r, int) else r for r in refs])
                                          for n, ((_, lab, _, where), refs) in enumerate(zip(rows, picked))]})


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
    if settings.ha_url:
        start_loop(lambda: (grocy, house, ha, settings))
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters["num"] = _fmt
    env.filters["exact"] = _exact
    env.globals["chip_names"] = _chip_names
    env.globals["chip_users"] = _chip_users
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
    def today(request: Request, msg: str = ""):
        who = current_who(request)
        if who is None:
            return back("/who")
        now = datetime.now()
        return page(request, "today.html", msg=msg, date=f"{now:%A, %B} {now.day}",
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

    # ---- Home Assistant hooks (header X-House-Secret; no HOOK_SECRET set = hooks off) ----------------------------
    def hook_secret(x_house_secret: str = Header("")):
        if not settings.hook_secret or not hmac.compare_digest(x_house_secret.encode(), settings.hook_secret.encode()):
            raise HTTPException(403, "Wrong or missing X-House-Secret")

    class ActionIn(BaseModel):
        action: str

    @app.post("/api/hook/action", dependencies=[Depends(hook_secret)])
    def hook_action(body: ActionIn):
        status = rm.action(grocy, house, ha, body.action, datetime.now())
        if status is None:
            return JSONResponse({"status": "error", "message": "not a house action"}, status_code=400)
        if status == "unknown-person":
            return JSONResponse({"status": "error", "message": "unknown person"}, status_code=404)
        return {"status": status}

    @app.post("/api/events/washer-finished", dependencies=[Depends(hook_secret)])
    def washer_finished():
        return {"status": rm.washer_finished(grocy, house, datetime.now())}

    @app.post("/api/events/washer-started", dependencies=[Depends(hook_secret)])
    def washer_started():
        return {"status": rm.washer_started(grocy, house, ha)}

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
    def me(request: Request):
        return page(request, "me.html", who=current_who(request))

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

    def set_min(pid: int, amount: float) -> None:
        gid = generic_of(product_or_404(pid))["id"]
        grocy.update("products", gid, {"min_stock_amount": amount, "cumulate_min_stock_amount_of_sub_products": 1})
        with plan_lock:
            rebuild_running_low(grocy)

    @app.get("/home/supplies")
    def supplies(request: Request, msg: str = ""):
        units, stock = unit_names(), grocy.stock_amounts()  # a generic's amount includes its brands
        rows = [{"id": p["id"], "name": p["name"], "amount": stock.get(int(p["id"]), 0.0),
                 "min": float(p.get("min_stock_amount") or 0), "unit": units.get(_int(p.get("qu_id_stock")), "")}
                for p in hm.supplies(grocy) if not p.get("parent_product_id")]
        return page(request, "supplies.html", msg=msg, supplies=sorted(rows, key=lambda r: r["name"].lower()))

    @app.post("/home/supplies/{pid}/used")
    def supply_used(pid: int):
        """One unit (a bag, a roll), whatever the package size; never more than is in stock (Grocy refuses that)."""
        p = product_or_404(pid)  # a generic: Grocy takes the stock from its brands
        with plan_lock:
            use = min(1.0, grocy.stock_amounts().get(pid, 0.0))
            if use > 0:
                grocy.consume(pid, use)
            rebuild_running_low(grocy)
        msg = f"Used one {p['name']}." if use > 0 else f"No {p['name']} left to use."
        return back("/home/supplies?" + urlencode({"msg": msg}))

    @app.post("/home/supplies/{pid}/min")
    def supply_min(pid: int, amount: float = Form(ge=0, allow_inf_nan=False)):
        set_min(pid, amount)  # 0 = never goes on Running low
        return back("/home/supplies")

    class MinIn(BaseModel):
        amount: float = Field(gt=0, allow_inf_nan=False)

    @app.post("/api/products/{pid}/min")
    def api_min(pid: int, body: MinIn):
        set_min(pid, body.amount)
        return {"status": "ok"}

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
        group, today = supplies_group(grocy), date.today()
        # brands copy their generic's group, so a product's own group is enough
        food = lambda pid: str(products.get(int(pid), {}).get("product_group_id")) != str(group)  # noqa: E731

        def when(bb, verb: str) -> tuple[str, bool]:
            """("Use by Friday" / "Best before Friday", past?) in Today's words."""
            try:
                d = date.fromisoformat(str(bb)[:10])
            except ValueError:
                return "", False
            if d < today:
                return "Past its date", True
            return f"{verb} {'by ' if verb == 'Use' and d > today else ''}{_day(d, today)}", False

        vol = grocy.volatile(settings.expiry_warn_days)
        soon = [{"name": name(r["product_id"]), "when": w, "past": past}
                for key in ("expired_products", "overdue_products", "due_products") for r in vol.get(key) or []
                if food(r["product_id"]) for w, past in [when(r.get("best_before_date"), "Use")]]
        rows: dict[tuple[int, int], dict] = {}
        totals: dict[int, float] = {}
        for e in grocy.stock_entries():
            pid, loc = int(e["product_id"]), _int(e.get("location_id")) or 0
            if not food(pid):
                continue  # household supplies live under Chores, Supplies
            row = rows.setdefault((pid, loc), {"pid": pid, "loc": loc, "name": name(pid), "amount": 0.0, "bb": NEVER,
                                               "unit": units.get(_int(products.get(pid, {}).get("qu_id_stock")), "")})
            row["amount"] += float(e["amount"])
            row["bb"] = min(row["bb"], str(e.get("best_before_date") or NEVER)[:10])
            totals[pid] = totals.get(pid, 0.0) + float(e["amount"])
        groups: dict[str, list[dict]] = {}
        for row in sorted(rows.values(), key=lambda r: (r["bb"], r["name"].lower())):
            if row["amount"] > 0:
                row["when"], row["past"] = when(row["bb"], "Best before") if row["bb"] != NEVER else ("", False)
                groups.setdefault(locs.get(row["loc"], "Other"), []).append(row)
        order = {n: k for k, n in enumerate(LOCATIONS)}
        groups = dict(sorted(groups.items(), key=lambda kv: order.get(kv[0], len(order))))
        return page(request, "pantry.html", soon=soon, groups=groups, totals=totals, locations=i.location)

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
        known = _num(grocy.userfields("products", r.product_id).get("package_amount"))
        pkg = amount or known
        unit = unit_names().get(_int(p.get("qu_id_stock")), "")
        supply = p.get("product_group_id") and str(p["product_group_id"]) == str(supplies_group(grocy))
        if supply and action == "use":
            pkg = 1.0  # supplies are used one at a time
        if action == "add" and r.created and supply and not float(generic_of(p).get("min_stock_amount") or 0):
            # a new supply: ask its keep-at-least once per generic, not per brand
            return {"status": "need_min", **info, "unit": unit, "default": pkg or 1}
        if pkg is None:
            return {"status": "need_amount", **info, "unit": unit}
        if action == "add":
            if amount and not known:  # remember the answer for this product's next scan
                grocy.set_userfields("products", r.product_id, {"package_amount": amount})
                gid = _int(p.get("parent_product_id"))  # and for its generic, which Running low and Bought use
                if gid and not _num(grocy.userfields("products", gid).get("package_amount")):
                    grocy.set_userfields("products", gid, {"package_amount": amount})  # a brand has its generic's unit
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
        return recipe_page(request, rid, servings)

    def recipe_page(request: Request, rid: int, servings: int | None = None, error: str | None = None,
                    status_code: int = 200):
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
        return page(request, "recipe.html", status_code, r=r, servings=servings, cfe=cfe, fallback=fallback,
                    error=error,
                    macros=recipe_macros(positions, info, base) if positions else None,
                    source_url=uf.get("source_url"), sections=SECTIONS, today=date.today().isoformat())

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
        return Response(got[0], media_type=got[1], headers={"Cache-Control": "max-age=86400"})

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
        action = str(form.get("action") or "")
        editing = action == "addop" or action.startswith("remove:")  # re-render only
        errors = [] if editing else _named(validate(table), table)
        if editing or errors:
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
                    photo=store.photo_path(p.id) is not None,
                    table_errors=_named(validate(table), table), errors=_named(errors, table))

    @app.get("/import/{pid}")
    def review(request: Request, pid: str):
        p = pending_or_404(pid)
        if p.status != "ready":
            return page(request, "review.html", p=p)
        rows = [{"product_id": m.product_id, "amount": m.stock_amount, "prev": m.product_id, "factor": "",
                 "new_name": ing.name, "error": None, "note": None}
                for m, ing in zip(propose_matches(grocy, ai, p.draft), p.draft.table.ingredients)]
        return review_page(request, p, p.draft.table, rows, [])

    @app.get("/import/{pid}/photo")
    def review_photo(pid: str):
        f = store.photo_path(pid)
        if f is None:
            raise HTTPException(404, "No photo")
        return FileResponse(f, media_type="image/jpeg")

    @app.post("/import/{pid}")
    async def review_post(request: Request, pid: str):
        """One form for the whole review; `action` is save, create:<i>, suggest:<i>, addop or remove:<step> (re-render)."""
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
                                                     for i, r in enumerate(rows)], source_url, store.pdf_path(pid),
                                      photo=store.photo_path(pid))
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
        shown = [{"day": p.day.isoformat(), "label": p.day.strftime("%A"), "section": p.section,
                  "recipe_id": p.recipe_id, "name": names.get(p.recipe_id, ""), "reason": p.reason} for p in picks]
        last_picks[monday] = shown
        return plan_page(request, monday, picks=shown)

    # ---- shopping -----------------------------------------------------------------------------------------------
    @app.get("/shopping")
    def shopping(request: Request, bad: int = 0):
        i = ids(grocy)
        with plan_lock:
            rebuild_meal_plan_list(grocy, date.today(), settings.shopping_weekday)
            rebuild_running_low(grocy)
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
                 for n in ("Meal plan", "Running low", "Extras")}
        todo: dict[int, float] = {}  # ticked = already got it, so it stays out of the Walmart hand-off
        # a food product with both a minimum and a plan shortfall counts twice here and in Bought (only matters
        # if food gets minimums)
        for r in lists["Meal plan"] + lists["Running low"]:
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
            rebuild_meal_plan_list(grocy, date.today(), settings.shopping_weekday)
            rebuild_running_low(grocy)
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

    @app.get("/api/summary")
    def api_summary():
        """Read-only numbers for Home Assistant sensors (LAN only, like /api/expiring)."""
        now = datetime.now()
        try:  # one HA call (2 s cap) for everyone, not one per person
            home = ha.home_states(timeout=2) if ha is not None else None
        except HAError:
            home = None
        shim = SimpleNamespace(home_states=lambda timeout=None: home) if home is not None else None
        people = {}
        for p in house.people():
            v = today_view(grocy, house, p, now, ha=shim, warn_days=settings.expiry_warn_days)
            people[p.name] = {"now": len(v["now_items"]), "titles": [i.title for i in v["now_items"]]}
        vol = grocy.volatile(settings.expiry_warn_days)
        use_soon = {int(r["product_id"]) for k in ("expired_products", "overdue_products", "due_products")
                    for r in vol.get(k) or []}
        key = house.flag("laundry")
        return {"people": people, "use_soon": len(use_soon), "running_low": len(vol.get("missing_products") or []),
                "laundry_open": bool(key) and key in rm.open_items(grocy), "ha_ok": home is not None}

    return app


def main_app() -> FastAPI:
    """uvicorn app.web:main_app --factory"""
    s = load_settings()
    return create_app(Grocy(s.grocy_url, s.grocy_api_key), AI(anthropic.Anthropic()), httpx.Client(timeout=20), s,
                      PendingStore(s.data_dir / "pending"))
