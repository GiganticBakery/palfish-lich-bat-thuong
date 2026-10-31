#!/usr/bin/env python3
"""Unit test logic lọc (không đụng mạng). Chạy: python test_first_lesson.py"""
import datetime as dt

from watch_first_lesson import (
    VN_TZ,
    select_new_candidates,
    renewal_uids_from,
    count_completed_after,
    todays_lessons,
    week_bounds,
    week_lessons,
    _parse_pay_time,
    _clean_uid,
    fetch_gvcn,
    fetch_phone,
    read_cs_notes,
    build_log_rows,
    LEADSINFO_URL,
    LEADSPHONE_URL,
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


def test_renewal_loai_excluded():
    # 1 đơn gần đây NHƯNG loai=续费 -> gia hạn -> LOẠI (bịt edge case đơn gốc thiếu)
    orders = [{"uid": "100000009", "pay_time": _pt(20), "is_test": False, "loai": "续费"}]
    assert "100000009" not in select_new_candidates(orders, NOW, 45, 60)


def test_new_acquisition_kept():
    orders = [{"uid": "100000010", "pay_time": _pt(20), "is_test": False, "loai": "广告"}]
    assert "100000010" in select_new_candidates(orders, NOW, 45, 60)


def test_renewal_uids_from_detects_zufei_and_resell():
    orders = [
        {"uid": "100000011", "pay_time": _pt(1), "is_test": False, "loai": "广告"},
        {"uid": "100000011", "pay_time": _pt(20), "is_test": False, "loai": "Resell"},
        {"uid": "100000012", "pay_time": _pt(20), "is_test": False, "loai": "续费"},
        {"uid": "100000013", "pay_time": _pt(20), "is_test": False, "loai": "转介绍"},
        {"uid": "100000014", "pay_time": _pt(20), "is_test": True, "loai": "续费"},  # test -> bỏ
    ]
    assert renewal_uids_from(orders) == {"100000011", "100000012"}


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


# --- fetch_profile: parse GVCN + SĐT từ shape thật của CRM (không đụng mạng) ---
class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeClient:
    """Giả client: trả đúng shape leadsinfo/get và leadsphone/get đã bắt từ HAR."""
    def __init__(self, info=None, phone=None):
        self._info = info
        self._phone = phone

    def post(self, url, headers=None, json=None, timeout=None):
        if url == LEADSINFO_URL:
            return _Resp(self._info)
        if url == LEADSPHONE_URL:
            return _Resp(self._phone)
        raise AssertionError(f"url lạ: {url}")


_INFO_OK = {"ret": 1, "data": {"ent": {"leads_info": {
    "head_teacher": {"name": "Ellie Advisor"},   # = GVCN
    "sale": {"name": "Pham Hong Hanh"},           # = NV bán hàng (phải BỎ)
    "user": {"phone": "84-886****88"},            # SĐT bị che -> không dùng
}}}}
_PHONE_OK = {"ret": 1, "data": {"ent": {"phone": "84-946224065"}}}  # số đầy đủ


def test_fetch_gvcn_head_teacher_not_sale():
    assert fetch_gvcn(_FakeClient(_INFO_OK, None), {}, 3321114530) == "Ellie Advisor"


def test_fetch_gvcn_error_returns_none():
    # leadsinfo lỗi (token) -> None (thử lại lượt sau)
    assert fetch_gvcn(_FakeClient({"ret": -1}, None), {}, 1) is None


def test_fetch_phone_full_number():
    assert fetch_phone(_FakeClient(None, _PHONE_OK), {}, 1) == "84-946224065"


def test_fetch_phone_ratelimited_returns_none():
    # ret!=1 (giới hạn tần suất) -> None để caller ngắt, KHÁC '' (không có số)
    assert fetch_phone(_FakeClient(None, {"ret": -1}), {}, 1) is None
    assert fetch_phone(_FakeClient(None, {"ret": 1, "data": {"ent": {"phone": ""}}}), {}, 1) == ""


# --- read_cs_notes: giữ ghi chú CS neo theo UID (không đụng mạng) ---
class _FakeSheets:
    def __init__(self, values):
        self._values = values

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId=None, range=None):
        return self

    def execute(self):
        return {"values": self._values}


def test_read_cs_notes_by_header_name():
    # Dò cột theo TÊN header -> đổi layout không lệch. header dòng 1, data dòng 2.
    header = ["Tên bé", "UID", "SĐT", "GVCN", "Ngày mua", "Có buổi hôm nay?",
              "Giờ hôm nay", "Trạng thái CS", "Ghi chú CS"]
    rows = [
        header,
        ["Gold", "3321114530", "84-886110888", "Ellie", "2026-10-27", "Có", "18:00",
         "Đã gọi", "mẹ bận, hẹn mai"],
        ["", "3319622906", "84-902569605", "Tran Thu Ha", "2026-10-27", "", "",
         "Chưa gọi", ""],
        ["x", "3320033660", "", "", "2026-10-23", "", "", "", ""],   # không note -> bỏ
        ["x", "84-placeholder", "", "", "", "", "", "Đã gọi", ""],    # uid rác -> bỏ
    ]
    out = read_cs_notes(_FakeSheets(rows), "sid", "Danh sách chờ", 1, 2)
    assert out == {
        "3321114530": ["Đã gọi", "mẹ bận, hẹn mai"],
        "3319622906": ["Chưa gọi", ""],
    }, out


# --- build_log_rows: log theo buổi đầu trong tuần, dedup (Ngày,UID), snapshot CS ---
def _wk(uid, ten, hh, mm, date="2026-10-29"):
    dk = dt.date.fromisoformat(date).toordinal() * 10000 + hh * 60 + mm
    return {"uid": uid, "ten": ten, "purchase": NOW,
            "week_rows": [{"date": date, "dkey": dk, "hhmm": f"{hh:02d}:{mm:02d}",
                           "status": "Chờ lên lớp", "teacher": "T. A"}]}


def test_build_log_dedup_and_cs_snapshot():
    old = [
        ["2026-10-28", "Bé Cũ", "3300000001", "84-1", "GV1", "10:00", "T", "Chờ",
         "", ""],   # chưa có ghi chú CS
        ["2026-10-29", "Trùng", "3300000002", "", "", "09:00", "T", "Chờ", "", ""],
    ]
    profiles = {"3300000003": {"sdt": "84-3", "gvcn": "GV3"}}
    care = {"3300000001": ["done", "ok"], "3300000003": ["Chưa gọi", ""]}
    students = [_wk("3300000002", "Trùng", 9, 0),    # (2026-10-29, ..2) đã có -> bỏ
                _wk("3300000003", "Bé Mới", 8, 30)]
    out = build_log_rows(old, students, profiles, care)
    assert out[0][:6] == ["2026-10-29", "Bé Mới", "3300000003", "84-3", "GV3", "08:30"], out[0]
    assert out[0][8:10] == ["Chưa gọi", ""]          # bé mới: snapshot ghi chú CS từ care
    assert len(out) == 3, out                        # 2 cũ + 1 mới (dedup ..2)
    old1 = [r for r in out if r[2] == "3300000001"][0]
    assert old1[8:10] == ["done", "ok"]              # dòng cũ: CẬP NHẬT ghi chú CS từ care


def test_week_bounds_monday_sunday():
    mon, sun = week_bounds(dt.date(2026, 10, 28))
    assert mon.weekday() == 0                       # T2
    assert sun == mon + dt.timedelta(days=6)        # CN
    assert mon <= dt.date(2026, 10, 28) <= sun


def test_week_lessons_filters_to_week_and_sorts():
    mon, sun = dt.date(2026, 10, 26), dt.date(2026, 11, 1)
    items = [_item(28, 14, 2), _item(26, 9, 1), _item(25, 10, 1)]  # 25 = ngoài tuần
    rows = week_lessons(items, mon, sun)
    assert [r["date"] for r in rows] == ["2026-10-26", "2026-10-28"]  # trong tuần, sắp tăng
    assert rows[0]["hhmm"] == "09:00"


def run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} test passed.")


if __name__ == "__main__":
    run()
