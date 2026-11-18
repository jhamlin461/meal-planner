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
