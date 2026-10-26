#!/usr/bin/env python3
"""Unit test logic lọc (không đụng mạng). Chạy: python test_first_lesson.py"""
import datetime as dt

from watch_first_lesson import (
    VN_TZ,
    select_new_candidates,
    count_completed_after,
    todays_lessons,
    _parse_pay_time,
    _clean_uid,
)

NOW = dt.datetime(2026, 10, 26, 12, 0, tzinfo=VN_TZ)


def _pt(day, hh=10, mm=0):
    return dt.datetime(2026, 10, day, hh, mm, tzinfo=VN_TZ).isoformat()


def test_clean_uid():
    assert _clean_uid("3316701347") == "3316701347"
    assert _clean_uid(" 3316701347 ") == "3316701347"
    assert _clean_uid("84-966845572") is None   # placeholder SĐT
    assert _clean_uid("") is None
    assert _clean_uid(None) is None
    assert _clean_uid("123") is None            # quá ngắn


def test_new_single_order():
    orders = [{"uid": "100000001", "pay_time": _pt(20), "is_test": False}]
    out = select_new_candidates(orders, NOW, lookback_days=45, same_buy_minutes=60)
    assert "100000001" in out


def test_new_siblings_same_buy():
    # 2 đơn (anh em) cách nhau 15' cùng ngày -> vẫn là MỚI
    orders = [
        {"uid": "100000002", "pay_time": _pt(20, 10, 0), "is_test": False},
        {"uid": "100000002", "pay_time": _pt(20, 10, 15), "is_test": False},
    ]
    out = select_new_candidates(orders, NOW, 45, 60)
    assert "100000002" in out
    assert out["100000002"] == _parse_pay_time(_pt(20, 10, 0))  # purchase = sớm nhất


def test_resell_excluded():
    # 2 lần mua cách xa (tái ký) -> KHÔNG mới
    orders = [
        {"uid": "100000003", "pay_time": _pt(1), "is_test": False},
        {"uid": "100000003", "pay_time": _pt(20), "is_test": False},
    ]
    out = select_new_candidates(orders, NOW, 45, 60)
    assert "100000003" not in out


def test_old_purchase_excluded():
    # mua quá lâu (ngoài lookback) -> loại dù chỉ 1 đơn
    orders = [{"uid": "100000004", "pay_time": _pt(1), "is_test": False}]  # 25 ngày? -> trong 45
    out = select_new_candidates(orders, NOW, lookback_days=10, same_buy_minutes=60)
    assert "100000004" not in out  # 26->1 = 25 ngày > 10


def test_is_test_skipped():
    orders = [{"uid": "100000005", "pay_time": _pt(20), "is_test": True}]
    out = select_new_candidates(orders, NOW, 45, 60)
    assert "100000005" not in out


def _item(day, hh, action):
    return {"local_time_desc": f"2026-10-{day:02d} {hh:02d}:00(Viet Nam)", "action": action}


def test_count_completed_after_purchase():
    purchase = _parse_pay_time(_pt(20, 9, 0))
    items = [
        _item(18, 10, 2),   # complete TRƯỚC purchase (học thử) -> không tính
        _item(22, 10, 2),   # complete SAU purchase -> tính
        _item(23, 10, 101),  # vắng SAU purchase -> KHÔNG tính
        _item(24, 10, 1),   # chờ lên lớp -> không tính
    ]
    assert count_completed_after(items, purchase) == 1


def test_count_zero_keeps():
    purchase = _parse_pay_time(_pt(20, 9, 0))
    items = [_item(23, 10, 101), _item(24, 10, 1)]  # vắng + chờ -> 0 complete
    assert count_completed_after(items, purchase) == 0


def test_todays_lessons():
    today = dt.date(2026, 10, 26)
    items = [
        _item(26, 17, 1),
        _item(26, 15, 1),
        _item(25, 10, 2),  # hôm qua -> bỏ
    ]
    rows = todays_lessons(items, today)
    assert [r["hhmm"] for r in rows] == ["15:00", "17:00"]  # sắp theo giờ


def run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} test passed.")


if __name__ == "__main__":
    run()
