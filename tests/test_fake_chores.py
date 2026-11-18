from datetime import date

from tests.fakes import FakeGrocy


def _g():
    g = FakeGrocy()
    g.today = date(2026, 11, 18)
    return g


def test_fake_chore_consumes_supply():
    g = _g()
    g.add_stock(5, 4, None, None)
    c = g.create("chores", {"name": "Trash", "period_type": "daily", "period_interval": 1,
                            "consume_product_on_chore_execution": 1, "product_id": 5, "product_amount": 1})
    g.execute_chore(c, None)
    assert g.stock_amounts()[5] == 3
    g.execute_chore(c, None, skipped=True)
    assert g.stock_amounts()[5] == 3
    assert g.chores_status()[0]["next_estimated_execution_time"].startswith("2026-11-19")


def test_fake_chore_with_no_stock_still_completes():
    g = _g()
    c = g.create("chores", {"name": "Trash", "period_type": "daily", "consume_product_on_chore_execution": 1,
                            "product_id": 5, "product_amount": 1})
    g.execute_chore(c, None)
    assert g.chores_status()[0]["last_tracked_time"].startswith("2026-11-18")


def test_fake_rotation_who_least_did_first():
    g = _g()
    c = g.create("chores", {"name": "Dishes", "period_type": "daily", "assignment_type": "who-least-did-first",
                            "assignment_config": "1,2"})
    assert g.chores_status()[0]["next_execution_assigned_to_user_id"] == 1
    for u in (1, 2, 1):
        g.execute_chore(c, u)
    assert g.chores_status()[0]["next_execution_assigned_to_user_id"] == 2


def test_fake_weekly_monthly_tasks_batteries():
    g = _g()  # a Wednesday
    w = g.create("chores", {"name": "w", "period_type": "weekly", "period_config": "friday"})
    m = g.create("chores", {"name": "m", "period_type": "monthly", "period_days": 5})
    g.execute_chore(w, None); g.execute_chore(m, None)
    due = {r["chore_id"]: r["next_estimated_execution_time"][:10] for r in g.chores_status()}
    assert due == {w: "2026-11-20", m: "2026-12-05"}
    t = g.create("tasks", {"name": "t", "done": 0})
    g.complete_task(t)
    assert g.open_tasks() == []
    b = g.create("batteries", {"name": "b", "charge_interval_days": 30})
    g.charge_battery(b)
    assert g.batteries_status()[0]["next_estimated_charge_time"].startswith("2026-12-18")
