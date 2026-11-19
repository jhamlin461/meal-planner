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

from app.ai import AIError, RecipeDraft
from app.barcode import _generics, _product_fields, generic_choices
from app.cfe import ingredient_text, validate
from app.nutrition import fill_missing_nutrition
from app.qty import is_packaging, normalize_unit, to_stock_amount
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


def extract_jsonld_recipe(html: str) -> str | None:
    for body in _Parser(html).ld:
        try:
            data = json.loads(body)
        except ValueError:
            continue
        for r in _recipes(data):
            lines = [_flat(r.get("name", ""))]
            if r.get("recipeYield"):
                lines.append(f"Yield: {_flat(r['recipeYield'])}")
            lines += ["", "Ingredients:"] + [f"- {i}" for i in r.get("recipeIngredient") or []]
            lines += ["", "Instructions:"] + [f"- {s}" for s in _steps(r.get("recipeInstructions"))]
            return "\n".join(lines)
    return None


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
    id: str
    status: Literal["processing", "ready", "failed"]
    source: str
    draft: RecipeDraft | None = None
    error: str | None = None


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


def _structure(ai, http, url: str | None, pdf: bytes | None) -> tuple[RecipeDraft, bytes | None]:
    """The draft and the dish photo (None when there is none)."""
    if pdf:
        return ai.structure_recipe(pdf=pdf), pdf_photo(pdf)
    if not url:
        raise ValueError("nothing to import: no URL or PDF")
    r = http.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=20)
    r.raise_for_status()
    draft = ai.structure_recipe(text=extract_jsonld_recipe(r.text) or page_text(r.text))
    return draft, fetch_photo(http, photo_url(r.text, str(r.url)))


def process(store: PendingStore, ai, http, pending_id: str, url: str | None, pdf: bytes | None) -> None:
    """Background job: fill a pending import's draft, or mark it failed. Never raises."""
    try:
        p = store.get(pending_id)
        if p is None:
            return  # deleted while queued
        try:
            draft, photo = _structure(ai, http, url, pdf)
            if photo:
                store.save_photo(p.id, photo)
            if draft.servings is None:
                draft.servings = 2
            p.status, p.draft, p.error = "ready", draft, None
        except Exception as e:  # AIError, HTTP, parse: all shown to the reviewer
            p.status, p.error = "failed", str(e) or type(e).__name__
        store.save(p)
    except Exception:
        log.exception("could not record result of import %s", pending_id)


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
                pdf_path: Path | None, photo: Path | None = None) -> int:
    """Create the reviewed recipe in Grocy. pdf_path is a PendingStore PDF (DATA_DIR/pending/<id>.pdf);
    it is copied to its sibling DATA_DIR/sources/<recipe_id>.pdf and source_pdf = "sources/<recipe_id>.pdf".
    photo (a PendingStore JPEG) becomes the recipe's picture; if that fails the recipe is still saved."""
    ings = draft.table.ingredients
    by_index = {m.index: m for m in matches}
    bad = [i for i in range(len(ings))
           if i not in by_index or by_index[i].product_id is None or by_index[i].stock_amount is None]
    errs = ([f"ingredients without a product or amount: {', '.join(map(str, bad))}"] if bad else []) \
        + validate(draft.table)
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
        uf = {"cfe_table": draft.table.model_dump_json(), "source_url": source_url or "", "source_pdf": ""}
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


def create_generic(grocy, ai, http, settings, name: str) -> int:
    """New generic product for an unmatched ingredient (Claude fills the details), or the existing one."""
    name = name.strip()
    generics = _generics(grocy)
    hit = next((p for p in generics if p["name"].lower() == name.lower()), None)
    if hit:
        return int(hit["id"])
    g = ai.guess_product({"name": name}, generic_choices(grocy), LOCATIONS)
    fields, uf = _product_fields(grocy, g)
    pid = grocy.create("products", {"name": name, **fields})
    grocy.set_userfields("products", pid, uf)
    fill_missing_nutrition(grocy, pid, settings.fdc_api_key, http)
    return pid
