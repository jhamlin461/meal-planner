"""Build app/static/icons.svg, a sprite of Material Design Icons (stdlib only). Run: python scripts/build_icons.py"""
import re
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons.svg"
URL = "https://cdn.jsdelivr.net/npm/@mdi/svg@7.4.47/svg/{}.svg"
ICONS = """white-balance-sunny silverware-fork-knife home-outline barcode-scan washing-machine broom cat trash-can-outline
calendar-check battery-sync-outline basket-outline fridge-outline book-open-variant cart-outline account-outline
check alarm-snooze plus pencil-outline chevron-right alert-circle-outline""".split()


def symbol(name: str, svg: str) -> str:
    view = re.search(r'viewBox="([^"]+)"', svg).group(1)
    inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1).strip()
    return f'<symbol id="{name}" viewBox="{view}">{inner}</symbol>'


def main() -> None:
    parts = []
    for name in ICONS:
        with urllib.request.urlopen(URL.format(name), timeout=20) as r:
            parts.append(symbol(name, r.read().decode()))
    OUT.write_text('<svg xmlns="http://www.w3.org/2000/svg">\n' + "\n".join(parts) + "\n</svg>\n", newline="\n")
    print(f"wrote {len(parts)} icons to {OUT}")


if __name__ == "__main__":
    main()
