from datetime import datetime

from app.house import HouseStore, Reminder


def test_store_roundtrip_people_and_reminders(tmp_path):
    s = HouseStore(tmp_path / "house.db")
    pid = s.save_person("Josh", 3, "person.jhamlin", "mobile_app_pixel_10_pro")
    s.save_person("Cat", 4, "person.catherine", "mobile_app_cat_s_pixel_10")
    s.save_person("Joshua", 3, "person.jhamlin", "mobile_app_pixel_10_pro", id=pid)
    r = Reminder("chore:1", "person:1", "08:30", 15, datetime(2026, 11, 18, 9), datetime(2026, 11, 18, 8, 30),
                 ["mobile_app_pixel_10_pro"], "Moved it")
    s.save_reminder(r)
    s2 = HouseStore(tmp_path / "house.db")
    assert [p.name for p in s2.people()] == ["Joshua", "Cat"]
    assert s2.person(pid).ha_person == "person.jhamlin" and s2.person(99) is None
    assert s2.reminder("chore:1") == r and s2.keys() == ["chore:1"]
    s2.delete_person(pid)
    s2.drop_reminder("chore:1")
    assert [p.name for p in s2.people()] == ["Cat"] and s2.keys() == []


def test_reminder_defaults(tmp_path):
    r = HouseStore(tmp_path / "h.db").reminder("task:9")
    assert r == Reminder("task:9", None, "09:00", None, None, None, [], None)


def test_flags(tmp_path):
    s = HouseStore(tmp_path / "h.db")
    assert s.flag("ha_down") is None
    s.set_flag("ha_down", "1")
    assert s.flag("ha_down") == "1"
    s.set_flag("ha_down", None)
    assert s.flag("ha_down") is None
