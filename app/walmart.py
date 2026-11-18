import math
import re
from urllib.parse import urlencode, urlparse

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
