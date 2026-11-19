"""Recipe import: fetch/parse sources, queue drafts for human review, save reviewed recipes to Grocy."""
import base64
import io
import ipaddress
import json
import logging
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

from app.ai import AIError, AIPaused, RecipeDraft
from app.barcode import _generic_named, _generics, _product_fields, create_manual, generic_choices
from app.cfe import CfeTable, Ingredient, ingredient_text, validate
from app.nutrition import fill_missing_nutrition
from app.qty import is_packaging, normalize_unit, parse_qty, to_stock_amount
from app.setup_grocy import LOCATIONS, ids

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
        self.save(p)
        return p

    def save(self, p: Pending) -> None:
        f = self._file(p.id, ".json")
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
        self._file(id, ".jpg").write_bytes(data)

    def all(self) -> list[Pending]:
        files = sorted(self.dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
        return [Pending.model_validate_json(f.read_text(encoding="utf-8")) for f in files]

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


def _structure(ai, http, url: str | None, pdf: bytes | None) -> tuple[RecipeDraft, bytes | None, bool]:
    """The draft, the dish photo (None when there is none) and whether it was read without Claude (no table)."""
    if pdf:
        return ai.structure_recipe(pdf=pdf), pdf_photo(pdf), False
    if not url:
        raise ValueError("nothing to import: no URL or PDF")
    r = _get(http, url)
    tableless = False
    try:
        draft = ai.structure_recipe(text=_recipe_text(ai, r.text))
    except AIPaused:
        node = _jsonld(r.text)
        if node is None:
            raise  # nothing to go on without Claude: wait for the 1st
        draft, tableless = draft_from_jsonld(node), True
    return draft, fetch_photo(http, photo_url(r.text, str(r.url))), tableless


def make_table(ai, http, text: str, url: str | None = None, pdf: bytes | None = None) -> tuple[CfeTable, bool]:
    """"Make the table" for a saved recipe: read its kept PDF, else its web page, else `text` (its saved ingredients
    and steps). Returns the table and whether `text` stood in for a page that couldn't be fetched. Raises AIError
    (AIPaused in free mode), or ValueError when Claude's table doesn't check out."""
    fell_back = False
    if pdf:
        draft = ai.structure_recipe(pdf=pdf)
    else:
        if url:
            try:
                text = _recipe_text(ai, _get(http, url).text)
            except httpx.HTTPError:
                log.warning("could not fetch %s; using the saved recipe", url, exc_info=True)
                fell_back = True
        draft = ai.structure_recipe(text=text)
    if validate(draft.table):
        raise ValueError("the table Claude made doesn't check out; try again, or make it by hand with Edit table")
    return draft.table, fell_back


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


def _ready(store: PendingStore, p: Pending, draft: RecipeDraft, photo: bytes | None, tableless: bool = False) -> None:
    if photo:
        store.save_photo(p.id, photo)
    if draft.servings is None:
        draft.servings = 2
    p.status, p.draft, p.error, p.batch_id, p.tableless = "ready", draft, None, None, tableless


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


def tick(store: PendingStore, ai, http) -> None:
    """Background, every minute: collect finished batch reads into their pending imports, and once AI is back
    (the 1st), read the imports that waited for it."""
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
            pdf = store.pdf_path(pid)
            _ready(store, p, got, pdf_photo(pdf.read_bytes()) if pdf else None)
        store.save(p)


def start_worker(store: PendingStore, ai, http, interval: int = 60) -> threading.Thread:
    """Daemon thread running tick() every `interval` seconds; failures are logged and it goes on."""
    def run():
        while True:
            try:
                tick(store, ai, http)
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


def propose_matches(grocy, ai, draft: RecipeDraft) -> list[IngredientMatch]:
    """Exact-ish name matches against generic products first, Claude for the rest; one match per ingredient."""
    generics = {int(p["id"]): p for p in _generics(grocy)}
    by_name = {p["name"].lower(): pid for pid, p in generics.items()}
    ings = draft.table.ingredients
    picks: dict[int, int] = {}
    rest: list[int] = []
    for i, ing in enumerate(ings):
        hit = get_close_matches(ing.name.lower(), list(by_name), n=1, cutoff=0.85)
        if hit:
            picks[i] = by_name[hit[0]]
        else:
            rest.append(i)
    if rest and generics:
        try:
            res = ai.match_ingredients([ings[i].name for i in rest],
                                       [{"id": pid, "name": p["name"]} for pid, p in generics.items()])
            for pk in res.items:
                if 0 <= pk.index < len(rest) and pk.product_id in generics:  # Claude can invent ids
                    picks.setdefault(rest[pk.index], pk.product_id)
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
                pdf_path: Path | None, photo: Path | None = None, need_table: bool = True) -> int:
    """Create the reviewed recipe in Grocy. pdf_path is a PendingStore PDF (DATA_DIR/pending/<id>.pdf);
    it is copied to its sibling DATA_DIR/sources/<recipe_id>.pdf and source_pdf = "sources/<recipe_id>.pdf".
    photo (a PendingStore JPEG) becomes the recipe's picture; if that fails the recipe is still saved.
    need_table=False (a free-mode import) saves a draft with no steps in its table as a recipe without a table."""
    ings = draft.table.ingredients
    by_index = {m.index: m for m in matches}
    bad = [i for i in range(len(ings))
           if i not in by_index or by_index[i].product_id is None or by_index[i].stock_amount is None]
    errs = ([f"ingredients without a product or amount: {', '.join(map(str, bad))}"] if bad else []) \
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
                "recipe_id": rid, "product_id": m.product_id, "amount": m.stock_amount,
                "qu_id": int(products[m.product_id]["qu_id_stock"]),
                "note": ingredient_text(ing, 1) + (f", {ing.note}" if ing.note else "")}))
        uf = {"cfe_table": draft.table.model_dump_json() if draft.table.ops else "", "source_url": source_url or "",
              "source_pdf": ""}
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
    g = ai.guess_product({"name": name}, generic_choices(grocy), LOCATIONS)
    fields, uf = _product_fields(grocy, g)
    pid = grocy.create("products", {"name": name, **fields})
    grocy.set_userfields("products", pid, uf)
    fill_missing_nutrition(grocy, pid, settings.fdc_api_key, http)
    return pid
