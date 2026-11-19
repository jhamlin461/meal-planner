import html
import logging
import math
import re
from urllib.parse import unquote, urlencode, urlparse

import httpx

from app.grocy import GrocyError

log = logging.getLogger(__name__)


_ID_RE = re.compile(r"\d+")
_IP_PATH_RE = re.compile(r"/ip/(?:[^/]+/)?(\d+)/?$")
_CART = "https://www.walmart.com/sc/cart/addToCart"


def parse_item_id(url_or_id: str) -> str | None:
    s = url_or_id.strip()
    if _ID_RE.fullmatch(s):
        return s
    parsed = urlparse(s if "://" in s else f"https://{s}")
    host = (parsed.hostname or "").lower()
    if host != "walmart.com" and not host.endswith(".walmart.com"):
        return None
    m = _IP_PATH_RE.search(parsed.path)
    return m.group(1) if m else None


def packages_needed(shortfall: float, package_amount: float | None) -> int:
    if not package_amount:
        return 1
    return max(1, math.ceil(shortfall / package_amount))


def cart_url(items: list[tuple[str, int]], store_id: str | None) -> str:
    parts = ",".join(f"{item_id}_{qty}" if qty != 1 else item_id for item_id, qty in items)
    url = f"{_CART}?items={parts}"
    if store_id:
        url += f"&storeId={store_id}"
    return url


def search_url(name: str) -> str:
    return "https://www.walmart.com/search?" + urlencode({"q": name})


def slug_text(link: str) -> str:
    """The product words of an item URL's slug; empty for a bare item number."""
    m = re.search(r"/ip/([^/?#]+)/\d+", urlparse(link.strip() if "://" in link else f"https://{link.strip()}").path)
    return unquote(m.group(1)).replace("-", " ") if m else ""


def page_title(http, item_id: str) -> str | None:
    """The item page's <title>, or None on any failure or bot challenge. Sends nothing about the household."""
    from app.importer import UA  # lazy: importer's users import this module
    try:
        with http.stream("GET", f"https://www.walmart.com/ip/{item_id}", headers={"User-Agent": UA}, timeout=5,
                         follow_redirects=False) as r:  # a redirect is a wall too: never leave walmart.com
            body = b""
            if r.status_code == 200:
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > 200_000:  # the title is at the top
                        break
        m = re.search(r"<title[^>]*>(.*?)</title>", body[:200_000].decode("utf-8", "replace"), re.S | re.I)
    except Exception:  # never raises: the slug alone is enough
        return None
    t = html.unescape(m.group(1)).strip() if m else ""
    return None if not t or re.search(r"robot or human|captcha|access denied|verify", t, re.I) else t


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def refine(grocy, ai, http, pid: int, link: str, item_id: str) -> None:
    """Fill blank package size and measure from the linked listing, and replace Claude's estimates; the owner's and
    a scan's values stay. Silent on any failure: the link is already saved."""
    from app.ai import AIError  # lazy: these import modules that import this one
    from app.measure import fixed, stock_unit
    try:
        if ai.mode() == "free":
            return
        text = " - ".join(t for t in (slug_text(link), page_title(http, item_id)) if t)
        if not text:
            return
        p = grocy.get_object("products", pid)
        uf, stock = grocy.userfields("products", pid), stock_unit(grocy, p)
        r = ai.read_walmart(p["name"], stock, text)
        new = {}
        if r.package_amount and r.package_amount > 0 and (not uf.get("package_amount") or uf.get("package_source") != "owner"):
            new |= {"package_amount": r.package_amount, "package_source": "link"}
        if r.grams_per_piece and r.grams_per_piece > 0 and stock == "piece" and (
                not uf.get("g_per_unit") or uf.get("package_source") != "owner"):
            new["g_per_unit"] = r.grams_per_piece
        m = fixed(uf.get("measure"), r.measure_factor, stock) if uf.get("measure") else None
        # fixed() keeps a kitchen measure's own constant: that is not the link's value, so no "link" stamp
        if m and uf.get("measure_source") != "owner" and m.factor == float(r.measure_factor) and m.factor != _num(
                uf.get("measure_factor")):
            new |= {"measure_factor": m.factor, "measure_source": "link"}
        if new:
            grocy.set_userfields("products", pid, new)
    except (AIError, GrocyError, httpx.HTTPError):
        log.warning("could not refine product %s from its Walmart link", pid, exc_info=True)
