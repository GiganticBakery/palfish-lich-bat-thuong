#!/usr/bin/env python3
"""
Danh sách học viên MỚI cần chăm TRƯỚC buổi học đầu tiên (đặt hàng chị Huyền).

Luồng:
  1. Lọc học viên mới từ Supabase `so_doanh_thu` (nguồn = phần non-HCM của
     gmv_all + HCM Online đều đã nằm ở đây):
       - "mới" = toàn bộ đơn của 1 uid rơi vào CÙNG 1 lần mua
         (1 đơn, hoặc nhiều đơn/nhiều bé nhưng pay_time nằm trong cùng
         1 cửa sổ -> case anh em). Nếu có đơn ở lần mua khác -> tái ký, bỏ.
       - chỉ giữ uid có lần mua trong N ngày gần đây (candidate đáng chăm).
  2. Với mỗi candidate: quét lịch CRM (tái dùng engine watch_schedule) rồi
     đếm buổi ĐÃ HỌC CHÍNH THỨC = action==2 ("Đã kết thúc lớp") có ngày học
     > purchase_time. Vắng (101) KHÔNG tính là đã học. Giữ uid nếu đếm = 0.
  3. Ghi Google Sheet RIÊNG (khác sheet "lịch bất thường"):
       - tab "Cần chăm hôm nay": bé có buổi trong ngày + giờ + giáo viên.
       - tab "Danh sách chờ": mọi bé mới chưa học buổi nào.

Dùng chung reader lịch CRM + auth token + sheets service của watch_schedule.py
(không sửa engine). Chạy qua GitHub Actions (cron 30', 8h30-22h VN).

Env cần (giống engine, trừ sheet đích riêng):
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   -> đọc so_doanh_thu (+ token CRM)
  CRM_ENCRYPT_KEY                           -> giải mã token CRM
  GOOGLE_SA_JSON | GOOGLE_SERVICE_ACCOUNT_JSON
  GOOGLE_SHEET_ID_FIRST                     -> sheet đích RIÊNG của chị Huyền
  CRM_COOKIE (tuỳ chọn, test local)
  FIRST_LESSON_LOOKBACK_DAYS   (mặc định 45)  -> cửa sổ "mới mua gần đây"
  FIRST_LESSON_SAME_BUY_MINUTES(mặc định 60)  -> ngưỡng coi là cùng 1 lần mua
"""
from __future__ import annotations

import os
import re
import sys
import datetime as dt
from collections import defaultdict

import httpx

# Tái dùng engine — KHÔNG sửa watch_schedule.py
from watch_schedule import (
    VN_TZ,
    ACTION_STATUS,
    get_auth_headers,
    fetch_lessons,
    build_sheets_service,
)

SHEET_TODAY = "Cần chăm hôm nay"
SHEET_WAITING = "Danh sách chờ"
POOL_TAB = "_pool"  # tab ẩn lưu state: bé đang chờ học buổi đầu

# Cửa sổ quét CRM đủ lùi để thấy mọi buổi kể từ lúc mua (>= lookback mua).
CRM_WINDOW_BACK_DAYS = 55
CRM_WINDOW_AHEAD_DAYS = 40
ACTION_COMPLETED = 2  # "Đã kết thúc lớp" = đã học chính thức


# ---------------------------------------------------------------------------
# Pure helpers (unit-test được, không đụng mạng)
# ---------------------------------------------------------------------------
def _parse_pay_time(value) -> dt.datetime | None:
    """pay_time Supabase (ISO, có/không offset) -> datetime tz-aware (VN)."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        # fallback "YYYY-MM-DD HH:MM:SS"
        m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})", s)
        if not m:
            return None
        d = dt.datetime(int(m.group(1)[:4]), int(m.group(1)[5:7]), int(m.group(1)[8:10]),
                        int(m.group(2)), int(m.group(3)))
    if d.tzinfo is None:
        d = d.replace(tzinfo=VN_TZ)
    return d.astimezone(VN_TZ)


def _clean_uid(value) -> str | None:
    """Giữ uid CRM thật (toàn số, >= 8 chữ số). Bỏ placeholder '84-...'/rỗng."""
    if value is None:
        return None
    s = str(value).strip()
    if s.isdigit() and len(s) >= 8:
        return s
    return None


def select_new_candidates(
    orders: list[dict],
    now: dt.datetime,
    lookback_days: int,
    same_buy_minutes: int,
) -> dict[str, dt.datetime]:
    """
    orders: [{uid, pay_time, is_test}] — TOÀN BỘ lịch sử (không chỉ gần đây).
    Trả {uid -> purchase_time} cho học viên "mới" mua trong lookback_days.

    "mới" = mọi đơn của uid nằm trong cùng 1 cửa sổ <= same_buy_minutes
    (1 đơn, hoặc nhiều đơn cùng lần mua). Có đơn ở lần mua khác -> loại.
    """
    by_uid: dict[str, list[dt.datetime]] = defaultdict(list)
    for o in orders:
        if o.get("is_test") in (True, "true", "True", 1, "1"):
            continue
        uid = _clean_uid(o.get("uid"))
        pt = _parse_pay_time(o.get("pay_time"))
        if uid and pt:
            by_uid[uid].append(pt)

    window = dt.timedelta(minutes=same_buy_minutes)
    earliest_ok = now - dt.timedelta(days=lookback_days)
    out: dict[str, dt.datetime] = {}
    for uid, times in by_uid.items():
        times.sort()
        # tất cả đơn trong cùng 1 lần mua?
        if times[-1] - times[0] > window:
            continue
        purchase = times[0]
        if purchase < earliest_ok or purchase > now:
            continue
        out[uid] = purchase
    return out


def _lesson_dt(item: dict) -> dt.datetime | None:
    """local_time_desc '2026-10-21 16:00(Viet Nam)' -> datetime VN."""
    ld = item.get("local_time_desc") or ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", ld)
    if not m:
        return None
    y, mo, d, hh, mm = (int(x) for x in m.groups())
    return dt.datetime(y, mo, d, hh, mm, tzinfo=VN_TZ)


def count_completed_after(items: list[dict], purchase: dt.datetime) -> int:
    """Đếm buổi đã học chính thức (action==2) có giờ học > purchase_time."""
    n = 0
    for it in items:
        if it.get("action") != ACTION_COMPLETED:
            continue
        ldt = _lesson_dt(it)
        if ldt and ldt > purchase:
            n += 1
    return n


def todays_lessons(items: list[dict], today: dt.date) -> list[dict]:
    """Buổi học trong NGÀY hôm nay (mọi trạng thái), sắp theo giờ."""
    rows = []
    for it in items:
        ldt = _lesson_dt(it)
        if not ldt or ldt.date() != today:
            continue
        a = it.get("action")
        rows.append({
            "hhmm": ldt.strftime("%H:%M"),
            "mins": ldt.hour * 60 + ldt.minute,
            "status": ACTION_STATUS.get(a, f"Khác (mã {a})"),
            "teacher": (it.get("teacher") or {}).get("name") or "",
        })
    rows.sort(key=lambda r: r["mins"])
    return rows


# ---------------------------------------------------------------------------
# Supabase: kéo toàn bộ (uid, pay_time, is_test) của so_doanh_thu
# ---------------------------------------------------------------------------
def load_orders() -> list[dict]:
    cookie_env = os.getenv("CRM_COOKIE", "").strip()
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not (url and key):
        if cookie_env:
            sys.exit("[ERR] Test local cần SUPABASE_URL/KEY để đọc so_doanh_thu")
        sys.exit("[ERR] Thiếu SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    from supabase import create_client

    sb = create_client(url, key)
    out: list[dict] = []
    page = 0
    size = 1000
    while True:
        res = (sb.table("so_doanh_thu")
               .select("uid, pay_time, is_test")
               .range(page * size, page * size + size - 1)
               .execute())
        batch = res.data or []
        out.extend(batch)
        if len(batch) < size:
            break
        page += 1
    print(f"[supabase] đọc {len(out)} dòng so_doanh_thu")
    return out


# ---------------------------------------------------------------------------
# Google Sheet riêng
# ---------------------------------------------------------------------------
def _ensure_tabs(svc, sheet_id: str) -> dict:
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets", [])}
    reqs = []
    for t in (SHEET_TODAY, SHEET_WAITING, POOL_TAB):
        if t not in existing:
            props = {"title": t}
            if t == POOL_TAB:  # tab state -> ẩn khỏi mắt người xem
                props["hidden"] = True
            reqs.append({"addSheet": {"properties": props}})
    if reqs:
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": reqs}).execute()
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                    for s in meta.get("sheets", [])}
    return existing


# ---------------------------------------------------------------------------
# State pool (tab ẩn _pool): bé đang chờ học buổi đầu, bền qua các lần chạy
# ---------------------------------------------------------------------------
def read_pool(svc, sheet_id: str) -> dict[str, dict]:
    """Đọc tab _pool -> {uid: {ten, purchase(datetime VN)}}. Bỏ qua nếu chưa có."""
    try:
        res = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{POOL_TAB}'!A2:C").execute()
    except Exception:
        return {}
    pool: dict[str, dict] = {}
    for row in res.get("values", []):
        if len(row) < 3:
            continue
        uid = _clean_uid(row[0])
        pt = _parse_pay_time(row[2])
        if uid and pt:
            pool[uid] = {"ten": row[1] or "", "purchase": pt}
    return pool


def write_pool(svc, sheet_id: str, pool: dict[str, dict]):
    values = [["uid", "ten", "purchase_time"]]
    for uid, v in sorted(pool.items(), key=lambda kv: kv[1]["purchase"], reverse=True):
        values.append([uid, v["ten"], v["purchase"].isoformat()])
    svc.spreadsheets().values().batchClear(
        spreadsheetId=sheet_id, body={"ranges": [f"'{POOL_TAB}'!A1:C5000"]}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{POOL_TAB}'!A1",
        valueInputOption="RAW", body={"values": values}).execute()


def write_sheet(svc, sheet_id: str, tabs: dict, kept: list[dict]):
    """kept: [{uid, ten, purchase, today_rows}] — bé mới chưa học buổi nào."""
    now_vn = dt.datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")

    with_today = [k for k in kept if k["today_rows"]]

    # ----- Tab Cần chăm hôm nay -----
    banner = (f"🔔 {len(with_today)} học viên có buổi học hôm nay — CS chuẩn bị chăm"
              if with_today else "✅ Hôm nay chưa có học viên mới nào tới buổi học")
    today_values = [
        [banner],
        [f"Cập nhật lúc: {now_vn} (giờ VN) · quét mỗi 30 phút · 8h30–22h"],
        [],
        ["Tên bé", "UID", "Giờ học (VN)", "Giáo viên", "Trạng thái", "Ngày mua"],
    ]
    for k in sorted(with_today, key=lambda k: k["today_rows"][0]["mins"]):
        gio = ", ".join(r["hhmm"] for r in k["today_rows"])
        gv = " / ".join(sorted({r["teacher"] for r in k["today_rows"] if r["teacher"]}))
        tt = " / ".join(sorted({r["status"] for r in k["today_rows"]}))
        today_values.append([k["ten"], k["uid"], gio, gv, tt,
                             k["purchase"].strftime("%Y-%m-%d")])
    if not with_today:
        today_values.append(["— không có —"])

    # ----- Tab Danh sách chờ (mọi bé mới chưa học) -----
    wait_header = ["Tên bé", "UID", "Ngày mua", "Có buổi hôm nay?", "Giờ hôm nay"]
    wait_values = [wait_header]
    for k in sorted(kept, key=lambda k: k["purchase"], reverse=True):
        gio = ", ".join(r["hhmm"] for r in k["today_rows"]) if k["today_rows"] else ""
        wait_values.append([k["ten"], k["uid"], k["purchase"].strftime("%Y-%m-%d"),
                            "Có" if k["today_rows"] else "", gio])
    if not kept:
        wait_values.append(["— không có học viên mới chưa học —"])

    svc.spreadsheets().values().batchClear(
        spreadsheetId=sheet_id,
        body={"ranges": [f"'{SHEET_TODAY}'!A1:Z1000", f"'{SHEET_WAITING}'!A1:Z3000"]},
    ).execute()
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": [
            {"range": f"'{SHEET_TODAY}'!A1", "values": today_values},
            {"range": f"'{SHEET_WAITING}'!A1", "values": wait_values},
        ]},
    ).execute()

    # ----- Định dạng banner + header + freeze -----
    tid, wid = tabs[SHEET_TODAY], tabs[SHEET_WAITING]
    blue = {"red": 0.82, "green": 0.89, "blue": 0.98}
    green = {"red": 0.80, "green": 0.92, "blue": 0.80}
    fmt = [
        {"mergeCells": {"range": {"sheetId": tid, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 0, "endColumnIndex": 6}, "mergeType": "MERGE_ALL"}},
        {"repeatCell": {
            "range": {"sheetId": tid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {
                "backgroundColor": blue if with_today else green,
                "textFormat": {"bold": True, "fontSize": 13},
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"}},
        {"repeatCell": {"range": {"sheetId": tid, "startRowIndex": 3, "endRowIndex": 4},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat"}},
        {"repeatCell": {"range": {"sheetId": wid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat"}},
        {"updateSheetProperties": {"properties": {"sheetId": wid, "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
    ]
    try:
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": fmt}).execute()
    except Exception as exc:  # format lỗi không chặn ghi data
        print(f"[warn] format lỗi (bỏ qua): {exc}")


# ---------------------------------------------------------------------------
def main():
    sheet_id = os.getenv("GOOGLE_SHEET_ID_FIRST", "").strip()
    if not sheet_id:
        sys.exit("[ERR] Chưa có GOOGLE_SHEET_ID_FIRST (sheet riêng chị Huyền)")
    same_buy = int(os.getenv("FIRST_LESSON_SAME_BUY_MINUTES") or "60")
    # Cửa sổ "bé mới vừa mua" cần nạp mỗi lần (nhỏ). Seed lần đầu: chạy tay với
    # FIRST_LESSON_SEED_DAYS lớn (vd 45) để gom hết tồn.
    arrival_days = int(os.getenv("FIRST_LESSON_ARRIVAL_DAYS") or "3")
    seed_days = int(os.getenv("FIRST_LESSON_SEED_DAYS") or "0")
    arrival_days = max(arrival_days, seed_days)
    # Bé ở pool quá lâu vẫn chưa học -> coi như tồn đọng, rút khỏi pool.
    pool_max_days = int(os.getenv("FIRST_LESSON_POOL_MAX_DAYS") or "45")

    now = dt.datetime.now(VN_TZ)
    today = now.date()

    svc = build_sheets_service()
    tabs = _ensure_tabs(svc, sheet_id)
    pool = read_pool(svc, sheet_id)
    print(f"[pool] {len(pool)} bé đang chờ (từ lần chạy trước)")

    # --- Lớp 1: nạp bé MỚI mua gần đây, chưa có trong pool ---
    orders = load_orders()
    candidates = select_new_candidates(orders, now, arrival_days, same_buy)
    new_uids = {u: p for u, p in candidates.items() if u not in pool}
    print(f"[arrival] {len(new_uids)} bé mới (mua trong {arrival_days} ngày) cần nạp")

    # purchase_time cho từng uid cần quét: pool giữ mốc cũ, bé mới lấy từ candidates
    scan: dict[str, dt.datetime] = {u: v["purchase"] for u, v in pool.items()}
    scan.update(new_uids)

    # --- Lớp 2: quét CRM CHỈ các uid trong (pool ∪ bé mới) ---
    headers = get_auth_headers()
    crm_cfg = {"date_window_days_back": CRM_WINDOW_BACK_DAYS,
               "date_window_days_ahead": CRM_WINDOW_AHEAD_DAYS}
    new_pool: dict[str, dict] = {}
    kept: list[dict] = []
    stale_cut = now - dt.timedelta(days=pool_max_days)
    print(f"[scan] quét CRM {len(scan)} bé")
    with httpx.Client() as client:
        for uid, purchase in scan.items():
            if purchase < stale_cut:  # tồn đọng quá lâu -> bỏ khỏi pool
                print(f"[drop] uid={uid} tồn >{pool_max_days} ngày, bỏ pool")
                continue
            try:
                items = fetch_lessons(client, headers, int(uid), crm_cfg)
            except Exception as exc:  # 1 uid lỗi -> giữ nguyên trong pool, bỏ qua lần này
                print(f"[warn] uid={uid} lỗi lấy lịch: {exc}")
                if uid in pool:
                    new_pool[uid] = pool[uid]
                continue
            if count_completed_after(items, purchase) >= 1:
                continue  # đã học buổi đầu -> rút khỏi pool
            ten = (pool.get(uid, {}).get("ten")
                   or next((((it.get("student") or {}).get("name") or "")
                            for it in items if (it.get("student") or {}).get("name")), ""))
            new_pool[uid] = {"ten": ten, "purchase": purchase}
            kept.append({"uid": uid, "ten": ten, "purchase": purchase,
                         "today_rows": todays_lessons(items, today)})

    kept_today = sum(1 for k in kept if k["today_rows"])
    print(f"[kept] pool còn {len(new_pool)} bé · {kept_today} bé có buổi hôm nay")
    write_pool(svc, sheet_id, new_pool)
    write_sheet(svc, sheet_id, tabs, kept)
    print("[done] Ghi sheet xong.")


if __name__ == "__main__":
    main()
