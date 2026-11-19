import base64
import secrets
from datetime import date, datetime

import httpx


class GrocyError(Exception):
    pass


class Grocy:
    def __init__(self, base_url: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"GROCY-API-KEY": api_key},
            transport=transport,
            timeout=30,
        )

    def _req(self, method: str, path: str, ok_400: bool = False, **kw):
        r = self._http.request(method, path, **kw)
        if ok_400 and r.status_code == 400:
            return None
        if not r.is_success:
            raise GrocyError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    def objects(self, entity: str, **where) -> list[dict]:
        params = [("query[]", f"{k}={v}") for k, v in where.items()]
        return self._req("GET", f"/objects/{entity}", params=params)

    def get_object(self, entity: str, id: int) -> dict | None:
        r = self._http.get(f"/objects/{entity}/{id}")
        if r.status_code == 404:
            return None
        if not r.is_success:
            raise GrocyError(f"GET /objects/{entity}/{id} -> {r.status_code}")
        return r.json()

    def create(self, entity: str, data: dict) -> int:
        return int(self._req("POST", f"/objects/{entity}", json=data)["created_object_id"])

    def update(self, entity: str, id: int, data: dict) -> None:
        self._req("PUT", f"/objects/{entity}/{id}", json=data)

    def delete(self, entity: str, id: int) -> None:
        self._req("DELETE", f"/objects/{entity}/{id}")

    def users(self) -> list[dict]:
        return self._req("GET", "/users")

    def create_user(self, username: str, first_name: str) -> int:
        """Grocy returns no id for a new user, so find it again by username."""
        self._req("POST", "/users", json={"username": username, "first_name": first_name, "last_name": "",
                                          "password": secrets.token_urlsafe(24)})
        for u in self.users():
            if u["username"] == username:
                return int(u["id"])
        raise GrocyError("user not created")

    def userfields(self, entity: str, id: int) -> dict:
        return self._req("GET", f"/userfields/{entity}/{id}") or {}

    def set_userfields(self, entity: str, id: int, data: dict) -> None:
        self._req("PUT", f"/userfields/{entity}/{id}", json=data)

    @staticmethod
    def _picture(filename: str) -> str:
        return "/files/recipepictures/" + base64.b64encode(filename.encode()).decode()

    def put_recipe_picture(self, filename: str, data: bytes) -> None:
        self._req("PUT", self._picture(filename), content=data, headers={"Content-Type": "application/octet-stream"})

    def recipe_picture(self, filename: str, width: int) -> tuple[bytes, str] | None:
        """(bytes, content type) of the picture scaled to fit `width`, or None when Grocy has no such file."""
        r = self._http.get(self._picture(filename), params={"force_serve_as": "picture", "best_fit_width": width})
        if r.status_code == 404:
            return None
        if not r.is_success:
            raise GrocyError(f"GET recipe picture -> {r.status_code}")
        return r.content, r.headers.get("content-type", "image/jpeg")

    def delete_recipe_picture(self, filename: str) -> None:
        self._req("DELETE", self._picture(filename))

    def product_by_barcode(self, code: str) -> dict | None:
        return self._req("GET", f"/stock/products/by-barcode/{code}", ok_400=True)

    def stock_amounts(self) -> dict[int, float]:
        own = {int(r["product_id"]): float(r["amount"]) for r in self._req("GET", "/stock")}
        amounts = dict(own)
        for p in self.objects("products"):
            parent = p.get("parent_product_id")
            if parent and int(p["id"]) in own:
                amounts[int(parent)] = amounts.get(int(parent), 0.0) + own[int(p["id"])]
        return amounts

    def stock_entries(self) -> list[dict]:
        return self.objects("stock")

    def add_stock(self, product_id: int, amount: float, best_before: date | None, location_id: int | None) -> None:
        body: dict = {"amount": amount, "transaction_type": "purchase"}
        if best_before:
            body["best_before_date"] = best_before.isoformat()
        if location_id:
            body["location_id"] = location_id
        self._req("POST", f"/stock/products/{product_id}/add", json=body)

    def consume(self, product_id: int, amount: float) -> None:
        self._req("POST", f"/stock/products/{product_id}/consume",
                  json={"amount": amount, "spoiled": False, "allow_subproduct_substitution": True})

    def transfer(self, product_id: int, amount: float, from_location: int, to_location: int) -> None:
        self._req("POST", f"/stock/products/{product_id}/transfer",
                  json={"amount": amount, "location_id_from": from_location, "location_id_to": to_location})

    def set_amount(self, product_id: int, new_amount: float) -> None:
        self._req("POST", f"/stock/products/{product_id}/inventory", json={"new_amount": new_amount})

    def volatile(self, days: int) -> dict:
        return self._req("GET", "/stock/volatile", params={"due_soon_days": days})

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def chores_status(self) -> list[dict]:
        return self._req("GET", "/chores")

    def execute_chore(self, chore_id: int, done_by: int | None, skipped: bool = False) -> None:
        body: dict = {"tracked_time": self._now(), "skipped": skipped}
        if done_by is not None:
            body["done_by"] = done_by
        self._req("POST", f"/chores/{chore_id}/execute", json=body)

    def calculate_next_assignments(self) -> None:
        """Grocy sets a chore's next assignee only on execution; this sets it now (a new or edited chore)."""
        self._req("POST", "/chores/executions/calculate-next-assignments", json={})

    def open_tasks(self) -> list[dict]:
        return self._req("GET", "/tasks")

    def complete_task(self, task_id: int) -> None:
        self._req("POST", f"/tasks/{task_id}/complete", json={"done_time": self._now()})

    def batteries_status(self) -> list[dict]:
        return self._req("GET", "/batteries")

    def charge_battery(self, battery_id: int) -> None:
        self._req("POST", f"/batteries/{battery_id}/charge", json={"tracked_time": self._now()})
