import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    grocy_url: str
    grocy_api_key: str
    anthropic_api_key: str
    fdc_api_key: str
    walmart_store_id: str | None
    shopping_weekday: int
    data_dir: Path
    expiry_warn_days: int
    ha_url: str = ""
    ha_token: str = ""
    hook_secret: str = ""
    quiet_hours: tuple[time, time] = (time(22), time(7))
    ai_monthly_budget: float = 100  # dollars of Claude a calendar month; at this the app stops calling Claude
    ai_economy_at: float = 80  # from here to the budget, cheaper settings


def _quiet(v: str) -> tuple[time, time]:
    a, b = v.split("-")
    return time.fromisoformat(a), time.fromisoformat(b)


def load_settings() -> Settings:
    e = os.environ.get
    return Settings(
        grocy_url=e("GROCY_URL", ""),
        grocy_api_key=e("GROCY_API_KEY", ""),
        anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
        fdc_api_key=e("FDC_API_KEY", "DEMO_KEY"),
        walmart_store_id=e("WALMART_STORE_ID") or None,
        shopping_weekday=int(e("SHOPPING_WEEKDAY", "6")),
        data_dir=Path(e("DATA_DIR", "/data")),
        expiry_warn_days=int(e("EXPIRY_WARN_DAYS", "3")),
        ha_url=e("HA_URL", ""),
        ha_token=e("HA_TOKEN", ""),
        hook_secret=e("HOOK_SECRET", ""),
        quiet_hours=_quiet(e("QUIET_HOURS") or "22:00-07:00"),
        ai_monthly_budget=float(e("AI_MONTHLY_BUDGET") or 100),
        ai_economy_at=float(e("AI_ECONOMY_AT") or 80),
    )
