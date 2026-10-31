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

SHEET_CARE = "Cần chăm tuần này"
_OLD_CARE_TAB = "Cần chăm hôm nay"   # tên cũ (daily) -> rename sang SHEET_CARE
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
CRM_WINDOW_AHEAD_DAYS = 7   # đủ phủ tới CN tuần này (bé có buổi muộn trong tuần)
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


# loai = cờ gia hạn trong so_doanh_thu. 续费 (6100 đơn) + Resell (297) = tái ký.
# Bé mới KHÔNG bao giờ có đơn loại này (xác nhận 10/10 UID IH1). Dùng để bịt cả
# edge case: bé gia hạn mà đơn gốc không có trong sổ thì chính đơn 续费 vẫn bị cờ.
RENEWAL_LOAI = {"续费", "Resell"}


def _is_test_order(o: dict) -> bool:
    return o.get("is_test") in (True, "true", "True", 1, "1")


def renewal_uids_from(orders: list[dict]) -> set[str]:
    """Tập uid từng có đơn gia hạn (loai ∈ 续费/Resell) -> KHÔNG phải bé mới."""
    out: set[str] = set()
    for o in orders:
        if _is_test_order(o):
            continue
        uid = _clean_uid(o.get("uid"))
        if uid and str(o.get("loai") or "").strip() in RENEWAL_LOAI:
            out.add(uid)
    return out


def select_new_candidates(
    orders: list[dict],
    now: dt.datetime,
    lookback_days: int,
    same_buy_minutes: int,
) -> dict[str, dt.datetime]:
    """
    orders: [{uid, pay_time, is_test, loai}] — TOÀN BỘ lịch sử (không chỉ gần đây).
    Trả {uid -> purchase_time} cho học viên "mới" mua trong lookback_days.

    "mới" = (a) KHÔNG có đơn gia hạn nào (loai ∉ 续费/Resell), VÀ
            (b) mọi đơn nằm trong cùng 1 cửa sổ <= same_buy_minutes
                (1 đơn, hoặc nhiều đơn cùng lần mua). Có đơn ở lần mua khác -> loại.
    """
    renewal = renewal_uids_from(orders)
    by_uid: dict[str, list[dt.datetime]] = defaultdict(list)
    for o in orders:
        if _is_test_order(o):
            continue
        uid = _clean_uid(o.get("uid"))
        pt = _parse_pay_time(o.get("pay_time"))
        if uid and pt:
            by_uid[uid].append(pt)

    window = dt.timedelta(minutes=same_buy_minutes)
    earliest_ok = now - dt.timedelta(days=lookback_days)
    out: dict[str, dt.datetime] = {}
    for uid, times in by_uid.items():
        if uid in renewal:          # từng gia hạn -> không phải bé mới
            continue
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


def count_completed_before(items: list[dict], purchase: dt.datetime,
                           before_date: dt.date) -> int:
    """Buổi đã học chính thức (action==2) sau khi mua NHƯNG trước mốc `before_date`.

    Dùng để loại bé ĐÃ học buổi chính thức từ trước tuần này (không phải bé buổi
    đầu) — tránh danh sách phình ra toàn bé đang học lâu rồi có buổi trong tuần."""
    n = 0
    for it in items:
        if it.get("action") != ACTION_COMPLETED:
            continue
        ldt = _lesson_dt(it)
        if ldt and ldt > purchase and ldt.date() < before_date:
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


def week_bounds(today: dt.date) -> tuple[dt.date, dt.date]:
    """Thứ 2 -> Chủ nhật của tuần chứa `today` (VN). weekday(): T2=0..CN=6."""
    monday = today - dt.timedelta(days=today.weekday())
    return monday, monday + dt.timedelta(days=6)


def week_lessons(items: list[dict], monday: dt.date, sunday: dt.date) -> list[dict]:
    """Buổi học trong TUẦN này [monday..sunday] (mọi trạng thái), sắp theo ngày+giờ."""
    rows = []
    for it in items:
        ldt = _lesson_dt(it)
        if not ldt or not (monday <= ldt.date() <= sunday):
            continue
        a = it.get("action")
        rows.append({
            "date": ldt.strftime("%Y-%m-%d"),
            "dkey": ldt.date().toordinal() * 10000 + ldt.hour * 60 + ldt.minute,
            "hhmm": ldt.strftime("%H:%M"),
            "status": ACTION_STATUS.get(a, f"Khác (mã {a})"),
            "teacher": (it.get("teacher") or {}).get("name") or "",
        })
    rows.sort(key=lambda r: r["dkey"])
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
               .select("uid, pay_time, is_test, loai")
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
    # Migrate: đổi tên tab cũ "Cần chăm hôm nay" -> "Cần chăm tuần này" (giữ ghi chú CS)
    if _OLD_CARE_TAB in existing and SHEET_CARE not in existing:
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [
            {"updateSheetProperties": {
                "properties": {"sheetId": existing[_OLD_CARE_TAB], "title": SHEET_CARE},
                "fields": "title"}}]}).execute()
        existing[SHEET_CARE] = existing.pop(_OLD_CARE_TAB)
    reqs = []
    for t in (SHEET_CARE, SHEET_WAITING, LOG_TAB, POOL_TAB, HEALTH_TAB, PROFILE_TAB):
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
            spreadsheetId=sheet_id, range=f"'{PROFILE_TAB}'!A2:D").execute()
    except Exception:
        return {}
    prof: dict[str, dict] = {}
    for row in res.get("values", []):
        uid = _clean_uid(row[0]) if row else None
        if uid:
            tries = row[3] if len(row) > 3 else "0"
            prof[uid] = {"gvcn": (row[1] if len(row) > 1 else "") or "",
                         "sdt": (row[2] if len(row) > 2 else "") or "",
                         "sdt_tries": int(tries) if str(tries).strip().isdigit() else 0}
    return prof


def write_profile(svc, sheet_id: str, profiles: dict[str, dict]):
    values = [["uid", "gvcn", "sdt", "sdt_tries"]]
    for uid, v in sorted(profiles.items()):
        values.append([uid, v.get("gvcn", ""), v.get("sdt", ""), v.get("sdt_tries", 0)])
    svc.spreadsheets().values().batchClear(
        spreadsheetId=sheet_id, body={"ranges": [f"'{PROFILE_TAB}'!A1:D20000"]}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{PROFILE_TAB}'!A1",
        valueInputOption="RAW", body={"values": values}).execute()


def fetch_gvcn(client, headers, uid) -> str | None:
    """GVCN = leads_info.head_teacher.name (leadsinfo/get) — KHÁC sale (NV bán hàng).
    Trả tên GVCN ('' nếu chưa gán), hoặc None nếu lỗi/token (thử lại lượt sau)."""
    try:
        body = {"uid": int(uid), "zone_name": "Asia/Saigon", "h_lc": "vi"}
        j = client.post(LEADSINFO_URL, headers=headers, json=body, timeout=30).json()
        if j.get("ret") != 1:
            return None
        li = ((j.get("data") or {}).get("ent") or {}).get("leads_info") or {}
        return ((li.get("head_teacher") or {}).get("name") or "").strip()
    except Exception:
        return None


def fetch_phone(client, headers, uid) -> str | None:
    """SĐT đầy đủ = data.ent.phone (leadsphone/get; leadsinfo che bớt số nên không dùng).

    Trả '' nếu CRM nói không có số; None nếu lỗi hoặc **bị giới hạn tần suất**
    ("Too many phone number queries") — caller đếm None để ngắt, tránh gọi dồn
    làm CRM khoá cổng.
    """
    try:
        body = {"uid": int(uid), "zone_name": "Asia/Saigon", "h_lc": "vi"}
        j = client.post(LEADSPHONE_URL, headers=headers, json=body, timeout=30).json()
        if j.get("ret") != 1:
            return None
        return (((j.get("data") or {}).get("ent") or {}).get("phone") or "").strip()
    except Exception:
        return None


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


CS_STATUS_HDR, CS_NOTE_HDR = "Trạng thái CS", "Ghi chú CS"


def read_cs_notes(svc, sheet_id: str, tab: str, header_row1: int,
                  data_row1: int) -> dict[str, list]:
    """Đọc 2 cột CS tự điền -> {uid: [trạng_thái, ghi_chú]}.

    Dò cột theo TÊN HEADER ("UID", "Trạng thái CS", "Ghi chú CS") chứ KHÔNG dùng
    chỉ số cứng — nên đổi layout (thêm/bớt cột) không làm lệch/mất ghi chú CS.
    Neo theo UID nên ghi chú luôn theo đúng bé dù dòng sắp xếp lại.
    """
    try:
        res = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!A{header_row1}:Z").execute()
    except Exception:
        return {}
    vals = res.get("values", [])
    if not vals:
        return {}
    header = [str(h).strip() for h in vals[0]]

    def _col(name):
        return header.index(name) if name in header else None

    ci_uid, ci_st, ci_nt = _col("UID"), _col(CS_STATUS_HDR), _col(CS_NOTE_HDR)
    if ci_uid is None or (ci_st is None and ci_nt is None):
        return {}
    out: dict[str, list] = {}
    for row in vals[(data_row1 - header_row1):]:
        uid = _clean_uid(row[ci_uid]) if len(row) > ci_uid else None
        if not uid:
            continue
        st = row[ci_st] if (ci_st is not None and len(row) > ci_st) else ""
        nt = row[ci_nt] if (ci_nt is not None and len(row) > ci_nt) else ""
        if st or nt:
            out[uid] = [st, nt]
    return out


def write_sheet(svc, sheet_id, tabs, kept, profiles, care_cs, wait_cs):
    """kept: [{uid, ten, purchase, today_rows, week_rows, completed}].
    care_cs/wait_cs: {uid: [trạng_thái, ghi_chú]} ghi chú CS đã đọc (neo UID) -> ghi lại."""
    now_vn = dt.datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")

    def _sdt(uid):
        return (profiles.get(uid) or {}).get("sdt", "")

    def _gvcn(uid):
        return (profiles.get(uid) or {}).get("gvcn", "")

    def _cs(notes, uid):
        return notes.get(uid, ["", ""])

    monday, sunday = week_bounds(dt.datetime.now(VN_TZ).date())
    with_week = [k for k in kept if k.get("week_rows")]

    # ----- Tab Cần chăm tuần này (T2–CN) -----
    wk = f"T2 {monday.strftime('%d/%m')} – CN {sunday.strftime('%d/%m')}"
    banner = (f"🔔 {len(with_week)} học viên có buổi đầu TUẦN NÀY ({wk}) — CS chuẩn bị chăm"
              if with_week else f"✅ Tuần này ({wk}) chưa có học viên mới tới buổi học")
    care_values = [
        [banner],
        [f"Cập nhật lúc: {now_vn} (giờ VN) · quét mỗi 30 phút · 8h30–22h · mốc T2–CN"],
        [],
        ["Tên bé", "UID", "SĐT", "GVCN", "Ngày học", "Giờ học (VN)", "GV dạy buổi",
         "Trạng thái", "Ngày mua"] + CS_COLS,
    ]
    for k in sorted(with_week, key=lambda k: k["week_rows"][0]["dkey"]):
        wr = k["week_rows"]
        ngay = ", ".join(dict.fromkeys(r["date"] for r in wr))   # ngày distinct, giữ thứ tự
        gio = ", ".join(r["hhmm"] for r in wr)
        gv = " / ".join(sorted({r["teacher"] for r in wr if r["teacher"]}))
        tt = " / ".join(sorted({r["status"] for r in wr}))
        care_values.append([k["ten"], k["uid"], _sdt(k["uid"]), _gvcn(k["uid"]),
                            ngay, gio, gv, tt, k["purchase"].strftime("%Y-%m-%d")]
                           + _cs(care_cs, k["uid"]))
    if not with_week:
        care_values.append(["— không có —"])

    # ----- Tab Danh sách chờ (bé mới CHƯA học buổi nào) -----
    waiting = [k for k in kept if not k.get("completed")]   # bỏ bé đã học xong buổi đầu
    wait_header = ["Tên bé", "UID", "SĐT", "GVCN", "Ngày mua",
                   "Có buổi hôm nay?", "Giờ hôm nay"] + CS_COLS
    wait_values = [wait_header]
    for k in sorted(waiting, key=lambda k: k["purchase"], reverse=True):
        gio = ", ".join(r["hhmm"] for r in k["today_rows"]) if k["today_rows"] else ""
        wait_values.append([k["ten"], k["uid"], _sdt(k["uid"]), _gvcn(k["uid"]),
                            k["purchase"].strftime("%Y-%m-%d"),
                            "Có" if k["today_rows"] else "", gio]
                           + _cs(wait_cs, k["uid"]))
    if not waiting:
        wait_values.append(["— không có học viên mới chưa học —"])

    svc.spreadsheets().values().batchClear(
        spreadsheetId=sheet_id,
        body={"ranges": [f"'{SHEET_CARE}'!A1:Z1000", f"'{SHEET_WAITING}'!A1:Z3000"]},
    ).execute()
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": [
            {"range": f"'{SHEET_CARE}'!A1", "values": care_values},
            {"range": f"'{SHEET_WAITING}'!A1", "values": wait_values},
        ]},
    ).execute()

    # ----- Định dạng banner + header + freeze -----
    tid, wid = tabs[SHEET_CARE], tabs[SHEET_WAITING]
    blue = {"red": 0.82, "green": 0.89, "blue": 0.98}
    green = {"red": 0.80, "green": 0.92, "blue": 0.80}
    fmt = [
        # bỏ merge cũ trước (lần trước banner merge A–F; nay A–H) để khỏi xung đột
        {"unmergeCells": {"range": {"sheetId": tid, "startRowIndex": 0, "endRowIndex": 1,
                                    "startColumnIndex": 0, "endColumnIndex": 26}}},
        {"mergeCells": {"range": {"sheetId": tid, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 0, "endColumnIndex": 11}, "mergeType": "MERGE_ALL"}},
        {"repeatCell": {
            "range": {"sheetId": tid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 11},
            "cell": {"userEnteredFormat": {
                "backgroundColor": blue if with_week else green,
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


def _week_log_row(k, profiles, care):
    """1 dòng log cho bé: Ngày = buổi SỚM NHẤT trong tuần (buổi đầu), kèm ghi chú CS."""
    wr = k["week_rows"]
    d0 = wr[0]["date"]
    same = [r for r in wr if r["date"] == d0]
    gio = ", ".join(r["hhmm"] for r in same)
    gv = " / ".join(sorted({r["teacher"] for r in same if r["teacher"]}))
    tt = " / ".join(sorted({r["status"] for r in same}))
    p = profiles.get(k["uid"], {})
    cs = care.get(k["uid"], ["", ""])
    return [d0, k["ten"], k["uid"], p.get("sdt", ""), p.get("gvcn", ""),
            gio, gv, tt, cs[0], cs[1]]


def build_log_rows(old_rows, students, profiles, care):
    """Dòng log cũ (giữ, CẬP NHẬT ghi chú CS mới nhất theo UID) + dòng mới (dedup
    theo (Ngày buổi đầu, UID), bé mới lên đầu). Pure. Cắt còn LOG_MAX.

    care = {uid: [trạng_thái, ghi_chú]} từ tab chăm -> SNAPSHOT vào log để giữ lịch
    sử CS dù bé đã rời tab chăm (hết tuần)."""
    width = len(LOG_HEADER)
    seen, kept_old = set(), []
    for r in old_rows:
        r = (list(r) + [""] * width)[:width]
        uid = _clean_uid(r[2])
        if uid:
            seen.add((r[0], uid))
            cs = care.get(uid)
            if cs and (cs[0] or cs[1]):      # có ghi chú CS mới -> cập nhật vào dòng log
                r[8], r[9] = cs[0], cs[1]
        kept_old.append(r)
    new_rows = []
    for k in sorted(students, key=lambda k: k["week_rows"][0]["dkey"]):
        key = (k["week_rows"][0]["date"], k["uid"])
        if key in seen:
            continue
        seen.add(key)
        new_rows.append(_week_log_row(k, profiles, care))
    return (new_rows + kept_old)[:LOG_MAX]


def write_log(svc, sheet_id, tabs, kept, profiles, care):
    students = [k for k in kept if k.get("week_rows")]
    try:
        res = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{LOG_TAB}'!A2:J").execute()
        old = res.get("values", [])
    except Exception:
        old = []
    rows = build_log_rows(old, students, profiles, care)
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
    monday, sunday = week_bounds(today)   # mốc T2–CN tuần này

    svc = build_sheets_service()
    tabs = _ensure_tabs(svc, sheet_id)
    pool = read_pool(svc, sheet_id)
    print(f"[pool] {len(pool)} bé đang chờ (từ lần chạy trước)")

    # --- Lớp 1: nạp bé MỚI mua gần đây, chưa có trong pool ---
    orders = load_orders()
    renewal = renewal_uids_from(orders)   # uid từng gia hạn (loai 续费/Resell)
    candidates = select_new_candidates(orders, now, arrival_days, same_buy)
    new_uids = {u: p for u, p in candidates.items() if u not in pool}
    print(f"[arrival] {len(new_uids)} bé mới (mua trong {arrival_days} ngày) cần nạp")

    # Dọn cả bé gia hạn LỌT VÀO pool từ lần chạy cũ (trước khi có luật loai).
    pool_clean = {u: v for u, v in pool.items() if u not in renewal}
    if len(pool) != len(pool_clean):
        print(f"[renewal] bỏ {len(pool) - len(pool_clean)} bé gia hạn khỏi pool", flush=True)

    # purchase_time cho từng uid cần quét: pool giữ mốc cũ, bé mới lấy từ candidates
    scan: dict[str, dt.datetime] = {u: v["purchase"] for u, v in pool_clean.items()}
    scan.update(new_uids)

    # --- Lớp 2: quét CRM CHỈ các uid trong (pool ∪ bé mới) ---
    # BACK phủ bé tồn lâu nhất (pool_max_days) để đếm đúng buổi đã học; AHEAD=7 phủ
    # hết tuần (tới CN). Tổng pool_max_days(45)+7 = 52 ngày, DƯỚI ngưỡng CRM ~54
    # (vượt là ret=-1). Nếu nới pool_max_days nhiều thì phải canh lại tổng < ~54.
    crm_cfg = {"date_window_days_back": pool_max_days,
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
        wk = week_lessons(items, monday, sunday)
        # Đã học buổi chính thức TỪ TRƯỚC tuần này -> không phải bé "buổi đầu" nữa
        # (bé đang học lâu rồi, vẫn có buổi trong tuần) -> LOẠI, tránh phình list.
        if count_completed_before(items, purchase, monday) >= 1:
            return
        # Còn lại: chưa học buổi nào trước tuần này. Buổi đầu rơi trong/sau tuần này.
        # Nếu đã học xong buổi đầu TRONG tuần này -> vẫn giữ (CS review cả tuần).
        completed = count_completed_after(items, purchase) >= 1
        ten = (pool.get(uid, {}).get("ten")
               or next((((it.get("student") or {}).get("name") or "")
                        for it in items if (it.get("student") or {}).get("name")), ""))
        new_pool[uid] = {"ten": ten, "purchase": purchase}
        kept.append({"uid": uid, "ten": ten, "purchase": purchase, "completed": completed,
                     "today_rows": todays_lessons(items, today),
                     "week_rows": wk})

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
    kept_week = sum(1 for k in kept if k.get("week_rows"))
    print(f"[kept] pool còn {len(new_pool)} bé · {kept_week} bé có buổi tuần này "
          f"({kept_today} hôm nay)")

    # --- Lớp 3: bổ sung GVCN (leadsinfo) + SĐT (leadsphone) ---
    # ⚠️ CRM giới hạn tần suất tra SĐT ("Too many phone number queries") và DOẠ
    # KHOÁ cổng nếu gọi dồn. Nên tra SĐT rất dè:
    #   - ưu tiên bé CÓ BUỔI HÔM NAY (CS cần gọi ngay), rồi ít bé khác;
    #   - cap cứng PHONE_LIMIT/lượt, nghỉ 1s/lần, tối đa PHONE_MAX_TRIES lần/bé;
    #   - gặp giới hạn (None liên tiếp) -> NGẮT tra SĐT lượt này.
    # GVCN (leadsinfo) nhẹ hơn, tra rộng hơn chút.
    profiles = read_profile(svc, sheet_id)
    gvcn_limit = int(os.getenv("FIRST_LESSON_GVCN_LIMIT") or "50")
    phone_limit = int(os.getenv("FIRST_LESSON_PHONE_LIMIT") or "25")
    phone_max_tries = int(os.getenv("FIRST_LESSON_PHONE_MAX_TRIES") or "5")

    def _p(uid):
        return profiles.setdefault(uid, {"gvcn": "", "sdt": "", "sdt_tries": 0})

    need_gvcn = [k["uid"] for k in kept if not _p(k["uid"])["gvcn"]][:gvcn_limit]
    week_uids = [k["uid"] for k in kept if k.get("week_rows")]   # bé hiện trong tab tuần
    other_uids = [k["uid"] for k in sorted(kept, key=lambda k: k["purchase"], reverse=True)
                  if not k.get("week_rows")]
    need_phone = [u for u in week_uids + other_uids
                  if not _p(u)["sdt"] and _p(u).get("sdt_tries", 0) < phone_max_tries][:phone_limit]

    if need_gvcn or need_phone:
        print(f"[profile] GVCN {len(need_gvcn)} bé · SĐT {len(need_phone)} bé "
              f"(ưu tiên {len(week_uids)} bé có buổi tuần này)", flush=True)
        g_ok = p_ok = 0
        with httpx.Client() as pclient:
            hdrs = get_auth_headers()
            for i, uid in enumerate(need_gvcn):
                if i and i % batch_size == 0:
                    hdrs = get_auth_headers()
                gv = fetch_gvcn(pclient, hdrs, uid)
                if gv is not None:
                    _p(uid)["gvcn"] = gv
                    g_ok += 1 if gv else 0
                time.sleep(0.3)
            # đợt SĐT: token tươi, nghỉ lâu, có circuit-breaker
            hdrs = get_auth_headers()
            consec_fail = 0
            for i, uid in enumerate(need_phone):
                if i and i % batch_size == 0:
                    hdrs = get_auth_headers()
                ph = fetch_phone(pclient, hdrs, uid)
                _p(uid)["sdt_tries"] = _p(uid).get("sdt_tries", 0) + 1
                if ph is None:
                    consec_fail += 1
                    if consec_fail >= 3:
                        print("[profile] leadsphone lỗi/giới hạn liên tục -> NGẮT tra "
                              "SĐT lượt này (tránh CRM khoá cổng)", flush=True)
                        break
                else:
                    consec_fail = 0
                    if ph:
                        _p(uid)["sdt"] = ph
                        p_ok += 1
                time.sleep(1.0)  # leadsphone bị giới hạn mạnh -> nghỉ lâu
        print(f"[profile] GVCN lấy {g_ok}/{len(need_gvcn)} · SĐT lấy {p_ok}/{len(need_phone)}",
              flush=True)
    # prune cache về đúng pool hiện tại cho gọn, rồi ghi lại
    profiles = {u: v for u, v in profiles.items() if u in new_pool}
    write_profile(svc, sheet_id, profiles)

    write_pool(svc, sheet_id, new_pool)
    # Đọc ghi chú CS 1 lần (dò cột theo tên header) rồi dùng cho cả sheet + log.
    care_cs = read_cs_notes(svc, sheet_id, SHEET_CARE, 4, 5)   # header dòng 4, data dòng 5
    wait_cs = read_cs_notes(svc, sheet_id, SHEET_WAITING, 1, 2)
    write_sheet(svc, sheet_id, tabs, kept, profiles, care_cs, wait_cs)
    write_log(svc, sheet_id, tabs, kept, profiles, care_cs)
    write_health(svc, sheet_id, "OK",
                 f"Quét {len(to_scan)} bé · pool còn {len(new_pool)} đang chờ · "
                 f"{kept_week} bé có buổi tuần này ({kept_today} hôm nay).")
    print("[done] Ghi sheet xong.")


if __name__ == "__main__":
    main()
