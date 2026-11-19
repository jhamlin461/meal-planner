from app.walmart import cart_url, packages_needed, parse_item_id, search_url


def test_parse_item_id():
    assert parse_item_id("https://www.walmart.com/ip/Great-Value-All-Purpose-Flour-5-lb/10535106?athbdg=L1600") == "10535106"
    assert parse_item_id("10535106") == "10535106" and parse_item_id("https://example.com/x") is None


def test_cart_url_matches_walmart_docs():
    assert cart_url([("945193065", 1), ("660768274", 2)], None) == "https://www.walmart.com/sc/cart/addToCart?items=945193065,660768274_2"
    assert cart_url([("938038697", 3)], "5435") == "https://www.walmart.com/sc/cart/addToCart?items=938038697_3&storeId=5435"


def test_packages_needed():
    assert packages_needed(300, 2268) == 1 and packages_needed(2500, 2268) == 2 and packages_needed(0.2, None) == 1


def test_search_url_encodes_name():
    assert search_url("all purpose flour") == "https://www.walmart.com/search?q=all+purpose+flour"


def test_slug_text():
    from app.walmart import slug_text
    assert slug_text("https://www.walmart.com/ip/Great-Value-Long-Grain-White-Rice-32-oz/10450114?x=1") == \
        "Great Value Long Grain White Rice 32 oz"
    assert slug_text("10450114") == "" and slug_text("https://www.walmart.com/ip/10450114") == ""


def _title(handler):
    import httpx
    from app.walmart import page_title
    return page_title(httpx.Client(transport=httpx.MockTransport(handler)), "1")


def test_page_title():
    import httpx
    ok = lambda r: httpx.Response(200, text="<html><title> Rice &amp; Beans </title></html>")
    assert _title(ok) == "Rice & Beans"
    assert _title(lambda r: httpx.Response(200, text="<title>Robot or human?</title>")) is None
    assert _title(lambda r: httpx.Response(404, text="<title>Nope</title>")) is None
    assert _title(lambda r: httpx.Response(302, headers={"location": "https://evil.example/"})) is None

    def boom(r):
        raise httpx.ConnectError("down")
    assert _title(boom) is None
    big = "x" * 300_000 + "<title>late</title>"
    assert _title(lambda r: httpx.Response(200, text=big)) is None
