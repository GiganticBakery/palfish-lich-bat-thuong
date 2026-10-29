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
import time
import datetime as dt
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

# Tái dùng engine — KHÔNG sửa watch_schedule.py
from watch_schedule import (
    VN_TZ,
    APPOINT_URL,
    ACTION_STATUS,
    get_auth_headers,
    fetch_lessons,
    build_sheets_service,
)

SHEET_TODAY = "Cần chăm hôm nay"
SHEET_WAITING = "Danh sách chờ"
POOL_TAB = "_pool"      # tab ẩn lưu state: bé đang chờ học buổi đầu
HEALTH_TAB = "_health"  # tab nhật ký mỗi lượt quét (OK/SKIP) — để chẩn đoán
PROFILE_TAB = "_profile"  # tab ẩn cache uid -> (GVCN, SĐT), khỏi tra CRM lại mỗi lượt
LOG_TAB = "Lịch sử buổi đầu"  # log bé có buổi đầu các ngày trước (append, giữ qua ngày)
LOG_MAX = 3000  # giữ tối đa 3000 dòng gần nhất

# Hồ sơ học viên CRM (per-UID). leadsinfo/get có GVCN = head_teacher.name (KHÁC
# sale = NV bán hàng) nhưng SĐT bị CHE -> SĐT đầy đủ lấy riêng ở leadsphone/get.
LEADSINFO_URL = "https://sea.pri.ibanyu.com/opapi/crmi18n/crmopapii18n/leads/leadsinfo/get"
LEADSPHONE_URL = "https://sea.pri.ibanyu.com/opapi/crmi18n/crmopapii18n/leads/leadsphone/get"

# Cửa sổ quét CRM. CRM appoint/formal/list TỪ CHỐI (ret=-1) khi khoảng ngày quá
# rộng — cửa sổ 95 ngày (55+40) làm MỌI request fail, kéo theo "lô đầu lỗi -> skip"
# và sheet trắng. Thực tế chỉ cần: đủ LÙI để phủ bé tồn lâu nhất trong pool
# (pool_max_days, mặc định 45) nhằm đếm đúng buổi đã học kể từ lúc mua; và chỉ cần
# vài ngày TỚI để bắt buổi hôm nay (không cần nhìn xa tương lai). Tổng ~50 ngày,
# nằm dưới ngưỡng CRM (Job 1 chạy tốt ở ~54 ngày).
CRM_WINDOW_BACK_DAYS = 48   # phải >= FIRST_LESSON_POOL_MAX_DAYS, nếu không đếm thiếu buổi đã học
CRM_WINDOW_AHEAD_DAYS = 2
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
    for t in (SHEET_TODAY, SHEET_WAITING, LOG_TAB, POOL_TAB, HEALTH_TAB, PROFILE_TAB):
        if t not in existing:
            props = {"title": t}
            if t in (POOL_TAB, PROFILE_TAB):  # tab state -> ẩn khỏi mắt người xem
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


# ---------------------------------------------------------------------------
# Cache hồ sơ (tab ẩn _profile): uid -> {gvcn, sdt}. SĐT không đổi, GVCN hiếm đổi
# -> cache để mỗi lượt chỉ tra CRM uid còn thiếu, nhẹ tải + đỡ cháy token.
# ---------------------------------------------------------------------------
def read_profile(svc, sheet_id: str) -> dict[str, dict]:
    try:
        res = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{PROFILE_TAB}'!A2:C").execute()
    except Exception:
        return {}
    prof: dict[str, dict] = {}
    for row in res.get("values", []):
        uid = _clean_uid(row[0]) if row else None
        if uid:
            prof[uid] = {"gvcn": (row[1] if len(row) > 1 else "") or "",
                         "sdt": (row[2] if len(row) > 2 else "") or ""}
    return prof


def write_profile(svc, sheet_id: str, profiles: dict[str, dict]):
    values = [["uid", "gvcn", "sdt"]]
    for uid, v in sorted(profiles.items()):
        values.append([uid, v.get("gvcn", ""), v.get("sdt", "")])
    svc.spreadsheets().values().batchClear(
        spreadsheetId=sheet_id, body={"ranges": [f"'{PROFILE_TAB}'!A1:C20000"]}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{PROFILE_TAB}'!A1",
        valueInputOption="RAW", body={"values": values}).execute()


def fetch_profile(client, headers, uid) -> dict | None:
    """Trả {gvcn, sdt} cho 1 uid, hoặc None nếu không lấy nổi cả GVCN (token lỗi).

    GVCN = leads_info.head_teacher.name (leadsinfo/get) — KHÁC sale (NV bán hàng).
    SĐT  = data.ent.phone (leadsphone/get, số đầy đủ; leadsinfo che bớt số nên không dùng).
    SĐT lỗi riêng -> trả gvcn kèm sdt rỗng, lượt sau tự tra lại phần thiếu.
    """
    try:
        n = int(uid)
    except Exception:
        return None
    body = {"uid": n, "zone_name": "Asia/Saigon", "h_lc": "vi"}
    try:
        j = client.post(LEADSINFO_URL, headers=headers, json=body, timeout=30).json()
        if j.get("ret") != 1:
            return None
        li = ((j.get("data") or {}).get("ent") or {}).get("leads_info") or {}
        gvcn = ((li.get("head_teacher") or {}).get("name") or "").strip()
    except Exception:
        return None
    sdt = ""
    try:
        j = client.post(LEADSPHONE_URL, headers=headers, json=body, timeout=30).json()
        if j.get("ret") == 1:
            sdt = (((j.get("data") or {}).get("ent") or {}).get("phone") or "").strip()
    except Exception:
        pass
    return {"gvcn": gvcn, "sdt": sdt}


def write_health(svc, sheet_id: str, status: str, detail: str):
    """Ghi 1 dòng nhật ký lượt quét vào tab _health (mới nhất trên cùng, giữ 50 dòng).

    Gọi ở MỌI nhánh kết thúc (OK lẫn SKIP) để nhìn sheet là biết tool có chạy không,
    lần cuối thành công khi nào, vì sao skip — không còn cảnh sheet trắng khó đoán.
    Lỗi khi ghi _health KHÔNG được làm hỏng lượt chạy.
    """
    try:
        now_vn = dt.datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        try:
            res = svc.spreadsheets().values().get(
                spreadsheetId=sheet_id, range=f"'{HEALTH_TAB}'!A2:C").execute()
            old = res.get("values", [])
        except Exception:
            old = []
        header = [["Thời điểm (VN)", "Trạng thái", "Chi tiết"]]
        rows = ([[now_vn, status, detail]] + old)[:50]
        svc.spreadsheets().values().batchClear(
            spreadsheetId=sheet_id, body={"ranges": [f"'{HEALTH_TAB}'!A1:C1000"]}).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"'{HEALTH_TAB}'!A1",
            valueInputOption="RAW", body={"values": header + rows}).execute()
    except Exception as exc:  # health là phụ trợ -> không chặn lượt chạy
        print(f"[warn] ghi _health lỗi (bỏ qua): {exc}", flush=True)


# 2 cột cuối CỦA CS (tự điền) — tool GIỮ NGUYÊN theo UID qua mỗi lượt, không ghi đè.
CS_COLS = ["Trạng thái CS", "Ghi chú CS"]


def read_cs_notes(svc, sheet_id: str, tab: str, data_row1: int,
                  uid_col: int, cs_col0: int) -> dict[str, list]:
    """Đọc 2 cột CS tự điền hiện có -> {uid: [trạng_thái, ghi_chú]} để ghi lại.

    data_row1 = dòng (1-based) bắt đầu data; uid_col/cs_col0 = chỉ số cột (0-based).
    Nhờ neo theo UID nên ghi chú luôn theo đúng bé dù dòng sắp xếp lại.
    """
    try:
        res = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!A{data_row1}:Z").execute()
    except Exception:
        return {}
    out: dict[str, list] = {}
    for row in res.get("values", []):
        uid = _clean_uid(row[uid_col]) if len(row) > uid_col else None
        if not uid:
            continue
        st = row[cs_col0] if len(row) > cs_col0 else ""
        nt = row[cs_col0 + 1] if len(row) > cs_col0 + 1 else ""
        if st or nt:
            out[uid] = [st, nt]
    return out


def write_sheet(svc, sheet_id: str, tabs: dict, kept: list[dict], profiles: dict[str, dict]):
    """kept: [{uid, ten, purchase, today_rows}] — bé mới chưa học buổi nào.
    profiles: {uid: {gvcn, sdt}} — bổ sung GVCN (CS phụ trách) + SĐT phụ huynh."""
    now_vn = dt.datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")

    # giữ ghi chú CS hiện có (neo theo UID) trước khi clear + ghi lại
    # Cần chăm hôm nay: data từ dòng 5, UID cột B(1), cột CS = I,J (8,9)
    # Danh sách chờ:    data từ dòng 2, UID cột B(1), cột CS = H,I (7,8)
    today_cs = read_cs_notes(svc, sheet_id, SHEET_TODAY, 5, 1, 8)
    wait_cs = read_cs_notes(svc, sheet_id, SHEET_WAITING, 2, 1, 7)

    def _sdt(uid):
        return (profiles.get(uid) or {}).get("sdt", "")

    def _gvcn(uid):
        return (profiles.get(uid) or {}).get("gvcn", "")

    def _cs(notes, uid):
        return notes.get(uid, ["", ""])

    with_today = [k for k in kept if k["today_rows"]]

    # ----- Tab Cần chăm hôm nay -----
    banner = (f"🔔 {len(with_today)} học viên có buổi học hôm nay — CS chuẩn bị chăm"
              if with_today else "✅ Hôm nay chưa có học viên mới nào tới buổi học")
    today_values = [
        [banner],
        [f"Cập nhật lúc: {now_vn} (giờ VN) · quét mỗi 30 phút · 8h30–22h"],
        [],
        ["Tên bé", "UID", "SĐT", "GVCN", "Giờ học (VN)", "GV dạy buổi",
         "Trạng thái", "Ngày mua"] + CS_COLS,
    ]
    for k in sorted(with_today, key=lambda k: k["today_rows"][0]["mins"]):
        gio = ", ".join(r["hhmm"] for r in k["today_rows"])
        gv = " / ".join(sorted({r["teacher"] for r in k["today_rows"] if r["teacher"]}))
        tt = " / ".join(sorted({r["status"] for r in k["today_rows"]}))
        today_values.append([k["ten"], k["uid"], _sdt(k["uid"]), _gvcn(k["uid"]),
                             gio, gv, tt, k["purchase"].strftime("%Y-%m-%d")]
                            + _cs(today_cs, k["uid"]))
    if not with_today:
        today_values.append(["— không có —"])

    # ----- Tab Danh sách chờ (mọi bé mới chưa học) -----
    wait_header = ["Tên bé", "UID", "SĐT", "GVCN", "Ngày mua",
                   "Có buổi hôm nay?", "Giờ hôm nay"] + CS_COLS
    wait_values = [wait_header]
    for k in sorted(kept, key=lambda k: k["purchase"], reverse=True):
        gio = ", ".join(r["hhmm"] for r in k["today_rows"]) if k["today_rows"] else ""
        wait_values.append([k["ten"], k["uid"], _sdt(k["uid"]), _gvcn(k["uid"]),
                            k["purchase"].strftime("%Y-%m-%d"),
                            "Có" if k["today_rows"] else "", gio]
                           + _cs(wait_cs, k["uid"]))
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
        # bỏ merge cũ trước (lần trước banner merge A–F; nay A–H) để khỏi xung đột
        {"unmergeCells": {"range": {"sheetId": tid, "startRowIndex": 0, "endRowIndex": 1,
                                    "startColumnIndex": 0, "endColumnIndex": 26}}},
        {"mergeCells": {"range": {"sheetId": tid, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 0, "endColumnIndex": 10}, "mergeType": "MERGE_ALL"}},
        {"repeatCell": {
            "range": {"sheetId": tid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 10},
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
# Log bé có buổi đầu các ngày trước (tab "Lịch sử buổi đầu"): append, giữ qua ngày
# để CS rà lại đã chăm buổi đầu chưa. "Cần chăm hôm nay" reset mỗi ngày — tab này
# tích luỹ. Dedup theo (Ngày, UID); giữ nguyên dòng cũ kèm ghi chú CS.
# ---------------------------------------------------------------------------
LOG_HEADER = ["Ngày", "Tên bé", "UID", "SĐT", "GVCN", "Giờ học (VN)",
              "GV dạy buổi", "Trạng thái", "Trạng thái CS", "Ghi chú CS"]


def _today_log_row(k, today_str, profiles):
    gio = ", ".join(r["hhmm"] for r in k["today_rows"])
    gv = " / ".join(sorted({r["teacher"] for r in k["today_rows"] if r["teacher"]}))
    tt = " / ".join(sorted({r["status"] for r in k["today_rows"]}))
    p = profiles.get(k["uid"], {})
    return [today_str, k["ten"], k["uid"], p.get("sdt", ""), p.get("gvcn", ""),
            gio, gv, tt, "", ""]


def build_log_rows(old_rows, with_today, today_str, profiles):
    """Dòng log cũ (giữ nguyên, kèm ghi chú CS) + dòng mới hôm nay (dedup theo
    (Ngày, UID), bé mới lên đầu). Pure — test không cần mạng. Cắt còn LOG_MAX."""
    width = len(LOG_HEADER)
    seen, kept_old = set(), []
    for r in old_rows:
        r = (list(r) + [""] * width)[:width]
        uid = _clean_uid(r[2])
        if uid:
            seen.add((r[0], uid))
        kept_old.append(r)
    new_rows = []
    for k in sorted(with_today, key=lambda k: k["today_rows"][0]["mins"]):
        key = (today_str, k["uid"])
        if key in seen:
            continue
        seen.add(key)
        new_rows.append(_today_log_row(k, today_str, profiles))
    return (new_rows + kept_old)[:LOG_MAX]


def write_log(svc, sheet_id, tabs, kept, today, profiles):
    with_today = [k for k in kept if k["today_rows"]]
    try:
        res = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{LOG_TAB}'!A2:J").execute()
        old = res.get("values", [])
    except Exception:
        old = []
    rows = build_log_rows(old, with_today, today.strftime("%Y-%m-%d"), profiles)
    svc.spreadsheets().values().batchClear(
        spreadsheetId=sheet_id, body={"ranges": [f"'{LOG_TAB}'!A1:J{LOG_MAX + 10}"]}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{LOG_TAB}'!A1",
        valueInputOption="RAW", body={"values": [LOG_HEADER] + rows}).execute()
    try:
        lid = tabs[LOG_TAB]
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [
            {"repeatCell": {"range": {"sheetId": lid, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat"}},
            {"updateSheetProperties": {"properties": {"sheetId": lid,
                                                      "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "gridProperties.frozenRowCount"}},
        ]}).execute()
    except Exception as exc:
        print(f"[warn] format log lỗi (bỏ qua): {exc}")


# ---------------------------------------------------------------------------
# Token CRM sống rất ngắn (~1') → probe 1 phát để biết token còn dùng được không
# (single-shot, không retry) trước khi quét hàng loạt.
# ---------------------------------------------------------------------------
def _probe_token(client, headers, uid: int) -> bool:
    today = dt.datetime.now(VN_TZ).date()
    body = {
        "stuid": int(uid), "kids": [], "action": "", "country_code": "VN",
        "local_zone_name": "Asia/Ho_Chi_Minh",
        "local_begin_time_desc": f"{today} 00:00:00",
        "local_end_time_desc": f"{today} 23:59:59",
        "limit": 1, "offset": 0, "zone_name": "Asia/Saigon", "h_lc": "vi",
    }
    try:
        r = client.post(APPOINT_URL, headers=headers, json=body, timeout=30)
        return r.json().get("ret") == 1
    except Exception:
        return False


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
    # Số luồng quét CRM song song (bám cap concurrency backend = 3).
    concurrency = int(os.getenv("FIRST_LESSON_CONCURRENCY") or "3")

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
    # BACK phải phủ bé tồn lâu nhất (pool_max_days) để đếm đúng buổi đã học kể từ
    # lúc mua; vẫn giữ tổng cửa sổ hẹp để CRM không trả ret=-1.
    crm_cfg = {"date_window_days_back": max(CRM_WINDOW_BACK_DAYS, pool_max_days + 3),
               "date_window_days_ahead": CRM_WINDOW_AHEAD_DAYS}
    batch_size = int(os.getenv("FIRST_LESSON_BATCH") or "25")

    new_pool: dict[str, dict] = {}
    kept: list[dict] = []
    stale_cut = now - dt.timedelta(days=pool_max_days)
    to_scan = [(u, p) for u, p in scan.items() if p >= stale_cut]
    dropped = len(scan) - len(to_scan)
    if dropped:
        print(f"[drop] {dropped} bé tồn >{pool_max_days} ngày, bỏ pool", flush=True)
    print(f"[scan] quét CRM {len(to_scan)} bé (lô {batch_size}, song song "
          f"{concurrency}, đọc lại token mỗi lô)", flush=True)

    def _scan_one(client, hdrs, uid, purchase):
        try:
            items = fetch_lessons(client, hdrs, int(uid), crm_cfg)
            return uid, purchase, items, None
        except Exception as exc:
            return uid, purchase, None, exc

    def _keep(uid, purchase, items):
        if count_completed_after(items, purchase) >= 1:
            return  # đã học buổi đầu -> rút khỏi pool
        ten = (pool.get(uid, {}).get("ten")
               or next((((it.get("student") or {}).get("name") or "")
                        for it in items if (it.get("student") or {}).get("name")), ""))
        new_pool[uid] = {"ten": ten, "purchase": purchase}
        kept.append({"uid": uid, "ten": ten, "purchase": purchase,
                     "today_rows": todays_lessons(items, today)})

    done_n = 0
    with httpx.Client() as client:
        # Token CRM sống ~1' -> đọc lại mỗi lô. Probe trước: token chết thì dừng
        # ngay (khỏi grind), kèm thông báo rõ.
        headers = get_auth_headers()
        if to_scan and not _probe_token(client, headers, to_scan[0][0]):
            headers = get_auth_headers()  # thử đọc lại 1 lần (token vừa xoay)
            if not _probe_token(client, headers, to_scan[0][0]):
                # Không phải lỗi code — chỉ là token chưa tươi (CS chưa mở CRM).
                # Kết thúc êm (exit 0) để Actions không báo đỏ; lần chạy sau tự thử lại.
                print("[skip] Token CRM đang hết hạn — bỏ qua lượt này, chạy lại khi "
                      "CS đang mở CRM (token tự làm mới mỗi phút).", flush=True)
                write_health(svc, sheet_id, "SKIP (token)",
                             "Token CRM hết hạn ngay ở probe — bỏ qua lượt này, tự "
                             "thử lại khi CS đang mở CRM (token làm mới mỗi phút).")
                return

        for bi in range(0, len(to_scan), batch_size):
            if bi > 0:
                headers = get_auth_headers()  # token tươi cho mỗi lô
            batch = to_scan[bi:bi + batch_size]
            fail_in_batch = 0
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                futs = [ex.submit(_scan_one, client, headers, u, p) for u, p in batch]
                for fut in as_completed(futs):
                    uid, purchase, items, exc = fut.result()
                    done_n += 1
                    if exc is not None:  # lỗi -> giữ nguyên pool, thử lại lần sau
                        fail_in_batch += 1
                        if uid in pool:
                            new_pool[uid] = pool[uid]
                        continue
                    _keep(uid, purchase, items)
            print(f"[scan] {done_n}/{len(to_scan)} (lô lỗi {fail_in_batch}/{len(batch)})",
                  flush=True)
            if bi == 0 and fail_in_batch == len(batch):  # lô đầu chết sạch -> dừng êm
                print("[skip] Toàn bộ lô đầu lỗi ret=-1 — có thể cửa sổ ngày/limit "
                      "vượt ngưỡng CRM, hoặc token hết hạn/bị chặn. Bỏ qua lượt này.",
                      flush=True)
                write_health(svc, sheet_id, "SKIP (ret=-1)",
                             f"Cả lô đầu {fail_in_batch}/{len(batch)} bé trả ret=-1 — "
                             "nghi cửa sổ ngày/limit vượt ngưỡng CRM hoặc token hết hạn. "
                             "Bỏ qua lượt này.")
                return

    kept_today = sum(1 for k in kept if k["today_rows"])
    print(f"[kept] pool còn {len(new_pool)} bé · {kept_today} bé có buổi hôm nay")

    # --- Lớp 3: bổ sung SĐT + GVCN cho bé hiển thị (cache _profile, chỉ tra bé thiếu) ---
    profiles = read_profile(svc, sheet_id)
    profile_limit = int(os.getenv("FIRST_LESSON_PROFILE_LIMIT") or "150")
    need = [k["uid"] for k in kept
            if not (profiles.get(k["uid"], {}).get("gvcn")
                    and profiles.get(k["uid"], {}).get("sdt"))][:profile_limit]
    if need:
        print(f"[profile] tra SĐT+GVCN cho {len(need)} bé còn thiếu", flush=True)
        got = 0
        with httpx.Client() as pclient:
            hdrs = get_auth_headers()
            for i, uid in enumerate(need):
                if i and i % batch_size == 0:
                    hdrs = get_auth_headers()  # token tươi mỗi lô
                info = fetch_profile(pclient, hdrs, uid)
                if info is not None:
                    profiles[uid] = info
                    if info.get("gvcn") or info.get("sdt"):
                        got += 1
                time.sleep(0.3)  # leadsphone/get (hiện số) bị rate-limit nếu gọi dồn
        print(f"[profile] lấy được {got}/{len(need)}", flush=True)
    # prune cache về đúng pool hiện tại cho gọn, rồi ghi lại
    profiles = {u: v for u, v in profiles.items() if u in new_pool}
    write_profile(svc, sheet_id, profiles)

    write_pool(svc, sheet_id, new_pool)
    write_sheet(svc, sheet_id, tabs, kept, profiles)
    write_log(svc, sheet_id, tabs, kept, today, profiles)
    write_health(svc, sheet_id, "OK",
                 f"Quét {len(to_scan)} bé · pool còn {len(new_pool)} đang chờ · "
                 f"{kept_today} bé có buổi hôm nay.")
    print("[done] Ghi sheet xong.")


if __name__ == "__main__":
    main()
