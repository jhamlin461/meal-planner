"""Recipe import: fetch/parse sources, queue drafts for human review, save reviewed recipes to Grocy."""
import base64
import io
import ipaddress
import itertools
import json
import logging
import math
import re
import secrets
import shutil
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from difflib import get_close_matches
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin

import httpx
from PIL import Image, ImageOps
from pydantic import BaseModel
from pypdf import PdfReader

from app.ai import AIError, AIPaused, Doubt, RecipeDraft, TableCheck
from app.barcode import _generic_named, _generics, _product_fields, create_manual, generic_choices
from app.cfe import CfeTable, Ingredient, line_text, validate
from app.nutrition import fill_missing_nutrition
from app.qty import is_packaging, normalize_unit, parse_qty, to_stock_amount
from app.inventory import places
from app.setup_grocy import ids

UA = "MealPlanner/1.0"
log = logging.getLogger(__name__)


class _Parser(HTMLParser):
    """Collects visible text (scripts/styles dropped) and the bodies of JSON-LD scripts."""

    def __init__(self, html: str):
        super().__init__()
        self.text: list[str] = []
        self.ld: list[str] = []
        self._skip: str | None = None
        self._ld: list[str] | None = None
        self.og: str | None = None  # the first og:image
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and self.og is None and "og:image" in (a.get("property"), a.get("name")) and a.get("content"):
            self.og = a["content"].strip()
        if tag in ("script", "style"):
            self._skip = tag
            if tag == "script" and (a.get("type") or "").strip().lower() == "application/ld+json":
                self._ld = []

    def handle_endtag(self, tag):
        if tag == self._skip:
            if self._ld is not None:
                self.ld.append("".join(self._ld))
            self._skip, self._ld = None, None

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
        elif not self._skip and data.strip():
            self.text.append(data.strip())


def _recipes(node):
    if isinstance(node, list):
        for n in node:
            yield from _recipes(n)
    elif isinstance(node, dict):
        t = node.get("@type")
        if "Recipe" in (t if isinstance(t, list) else [t]):
            yield node
        yield from _recipes(node.get("@graph", []))


def _steps(node) -> list[str]:
    if isinstance(node, str):
        return [node.strip()] if node.strip() else []
    if isinstance(node, list):
        return [s for n in node for s in _steps(n)]
    if isinstance(node, dict):
        if "itemListElement" in node:  # HowToSection
            return ([f"{node['name']}:"] if node.get("name") else []) + _steps(node["itemListElement"])
        return _steps(node.get("text") or node.get("name") or "")
    return []


def _flat(v) -> str:
    return ", ".join(map(str, v)) if isinstance(v, list) else str(v)


def _jsonld(html: str) -> dict | None:
    """The page's first JSON-LD Recipe."""
    for body in _Parser(html).ld:
        try:
            data = json.loads(body)
        except ValueError:
            continue
        for r in _recipes(data):
            return r
    return None


def extract_jsonld_recipe(html: str) -> str | None:
    r = _jsonld(html)
    if r is None:
        return None
    lines = [_flat(r.get("name", ""))]
    if r.get("recipeYield"):
        lines.append(f"Yield: {_flat(r['recipeYield'])}")
    lines += ["", "Ingredients:"] + [f"- {i}" for i in r.get("recipeIngredient") or []]
    lines += ["", "Instructions:"] + [f"- {s}" for s in _steps(r.get("recipeInstructions"))]
    return "\n".join(lines)


UNITS = {"cup", "tbsp", "tsp", "g", "kg", "ml", "l", "oz", "lb", "pinch", "dash", "clove", "can", "slice", "stick",
         "bunch"}


def _ingredient(line: str) -> Ingredient:
    """'2 Tbsp. butter' -> 2, tbsp, butter: a leading amount, a known unit word, the rest is the name."""
    toks = line.split()
    n = 0
    while n < len(toks) and parse_qty(" ".join(toks[:n + 1])) is not None:
        n += 1
    qty = parse_qty(" ".join(toks[:n])) if n else None
    unit = ""
    if qty is not None and n < len(toks):
        u = normalize_unit(toks[n].rstrip(".,"))
        u = u if u in UNITS else u[:-1] if u[:-1] in UNITS and u.endswith("s") else None
        if u:
            unit, n = u, n + 1
    return Ingredient(qty=qty, unit=unit, name=" ".join(toks[n:]) or line.strip())


def draft_from_jsonld(r: dict) -> RecipeDraft:
    """Free mode: the page's own ingredients and steps, without a CfE table (no ops)."""
    servings = re.search(r"\d+", _flat(r.get("recipeYield") or ""))
    ings = [_ingredient(str(i)) for i in r.get("recipeIngredient") or [] if str(i).strip()]
    return RecipeDraft(title=_flat(r.get("name") or "") or "Recipe", servings=int(servings[0]) if servings else None,
                       steps=_steps(r.get("recipeInstructions")), table=CfeTable(pre=[], ingredients=ings, ops=[]))


def page_text(html: str) -> str:
    return "\n".join(_Parser(html).text)


def _image(node) -> str | None:
    """A JSON-LD `image`: a URL string, an ImageObject, or a list of either (the first wins)."""
    if isinstance(node, list):
        return next(filter(None, map(_image, node)), None)
    if isinstance(node, dict):
        return _image(node.get("url") or node.get("contentUrl"))
    return (node.strip() or None) if isinstance(node, str) else None


def photo_url(html: str, base: str) -> str | None:
    """The dish photo's absolute URL: the recipe JSON-LD `image`, else og:image."""
    p = _Parser(html)
    for body in p.ld:
        try:
            data = json.loads(body)
        except ValueError:
            continue
        for r in _recipes(data):
            if url := _image(r.get("image")):
                return urljoin(base, url)
    return urljoin(base, p.og) if p.og else None


PHOTO_EDGE, PHOTO_MAX, PHOTO_SECONDS = 1600, 5 * 1024 * 1024, 15
MAX_PIXELS = 40_000_000  # bigger than any real photo; decoding more is how a tiny file eats the memory
Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # past twice this Pillow refuses even to open it


def to_jpeg(data: bytes | Image.Image) -> bytes:
    """Any image as a JPEG at most 1600 px on its long edge, upright, without EXIF (so no location).
    Raises if it isn't an image or has more than MAX_PIXELS (checked from the header, before decoding)."""
    img = data if isinstance(data, Image.Image) else Image.open(io.BytesIO(data))
    if img.width * img.height > MAX_PIXELS:
        raise ValueError(f"image too large: {img.width}x{img.height}")
    img.draft("RGB", (PHOTO_EDGE, PHOTO_EDGE))  # a JPEG decodes straight at a smaller scale
    img.thumbnail((PHOTO_EDGE, PHOTO_EDGE))
    img = ImageOps.exif_transpose(img).convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def pdf_photo(pdf: bytes) -> bytes | None:
    """The largest raster image (200x200 up to MAX_PIXELS) on the first two pages, as a JPEG; None if there is none.
    Sizes come from the image dictionaries, so only the chosen image is ever decoded."""
    try:
        found = []  # (pixels, page, image name)
        for page in PdfReader(io.BytesIO(pdf)).pages[:2]:
            res = page["/Resources"] if "/Resources" in page else {}
            xobjs = res["/XObject"] if "/XObject" in res else {}
            for name in xobjs:
                try:
                    o = xobjs[name]
                    w, h = int(o["/Width"]), int(o["/Height"])
                except Exception:  # a form, or a broken entry
                    continue
                if o.get("/Subtype") == "/Image" and w >= 200 and h >= 200 and w * h <= MAX_PIXELS:
                    found.append((w * h, page, name))
        for _, page, name in sorted(found, key=lambda f: -f[0]):
            try:
                return to_jpeg(page.images[name].image)
            except Exception:  # one pypdf can't decode: try the next largest
                continue
    except Exception:
        log.warning("no photo from the PDF", exc_info=True)
    return None


def _resolve(host: str) -> list[str]:
    return [a[4][0] for a in socket.getaddrinfo(host, None)]


def _public(url: httpx.URL) -> bool:
    """True when every address the host resolves to is on the public internet (not the LAN, a tailnet, or this box)."""
    if url.scheme not in ("http", "https") or not url.host:
        return False
    try:
        addrs = _resolve(url.host)
        return bool(addrs) and all(ipaddress.ip_address(a).is_global for a in addrs)
    except (OSError, ValueError):
        return False


def fetch_photo(http, url: str | None) -> bytes | None:
    """Download an image as a JPEG; any failure is just no photo. The URL comes from someone else's page, so: public
    hosts only (checked again on each of at most 3 redirects), image/* only, 5 MB and 15 s at most.
    ponytail: the check and httpx resolve the name separately; pin the address if DNS rebinding ever matters here."""
    if not url:
        return None
    deadline = time.monotonic() + PHOTO_SECONDS
    try:
        target = httpx.URL(url)
        for _ in range(4):  # the URL and up to 3 redirects
            if not _public(target):
                return None
            with http.stream("GET", target, headers={"User-Agent": UA}, follow_redirects=False,
                             timeout=PHOTO_SECONDS) as r:
                if r.is_redirect:
                    target = target.join(r.headers["location"])
                    continue
                if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
                    return None
                data = bytearray()
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > PHOTO_MAX or time.monotonic() > deadline:
                        return None
                return to_jpeg(bytes(data))
        return None
    except Exception:
        log.warning("no photo from %s", url, exc_info=True)
        return None


class Pending(BaseModel):
    """status: processing (being read now), reading (in a Message Batch, batch_id), waiting (free mode: read after
    the 1st), ready, failed."""
    id: str
    status: Literal["processing", "reading", "waiting", "ready", "failed"]
    source: str
    draft: RecipeDraft | None = None
    error: str | None = None
    batch_id: str | None = None
    tableless: bool = False  # read without Claude from the page's JSON-LD: may be saved without a table
    ai_picks: dict[str, int | None] = {}  # Claude's product per ingredient name, so a reload doesn't ask again
    estimates: dict[str, float] = {}  # "{product_id}:{unit}" -> Claude's stock units per 1 unit, asked once
    doubts: list[dict] = []  # the accuracy check's Doubts (ref, reason), shown on the review until Save


_ID = re.compile(r"[0-9a-f]{32}")


class PendingStore:
    """One `<id>.json` per unreviewed import, plus `<id>.pdf` when the source was a PDF and `<id>.jpg` when a dish
    photo was found."""

    def __init__(self, dir: Path):
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _file(self, id: str, ext: str) -> Path | None:
        return self.dir / f"{id}{ext}" if _ID.fullmatch(id) else None  # ids come from URLs: no path tricks

    def new(self, source: str, pdf: bytes | None = None) -> Pending:
        p = Pending(id=uuid.uuid4().hex, status="processing", source=source)
        if pdf:
            self._file(p.id, ".pdf").write_bytes(pdf)
        self.save(p, new=True)
        return p

    def save(self, p: Pending, new: bool = False) -> None:
        """Writes the import back; an import discarded meanwhile (its .json gone) stays discarded.
        ponytail: check-then-write, so a discard in the same millisecond can still lose; a lock if that ever bites."""
        f = self._file(p.id, ".json")
        if not new and not f.exists():
            return
        tmp = f.with_suffix(".tmp")
        tmp.write_text(p.model_dump_json(), encoding="utf-8")
        tmp.replace(f)  # atomic: a reader never sees half a file

    def get(self, id: str) -> Pending | None:
        f = self._file(id, ".json")
        return Pending.model_validate_json(f.read_text(encoding="utf-8")) if f and f.exists() else None

    def pdf_path(self, id: str) -> Path | None:
        f = self._file(id, ".pdf")
        return f if f and f.exists() else None

    def photo_path(self, id: str) -> Path | None:
        f = self._file(id, ".jpg")
        return f if f and f.exists() else None

    def save_photo(self, id: str, data: bytes) -> None:
        if self._file(id, ".json").exists():  # not for an import discarded meanwhile
            self._file(id, ".jpg").write_bytes(data)

    def all(self) -> list[Pending]:
        out = []
        for f in self.dir.glob("*.json"):
            try:  # one discarded between the glob and the read is just gone
                out.append((f.stat().st_mtime, Pending.model_validate_json(f.read_text(encoding="utf-8"))))
            except FileNotFoundError:
                pass
        return [p for _, p in sorted(out, key=lambda x: x[0])]

    def delete(self, id: str) -> None:
        for ext in (".json", ".pdf", ".jpg"):
            f = self._file(id, ext)
            if f:
                f.unlink(missing_ok=True)


PAGE_CAP = 20_000  # economy: characters of a page without JSON-LD sent to Claude


def recipe_part(text: str) -> str:
    """A page's visible text from a little before its first "ingredient" line, capped at PAGE_CAP characters.
    ponytail: a keyword heuristic; a page that never says "ingredient" is just capped."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if "ingredient" in line.lower()), 0)
    return "\n".join(lines[max(0, start - 10):])[:PAGE_CAP]


def _recipe_text(ai, html: str) -> str:
    """What Claude reads from a web page: its JSON-LD recipe, else its visible text (trimmed in economy mode)."""
    text = extract_jsonld_recipe(html)
    if text is None:
        text = page_text(html)
        if ai.mode() == "economy":
            text = recipe_part(text)
    return text


def _get(http, url: str) -> httpx.Response:
    r = http.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=20)
    r.raise_for_status()
    return r


MISFIT = "The check suggested changes that didn't fit; compare with the card."


def apply_check(table: CfeTable, check: TableCheck) -> tuple[CfeTable, list[Doubt]]:
    """The checked table and its doubts. The correction is taken only when it keeps every ingredient row and step
    where it was (product picks go by row) and still validates; otherwise the table stays as it was and the check's
    doubts are kept for the table as a whole, since their rows may not line up. Doubts about unknown rows are dropped."""
    new, names = check.table, [g.name for g in table.ingredients]
    moved = any(g.name != old and g.name in names for g, old in zip(new.ingredients, names))  # rows swapped
    if (len(new.ingredients) != len(names) or moved or [o.id for o in new.ops] != [o.id for o in table.ops]
            or validate(new)):
        return table, [Doubt(ref="", reason=MISFIT), *(Doubt(ref="", reason=d.reason) for d in check.doubts)]
    refs = {"", *(f"i{n}" for n in range(len(names))), *(o.id for o in new.ops)}
    renamed = [Doubt(ref=f"i{n}", reason=f"Renamed from {old}; compare with the card.")
               for n, (g, old) in enumerate(zip(new.ingredients, names)) if g.name != old]
    return new, [d for d in check.doubts if d.ref in refs] + renamed


def renumber(table: CfeTable, doubts: list[Doubt]) -> tuple[CfeTable, list[Doubt]]:
    """Claude's step ids made o1, o2, ... in order, as the table editor numbers them (web._table_from_form), so a
    doubt about a step still names it after the review form posts the steps back. Inputs that name no step (an
    invalid table) are left as they are, and no new id takes their name."""
    ids = {o.id for o in table.ops}
    stray = {r for o in table.ops for r in o.inputs if r not in ids}
    fresh = (f"o{n}" for n in itertools.count(1) if f"o{n}" not in stray)
    new = {o.id: next(fresh) for o in table.ops}
    ops = [o.model_copy(update={"id": new[o.id], "inputs": [new.get(r, r) for r in o.inputs]}) for o in table.ops]
    return (table.model_copy(update={"ops": ops}),
            [d.model_copy(update={"ref": new.get(d.ref, d.ref)}) for d in doubts])


def check(ai, table: CfeTable, servings: int | None, **source) -> tuple[CfeTable, list[Doubt]]:
    """The accuracy check of a table Claude just made, against the same source (text=, pdf=), with its steps
    renumbered. Claude failing or paused leaves the table unchecked, with no doubts."""
    try:
        got = apply_check(table, ai.check_table(table, servings, **source))
    except AIPaused:
        log.info("AI is paused; the table is kept unchecked")
        got = table, []
    except AIError:
        log.warning("could not check the table; keeping it unchecked", exc_info=True)
        got = table, []
    return renumber(*got)


def _structure(ai, http, url: str | None,
               pdf: bytes | None) -> tuple[RecipeDraft, bytes | None, bool, list[Doubt]]:
    """The checked draft, the dish photo (None when there is none), whether it was read without Claude (no table)
    and the check's doubts."""
    if pdf:
        draft = ai.structure_recipe(pdf=pdf)
        draft.table, doubts = check(ai, draft.table, draft.servings, pdf=pdf)
        return draft, pdf_photo(pdf), False, doubts
    if not url:
        raise ValueError("nothing to import: no URL or PDF")
    r = _get(http, url)
    tableless, doubts = False, []
    try:
        text = _recipe_text(ai, r.text)
        draft = ai.structure_recipe(text=text)
    except AIPaused:
        node = _jsonld(r.text)
        if node is None:
            raise  # nothing to go on without Claude: wait for the 1st
        draft, tableless = draft_from_jsonld(node), True
    else:
        draft.table, doubts = check(ai, draft.table, draft.servings, text=text)
    return draft, fetch_photo(http, photo_url(r.text, str(r.url))), tableless, doubts


def make_table(ai, http, text: str, url: str | None = None,
               pdf: bytes | None = None) -> tuple[CfeTable, bool, list[Doubt]]:
    """"Make the table" for a saved recipe: read its kept PDF, else its web page, else `text` (its saved ingredients
    and steps), then check it against the same. Returns the table, whether `text` stood in for a page that couldn't be
    fetched, and the check's doubts. Raises AIError (AIPaused in free mode), or ValueError when Claude's table doesn't
    check out."""
    fell_back = False
    if pdf:
        draft = ai.structure_recipe(pdf=pdf)
        table, doubts = check(ai, draft.table, draft.servings, pdf=pdf)
    else:
        if url:
            try:
                text = _recipe_text(ai, _get(http, url).text)
            except httpx.HTTPError:
                log.warning("could not fetch %s; using the saved recipe", url, exc_info=True)
                fell_back = True
        draft = ai.structure_recipe(text=text)
        table, doubts = check(ai, draft.table, draft.servings, text=text)
    if validate(table):
        raise ValueError("the table Claude made doesn't check out; try again, or make it by hand with Edit table")
    return table, fell_back, doubts


def process(store: PendingStore, ai, http, pending_id: str, url: str | None, pdf: bytes | None) -> None:
    """Background job: fill a pending import's draft, or mark it failed. Never raises."""
    try:
        p = store.get(pending_id)
        if p is None:
            return  # deleted while queued
        try:
            _ready(store, p, *_structure(ai, http, url, pdf))
        except AIPaused:
            p.status, p.error = "waiting", None
        except Exception as e:  # AIError, HTTP, parse: all shown to the reviewer
            p.status, p.error = "failed", str(e) or type(e).__name__
        store.save(p)
    except Exception:
        log.exception("could not record result of import %s", pending_id)


def _ready(store: PendingStore, p: Pending, draft: RecipeDraft, photo: bytes | None, tableless: bool = False,
           doubts: list[Doubt] = ()) -> None:
    if photo:
        store.save_photo(p.id, photo)
    if draft.servings is None:
        draft.servings = 2
    p.status, p.draft, p.error, p.batch_id, p.tableless = "ready", draft, None, None, tableless
    p.doubts = [d.model_dump() for d in doubts]


BATCH_MIN = 3  # economy: this many PDFs imported at once go through one Message Batch


def submit_batch(store: PendingStore, ai, pending: list[Pending]) -> bool:
    """Economy: send these PDF imports as one Message Batch and mark them reading; False (nothing changed) when
    Claude is unavailable, so the caller reads them one at a time."""
    try:
        bid = ai.submit_recipe_batch({p.id: store.pdf_path(p.id).read_bytes() for p in pending})
    except AIError:
        log.warning("batch import not accepted; reading one at a time", exc_info=True)
        return False
    for p in pending:
        p.status, p.batch_id = "reading", bid
        store.save(p)
    return True


def tick(store: PendingStore, ai, http, grocy=None, tried: dict[str, float] | None = None) -> None:
    """Background, every minute: collect finished batch reads into their pending imports, and once AI is back
    (the 1st), read the imports that waited for it and fill in the amounts saved without one (given `grocy`)."""
    if grocy is not None:
        try:  # its own failure never stalls the imports
            fill_estimates(grocy, ai, {} if tried is None else tried)
        except Exception:
            log.exception("could not fill in estimated amounts")
    batches: dict[str, list[str]] = {bid: [] for bid in ai.open_batches()}  # unmetered, even with no imports left
    waiting: list[Pending] = []
    for p in store.all():
        if p.status == "reading" and p.batch_id:
            batches.setdefault(p.batch_id, []).append(p.id)
        elif p.status == "waiting":
            waiting.append(p)
    if waiting and ai.mode() != "free":
        for p in waiting:
            pdf = store.pdf_path(p.id)
            p.status = "processing"
            store.save(p)
            process(store, ai, http, p.id, None if pdf else p.source, pdf.read_bytes() if pdf else None)
    for bid, pids in batches.items():
        try:  # one batch at a time: a bad one never stalls the rest
            _collect(store, ai, bid, pids)
        except Exception:
            log.exception("could not collect batch %s", bid)


def _collect(store: PendingStore, ai, bid: str, pids: list[str]) -> None:
    results = ai.recipe_batch_results(bid)
    if results is None:
        return  # still running
    for pid in pids:
        p = store.get(pid)
        if p is None or p.status != "reading":
            continue  # discarded meanwhile, or already collected
        got = results.get(pid, "Claude returned no result")
        if isinstance(got, str):
            p.status, p.error, p.batch_id = "failed", got, None
        else:
            f = store.pdf_path(pid)
            pdf = f.read_bytes() if f else None
            doubts = []
            if pdf:  # checked by a normal call, now its result is here
                got.table, doubts = check(ai, got.table, got.servings, pdf=pdf)
            _ready(store, p, got, pdf_photo(pdf) if pdf else None, doubts=doubts)
        store.save(p)


def start_worker(store: PendingStore, ai, http, grocy=None, interval: int = 60, setup=None) -> threading.Thread:
    """Daemon thread running tick() every `interval` seconds; failures are logged and it goes on. `setup` (the
    start's Grocy bootstrap, when it failed) is tried before each tick until it succeeds once."""
    tried: dict[str, float] = {}  # failed estimates, kept for this run of the app

    def run():
        todo = setup
        while True:
            if todo is not None:
                try:
                    todo()
                    todo = None
                except Exception:
                    log.warning("could not set up Grocy; trying again", exc_info=True)
            try:
                tick(store, ai, http, grocy, tried)
            except Exception:
                log.exception("import tick failed")
            time.sleep(interval)

    t = threading.Thread(target=run, name="imports", daemon=True)
    t.start()
    return t


def _words(name: str) -> set[str]:
    """Lowercase words, plurals made singular: 'Ripe Tomatoes' -> {'ripe', 'tomato'}."""
    out = set()
    for w in re.findall(r"[a-z]+", name.lower()):
        out.add(w[:-2] if w.endswith("oes") else w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 3
                else w)
    return out


def plain_match(name: str, generics: dict[int, dict]) -> int | None:
    """Free mode: the generic whose words all appear in the ingredient name, the most specific (most words) first."""
    words = _words(name)
    hits = [(len(gw), pid) for pid, p in generics.items() if (gw := _words(p["name"])) and gw <= words]
    return max(hits)[1] if hits else None


@dataclass
class IngredientMatch:
    index: int
    product_id: int | None
    stock_amount: float | None


def _unit(text: str) -> str:
    return normalize_unit(text.strip().rstrip("."))


def _stock_amount(product: dict, qty: float | None, unit: str, convs: list[dict], names: dict[int, str]) -> float | None:
    stock = names.get(int(product.get("qu_id_stock") or 0))
    if qty is None or stock is None:
        return None
    pid = int(product["id"])
    pairs = [(names[int(c["from_qu_id"])], names[int(c["to_qu_id"])], float(c["factor"])) for c in convs
             if c.get("product_id") in (None, "") or int(c["product_id"]) == pid]
    return to_stock_amount(qty, _unit(unit), stock, pairs)


def propose_matches(grocy, ai, draft: RecipeDraft, asked: dict[str, int | None] | None = None) -> list[IngredientMatch]:
    """Exact-ish name matches against generic products first, Claude for the rest; one match per ingredient.
    `asked` remembers Claude's answers by ingredient name (filled in here): a name in it isn't asked again."""
    asked = {} if asked is None else asked
    generics = {int(p["id"]): p for p in _generics(grocy)}
    by_name = {p["name"].lower(): pid for pid, p in generics.items()}
    ings = draft.table.ingredients
    picks: dict[int, int] = {}
    rest: list[int] = []
    for i, ing in enumerate(ings):
        hit = get_close_matches(ing.name.lower(), list(by_name), n=1, cutoff=0.85)
        if hit:
            picks[i] = by_name[hit[0]]
        elif ing.name in asked:
            if asked[ing.name] in generics:  # still there
                picks[i] = asked[ing.name]
        else:
            rest.append(i)
    if rest and generics:
        try:
            res = ai.match_ingredients([ings[i].name for i in rest],
                                       [{"id": pid, "name": p["name"]} for pid, p in generics.items()])
            for pk in res.items:
                if 0 <= pk.index < len(rest) and pk.product_id in generics:  # Claude can invent ids
                    picks.setdefault(rest[pk.index], pk.product_id)
            asked.update((ings[i].name, picks.get(i)) for i in rest)
        except AIPaused:
            for i in rest:
                if (pid := plain_match(ings[i].name, generics)) is not None:
                    picks[i] = pid
        except AIError:
            pass  # left unmatched; the reviewer picks by hand
    names = {v: k for k, v in ids(grocy).unit.items()}
    convs = grocy.objects("quantity_unit_conversions")
    return [IngredientMatch(i, picks.get(i), _stock_amount(generics[picks[i]], ing.qty, ing.unit, convs, names)
                            if i in picks else None) for i, ing in enumerate(ings)]


def stock_amount_for(grocy, product_id: int, qty: float | None, unit: str) -> float | None:
    """`qty unit` in the product's stock unit (as propose_matches computes it), or None if not convertible."""
    product = grocy.get_object("products", product_id)
    if product is None:
        return None
    names = {v: k for k, v in ids(grocy).unit.items()}
    return _stock_amount(product, qty, unit, grocy.objects("quantity_unit_conversions"), names)


def estimate_key(product_id: int, unit: str) -> str:
    return f"{product_id}:{_unit(unit)}"


def estimate_amount(grocy, ai, pending: Pending, product_id: int, qty: float | None, unit: str) -> float | None:
    """`qty unit` in the product's stock unit: a known conversion first, else Claude's estimate, cached on the
    pending import under "{product_id}:{unit}" (dropped again once a known conversion covers it, so Save stores only
    estimates it used). None with no amount (to taste), in free mode, or when Claude fails, and for a packaging unit:
    a packet ingredient carries its measured amount (`packets` apart), so "1 packet" is only an older import or a slip
    and is never guessed."""
    known = stock_amount_for(grocy, product_id, qty, unit)
    key = estimate_key(product_id, unit)
    if known is not None or qty is None or is_packaging(unit):
        pending.estimates.pop(key, None)
        return known
    if key not in pending.estimates:
        prod = grocy.get_object("products", product_id) or {}
        stock = {v: k for k, v in ids(grocy).unit.items()}.get(int(prod.get("qu_id_stock") or 0))
        if stock is None:
            return None
        try:
            factor = ai.guess_conversion(prod["name"], _unit(unit), stock).factor
        except AIError:  # AIPaused in free mode: saved without an amount
            return None
        if not (math.isfinite(factor) and factor > 0):
            return None
        pending.estimates[key] = factor
    return qty * pending.estimates[key]


def add_conversion(grocy, product_id: int, from_unit: str, factor: float) -> None:
    """Product-specific `from_unit` -> stock unit conversion (1 from_unit = factor stock units); upserts."""
    if is_packaging(from_unit):  # packets vary by brand and card; they must never become a stored conversion
        raise ValueError(f"'{from_unit}' is packaging, not a measurement; enter the real amount instead")
    if not factor > 0:  # 0 breaks to_stock_amount's 1/f reverse edge; negatives poison shopping sums
        raise ValueError(f"conversion factor must be positive, got {factor}")
    name = _unit(from_unit)
    qu = ids(grocy).unit.get(name) or grocy.create("quantity_units", {"name": name, "name_plural": name})
    to = int(grocy.get_object("products", product_id)["qu_id_stock"])
    old = next((c for c in grocy.objects("quantity_unit_conversions", product_id=product_id)
                if int(c["from_qu_id"]) == qu and int(c["to_qu_id"]) == to), None)
    if old:
        grocy.update("quantity_unit_conversions", int(old["id"]), {"factor": factor})
    else:
        grocy.create("quantity_unit_conversions",
                     {"from_qu_id": qu, "to_qu_id": to, "factor": factor, "product_id": product_id})


FILL_RETRY = 24 * 3600  # seconds before an estimate Claude couldn't give is asked again


def fill_estimates(grocy, ai, tried: dict[str, float], now: float | None = None) -> None:
    """Positions saved without a stock amount (amount 0, `variable_amount` "<qty> <unit>": see needs_estimate) get
    one once Claude is available: the amount is written, the marker cleared and the estimate stored as the product's
    conversion. `tried` remembers failures ("{product_id}:{unit}" -> when) so one isn't paid for again for a day."""
    if ai.mode() == "free":
        return
    now = time.monotonic() if now is None else now
    cache = Pending(id="0" * 32, status="ready", source="")  # one ask per product and unit in this run
    for pos in grocy.objects("recipes_pos"):
        q, _, unit = str(pos.get("variable_amount") or "").partition(" ")
        qty = parse_qty(q)
        if float(pos.get("amount") or 0) or qty is None or is_packaging(unit):
            continue  # has an amount, to taste, or not ours; a stray packet stays as it is (see estimate_amount)
        pid = int(pos["product_id"])
        key = estimate_key(pid, unit)
        if key in tried and now - tried[key] < FILL_RETRY:
            continue
        try:
            amount = estimate_amount(grocy, ai, cache, pid, qty, unit)
            if amount is None:
                tried[key] = now
                continue
            if key in cache.estimates:
                add_conversion(grocy, pid, unit, cache.estimates.pop(key))  # the next row of it is then known
            grocy.update("recipes_pos", int(pos["id"]), {"amount": amount, "variable_amount": ""})
        except Exception:
            log.warning("could not fill in the amount of recipe position %s", pos.get("id"), exc_info=True)
            tried[key] = now


def needs_estimate(ing: Ingredient) -> dict:
    """A position saved without a stock amount has amount 0, so cooking and shopping skip it. Grocy's own
    `variable_amount` keeps the recipe's amount ("2 cup") for an estimate to fill in later; to taste stays unmarked."""
    return {"variable_amount": f"{ing.qty:g} {_unit(ing.unit)}"} if ing.qty is not None else {}


def put_photo(grocy, rid: int, jpeg: bytes) -> None:
    """Store a JPEG as the recipe's Grocy picture, under a fresh name each time so cached copies never go stale;
    the picture it replaces is then deleted (best effort: a leftover file harms nothing)."""
    while True:
        name = f"recipe-{rid}-{secrets.token_hex(4)}.jpg"
        if "/" not in base64.b64encode(name.encode()).decode():  # Grocy's URLs carry the name in base64
            break
    old = (grocy.get_object("recipes", rid) or {}).get("picture_file_name")
    grocy.put_recipe_picture(name, jpeg)
    grocy.update("recipes", rid, {"picture_file_name": name})
    if old:
        try:
            grocy.delete_recipe_picture(old)
        except Exception:
            log.warning("could not delete old picture %s", old, exc_info=True)


def save_recipe(grocy, draft: RecipeDraft, matches: list[IngredientMatch], source_url: str | None,
                pdf_path: Path | None, photo: Path | None = None, need_table: bool = True,
                doubts: list[dict] = ()) -> int:
    """Create the reviewed recipe in Grocy. pdf_path is a PendingStore PDF (DATA_DIR/pending/<id>.pdf);
    it is copied to its sibling DATA_DIR/sources/<recipe_id>.pdf and source_pdf = "sources/<recipe_id>.pdf".
    photo (a PendingStore JPEG) becomes the recipe's picture; if that fails the recipe is still saved.
    need_table=False (a free-mode import) saves a draft with no steps in its table as a recipe without a table.
    `doubts` (the check's, still open) are kept on the recipe until its table editor saves."""
    ings = draft.table.ingredients
    by_index = {m.index: m for m in matches}
    bad = [i for i in range(len(ings)) if i not in by_index or by_index[i].product_id is None]
    errs = ([f"Pick a product for {', '.join(f'i{i}' for i in bad)}"] if bad else []) \
        + (validate(draft.table) if need_table or draft.table.ops else [])
    if errs:
        raise ValueError("; ".join(errs))
    products = {}
    for m in by_index.values():  # check every product before writing anything
        prod = grocy.get_object("products", m.product_id) if m.product_id is not None else None
        if prod is None or not prod.get("qu_id_stock"):
            raise ValueError(f"product {m.product_id} no longer exists or has no stock unit; pick it again")
        products[m.product_id] = prod
    taken = {r["name"].lower() for r in grocy.objects("recipes")}
    name, n = draft.title, 2
    while name.lower() in taken:
        name, n = f"{draft.title} ({n})", n + 1
    steps = "".join(f"<li>{escape(s)}</li>" for s in draft.steps)
    rid = grocy.create("recipes", {"name": name, "base_servings": draft.servings or 2,
                                   "description": f"<ol>{steps}</ol>"})
    made: list[int] = []
    try:
        for i, ing in enumerate(ings):
            m = by_index[i]
            made.append(grocy.create("recipes_pos", {
                "recipe_id": rid, "product_id": m.product_id, "amount": m.stock_amount or 0,
                "qu_id": int(products[m.product_id]["qu_id_stock"]),
                "note": line_text(ing, 1),
                **(needs_estimate(ing) if m.stock_amount is None else {})}))
        uf = {"cfe_table": draft.table.model_dump_json() if draft.table.ops else "", "source_url": source_url or "",
              "source_pdf": "", "doubts": json.dumps(list(doubts)) if doubts else ""}
        if pdf_path:
            dest = Path(pdf_path).parent.parent / "sources" / f"{rid}.pdf"
            dest.parent.mkdir(exist_ok=True)
            shutil.copyfile(pdf_path, dest)
            uf["source_pdf"] = f"sources/{rid}.pdf"
        grocy.set_userfields("recipes", rid, uf)
    except Exception:  # undo, so a retry neither finds a half recipe nor saves "Title (2)"
        try:
            for pos in made:
                grocy.delete("recipes_pos", pos)
            grocy.delete("recipes", rid)
        except Exception:
            log.exception("could not roll back half-saved recipe %s", rid)
        raise
    if photo:
        try:
            put_photo(grocy, rid, Path(photo).read_bytes())
        except Exception:
            log.exception("could not store the photo for recipe %s", rid)
    return rid


def create_generic(grocy, ai, http, settings, name: str, details: dict | None = None) -> int:
    """New generic product for an unmatched ingredient, or the existing one. Claude fills the details; in free mode
    the reviewer does: `details` has generic, stock_unit, package_amount, location and category (create_manual's
    fields), and the generic it names is the one returned."""
    name = name.strip()
    generics = _generics(grocy)
    if details:
        generic = details["generic"].strip() or name
        hit = _generic_named(grocy, generic)
        if hit is None:
            r = create_manual(grocy, http, None, name, generic, details["stock_unit"], details["package_amount"],
                              details["location"], details["category"])
            hit = _generic_named(grocy, r.generic_name)
            fill_missing_nutrition(grocy, int(hit["id"]), settings.fdc_api_key, http)
        return int(hit["id"])
    hit = next((p for p in generics if p["name"].lower() == name.lower()), None)
    if hit:
        return int(hit["id"])
    g = ai.guess_product({"name": name}, generic_choices(grocy), list(places(grocy)))
    fields, uf = _product_fields(grocy, g)
    pid = grocy.create("products", {"name": name, **fields})
    grocy.set_userfields("products", pid, uf)
    fill_missing_nutrition(grocy, pid, settings.fdc_api_key, http)
    return pid
