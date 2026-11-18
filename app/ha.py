import httpx

SEND_TIMEOUT = 5  # notify / clear run under the reminder LOCK, so a hung HA can't hold it for long


class HAError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HA's HTTP status; None when HA wasn't reached


class HA:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=15,
        )

    def _req(self, method: str, path: str, **kw):
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise HAError(f"{method} {path} failed: {e}") from e
        if not r.is_success:
            raise HAError(f"{method} {path} -> {r.status_code}: {r.text[:200]}", r.status_code)
        try:
            return r.json() if r.content else None
        except ValueError as e:
            raise HAError(f"{method} {path}: non-JSON reply") from e

    def _persons(self, **kw) -> list[dict]:
        return [s for s in self._req("GET", "/api/states", **kw) if s["entity_id"].startswith("person.")]

    def persons(self) -> dict[str, str]:
        return {s["entity_id"]: s.get("attributes", {}).get("friendly_name", s["entity_id"]) for s in self._persons()}

    def person_trackers(self) -> dict[str, list[str]]:
        return {s["entity_id"]: list(s.get("attributes", {}).get("device_trackers") or []) for s in self._persons()}

    def home_states(self, timeout: float | None = None) -> dict[str, str]:
        """timeout: seconds for this call (a page that can't wait); the client's 15 s otherwise."""
        return {s["entity_id"]: s["state"] for s in self._persons(**({"timeout": timeout} if timeout else {}))}

    def notify_services(self) -> list[str]:
        return [n for d in self._req("GET", "/api/services") if d["domain"] == "notify"
                for n in d["services"] if n.startswith("mobile_app_")]

    def notify(self, service: str, title: str, message: str, tag: str, actions: list[tuple[str, str]]) -> None:
        self._req("POST", f"/api/services/notify/{service}", json={
            "title": title, "message": message,
            "data": {"tag": tag, "actions": [{"action": a, "title": t} for a, t in actions],
                     "channel": "House reminders", "ttl": 0, "priority": "high"},
        }, timeout=SEND_TIMEOUT)

    def clear(self, service: str, tag: str) -> None:
        self._req("POST", f"/api/services/notify/{service}", json={"message": "clear_notification", "data": {"tag": tag}},
                  timeout=SEND_TIMEOUT)
