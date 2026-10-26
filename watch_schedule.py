#!/usr/bin/env python3
"""
Theo dõi lịch học bất thường trên PalFish CRM → ghi Google Sheet có cảnh báo.

Luật phát hiện: trong CÙNG 1 ngày, học viên/PH TỰ ĐẶT (create_by="STUDENT")
>= MIN_CLUSTER buổi mà các giờ bắt đầu cách nhau <= GAP_MINUTES phút
(các buổi nối chuỗi liền kề) -> cờ bất thường.

Nguồn auth: token CRM lưu mã hoá ở Supabase (bảng crm_tokens, id=1), refresh
tự động mỗi khi CS mở CRM qua extension. Script giải mã y hệt backend
(crm_routes.py) rồi gọi API appoint/formal/list.

Chạy định kỳ qua GitHub Actions (cron 15'). Không đụng hạ tầng ECS/Render.

Env cần:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, CRM_ENCRYPT_KEY   -> đọc token CRM
  GOOGLE_SA_JSON (nội dung JSON) hoặc GOOGLE_SERVICE_ACCOUNT_JSON (đường dẫn file)
  GOOGLE_SHEET_ID                                            -> sheet đích (share cho SA)
  CRM_COOKIE (tuỳ chọn)  -> cookie thủ công để test local khi không có CRM_ENCRYPT_KEY
"""
from __future__ import annotations

import json
import os
import re
import sys
import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
APPOINT_URL = "https://sea.pri.ibanyu.com/opapi/crmi18n/crmopapii18n/appoint/formal/list"
SHEET_ALERT = "Cảnh báo vận hành"
SHEET_TRACK = "Theo dõi"

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config() -> dict:
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    # Cho phép override watchlist bằng secret WATCHLIST_JSON (repo public không lộ khách)
    raw = os.getenv("WATCHLIST_JSON", "").strip()
    if raw:
        try:
            cfg["watchlist"] = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.exit(f"[ERR] WATCHLIST_JSON không phải JSON hợp lệ: {exc}")
    return cfg


# ---------------------------------------------------------------------------
# Auth: lấy cookie/headers CRM (Supabase token hoặc CRM_COOKIE local)
# ---------------------------------------------------------------------------
def _decrypt(raw: str, key: str) -> str:
    if not key or not raw:
        return raw
    try:
        return Fernet(key.encode()).decrypt(raw.encode()).decode()
    except Exception:
        return raw


def _parse_bundle(raw: str) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                return {"cookie": str(d.get("cookie") or ""), "headers": d.get("headers") or {}}
        except json.JSONDecodeError:
            pass
    return {"cookie": raw, "headers": {}}


def get_auth_headers() -> dict:
    cookie_env = os.getenv("CRM_COOKIE", "").strip()
    if cookie_env:
        bundle = {"cookie": cookie_env, "headers": {}}
    else:
        from supabase import create_client

        url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        enc = os.getenv("CRM_ENCRYPT_KEY", "").strip()
        if not (url and key):
            sys.exit("[ERR] Thiếu SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        sb = create_client(url, key)
        res = sb.table("crm_tokens").select("cookie_value,updated_at").eq("id", 1).limit(1).execute()
        if not res.data:
            sys.exit("[ERR] Không có token CRM trong crm_tokens (id=1)")
        raw = _decrypt(res.data[0].get("cookie_value", ""), enc)
        bundle = _parse_bundle(raw)
        if not bundle["cookie"]:
            sys.exit("[ERR] Giải mã token CRM thất bại — kiểm tra CRM_ENCRYPT_KEY")
        print(f"[auth] token CRM cập nhật lúc {res.data[0].get('updated_at')}")

    cookie = bundle["cookie"]
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://sea.pri.ibanyu.com/kid-oversea-crm/aftersale",
        "Origin": "https://sea.pri.ibanyu.com",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
        ),
        "subject": "0",
    }
    skip = {"cookie", "content-length", "host", "content-type", "accept"}
    for k, v in (bundle["headers"] or {}).items():
        if v in (None, "") or k.lower() in skip:
            continue
        headers[k] = str(v)
    if "token" not in {k.lower() for k in headers}:
        m = re.search(r"(?:^|;\s*)token=([^;]+)", cookie)
        if m:
            headers["token"] = m.group(1)
    return headers


# ---------------------------------------------------------------------------
# Fetch lịch học 1 học viên
# ---------------------------------------------------------------------------
def fetch_lessons(client: httpx.Client, headers: dict, uid: int, cfg: dict) -> list[dict]:
    today = dt.datetime.now(VN_TZ).date()
    begin = today - dt.timedelta(days=cfg.get("date_window_days_back", 14))
    end = today + dt.timedelta(days=cfg.get("date_window_days_ahead", 60))
    body = {
        "stuid": uid,
        "kids": [],
        "action": "",
        "country_code": "VN",
        "local_zone_name": "Asia/Ho_Chi_Minh",
        "local_begin_time_desc": f"{begin} 00:00:00",
        "local_end_time_desc": f"{end} 23:59:59",
        "limit": 200,
        "offset": 0,
        "zone_name": "Asia/Saigon",
        "h_lc": "vi",
    }
    # Retry: token CRM được extension xoay ~mỗi phút; ngay khoảnh khắc xoay,
    # một request có thể trả ret=-1 dù token hợp lệ. Thử lại vài lần.
    import time
    last = None
    for attempt in range(3):
        r = client.post(APPOINT_URL, headers=headers, json=body, timeout=60)
        r.raise_for_status()
        j = r.json()
        if j.get("ret") == 1:
            return ((j.get("data") or {}).get("ent") or {}).get("items") or []
        last = j.get("ret")
        if attempt < 2:
            time.sleep(6)
    raise RuntimeError(f"API trả lỗi cho uid={uid} sau 3 lần thử: ret={last}")


# ---------------------------------------------------------------------------
# Chuẩn hoá + luật phát hiện
# ---------------------------------------------------------------------------
def _creator_label(create_by: str) -> str:
    if create_by == "STUDENT":
        return "Học viên/PH tự đặt"
    if "自动预约" in (create_by or ""):
        return "Hệ thống tự đặt (lịch cố định)"
    return create_by or "—"


# Mã action -> nhãn trạng thái (đối chiếu trực tiếp với CRM, xem README)
ACTION_STATUS = {
    1: "Chờ lên lớp",
    2: "Đã kết thúc lớp",
    98: "Học sinh huỷ",
    99: "Giáo viên huỷ",
    101: "Học sinh vắng",
}


def _status_label(item: dict, now_ts: int) -> str:
    a = item.get("action")
    label = ACTION_STATUS.get(a)
    if label is None:  # mã chưa biết -> fallback an toàn, không bịa nhãn
        if item.get("cancel_time"):
            label = "Đã huỷ"
        elif (item.get("stamp") or 0) > now_ts:
            label = "Chờ lên lớp"
        else:
            label = f"Khác (mã {a})"
    ct = item.get("cancel_time")
    if ct and a in (98, 99):  # huỷ -> kèm thời điểm huỷ
        label += " (" + dt.datetime.fromtimestamp(ct, VN_TZ).strftime("%Y-%m-%d %H:%M") + ")"
    return label


def normalize(items: list[dict]) -> list[dict]:
    now_ts = int(dt.datetime.now(tz=dt.timezone.utc).timestamp())
    rows = []
    for it in items:
        ld = it.get("local_time_desc") or ""  # "2026-10-21 16:00(Viet Nam)"
        m = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})", ld)
        if not m:
            continue
        date_s, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
        ct = it.get("create_time") or 0
        rows.append({
            "uid": (it.get("student") or {}).get("uid"),
            "ten": (it.get("student") or {}).get("name") or "",
            "date": date_s,
            "mins": hh * 60 + mm,
            "hhmm": f"{hh:02d}:{mm:02d}",
            "create_by": it.get("create_by") or "",
            "creator": _creator_label(it.get("create_by") or ""),
            "status": _status_label(it, now_ts),
            "created_at": (dt.datetime.fromtimestamp(ct, VN_TZ).strftime("%Y-%m-%d %H:%M")
                           if ct else "—"),
            "created_ts": ct,
            "lessonid": it.get("lessonid"),
            "teacher": (it.get("teacher") or {}).get("name") or "",
            "flag": False,
        })
    return rows


def detect_clusters(rows: list[dict], gap_minutes: int, min_cluster: int) -> list[dict]:
    """Nối chuỗi các buổi TỰ ĐẶT cùng ngày cách nhau <= gap. Trả list cụm bất thường."""
    by_day = defaultdict(list)
    for r in rows:
        if r["create_by"] == "STUDENT":
            by_day[r["date"]].append(r)

    clusters = []
    for date_s, lst in by_day.items():
        lst.sort(key=lambda r: r["mins"])
        chain = [lst[0]]
        for prev, cur in zip(lst, lst[1:]):
            if cur["mins"] - prev["mins"] <= gap_minutes:
                chain.append(cur)
            else:
                if len(chain) >= min_cluster:
                    clusters.append(chain)
                chain = [cur]
        if len(chain) >= min_cluster:
            clusters.append(chain)

    for chain in clusters:
        for r in chain:
            r["flag"] = True
    return clusters


# ---------------------------------------------------------------------------
# Google Sheet
# ---------------------------------------------------------------------------
def build_sheets_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    raw = os.getenv("GOOGLE_SA_JSON", "").strip()
    if raw:
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if not path:
            sys.exit("[ERR] Thiếu GOOGLE_SA_JSON hoặc GOOGLE_SERVICE_ACCOUNT_JSON")
        creds = Credentials.from_service_account_file(path, scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _ensure_tabs(svc, sheet_id: str) -> dict:
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets", [])}
    reqs = []
    for title in (SHEET_ALERT, SHEET_TRACK):
        if title not in existing:
            reqs.append({"addSheet": {"properties": {"title": title}}})
    if reqs:
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": reqs}).execute()
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        existing = {s["properties"]["title"]: s["properties"]["sheetId"]
                    for s in meta.get("sheets", [])}
    return existing


def write_sheet(sheet_id: str, rows: list[dict], clusters: list[dict], cfg: dict):
    svc = build_sheets_service()
    tabs = _ensure_tabs(svc, sheet_id)
    now_vn = dt.datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    has_alert = bool(clusters)

    # ----- Tab Cảnh báo vận hành -----
    banner = ("⚠️ CÓ BẤT THƯỜNG — KIỂM TRA & XỬ LÝ NGAY"
              if has_alert else "✅ BÌNH THƯỜNG — không có đặt lịch bất thường")
    alert_values = [
        [banner],
        [f"Cập nhật lúc: {now_vn} (giờ VN) · quét mỗi 15 phút"],
        [],
        ["Tên bé", "UID", "Ngày học", "Số buổi tự đặt", "Các giờ (VN)", "Đặt lúc", "Trạng thái"],
    ]
    for chain in sorted(clusters, key=lambda c: c[0]["date"], reverse=True):
        gio = ", ".join(r["hhmm"] for r in sorted(chain, key=lambda r: r["mins"]))
        statuses = sorted({r["status"] for r in chain})
        alert_values.append([
            chain[0]["ten"], str(chain[0]["uid"]), chain[0]["date"],
            len(chain), gio, chain[0]["created_at"], " / ".join(statuses),
        ])
    if not clusters:
        alert_values.append(["— không có —"])

    # ----- Tab Theo dõi (toàn bộ buổi tự đặt, mới nhất trước) -----
    track_header = ["Tên bé", "UID", "Ngày học", "Giờ (VN)", "Người đặt",
                    "Trạng thái", "Cờ", "Đặt lúc", "Giáo viên", "Lesson ID"]
    self_booked = [r for r in rows if r["create_by"] == "STUDENT"]
    self_booked.sort(key=lambda r: (r["date"], r["mins"]), reverse=True)
    track_values = [track_header]
    for r in self_booked:
        track_values.append([
            r["ten"], str(r["uid"]), r["date"], r["hhmm"], r["creator"],
            r["status"], "⚠️" if r["flag"] else "", r["created_at"],
            r["teacher"], str(r["lessonid"]),
        ])

    # ----- Ghi values (clear vùng cũ trước) -----
    svc.spreadsheets().values().batchClear(
        spreadsheetId=sheet_id,
        body={"ranges": [f"'{SHEET_ALERT}'!A1:Z1000", f"'{SHEET_TRACK}'!A1:Z2000"]},
    ).execute()
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": [
            {"range": f"'{SHEET_ALERT}'!A1", "values": alert_values},
            {"range": f"'{SHEET_TRACK}'!A1", "values": track_values},
        ]},
    ).execute()

    # ----- Định dạng: banner màu + freeze + cờ đỏ -----
    aid, tid = tabs[SHEET_ALERT], tabs[SHEET_TRACK]
    red = {"red": 0.96, "green": 0.80, "blue": 0.80}
    green = {"red": 0.80, "green": 0.92, "blue": 0.80}
    fmt_reqs = [
        # banner A1: merge + màu theo trạng thái + in đậm
        {"mergeCells": {"range": {"sheetId": aid, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 0, "endColumnIndex": 7}, "mergeType": "MERGE_ALL"}},
        {"repeatCell": {
            "range": {"sheetId": aid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 7},
            "cell": {"userEnteredFormat": {
                "backgroundColor": red if has_alert else green,
                "textFormat": {"bold": True, "fontSize": 13},
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"}},
        # header rows đậm + freeze
        {"repeatCell": {"range": {"sheetId": aid, "startRowIndex": 3, "endRowIndex": 4},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat"}},
        {"repeatCell": {"range": {"sheetId": tid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat"}},
        {"updateSheetProperties": {"properties": {"sheetId": tid, "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
        # xoá conditional format cũ (index 0) rồi thêm lại: cờ "⚠️" -> cả dòng đỏ nhạt
        {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": tid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 10}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": '=$G2="⚠️"'}]},
                "format": {"backgroundColor": red}}}}},
    ]
    try:
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": fmt_reqs}).execute()
    except Exception as exc:  # định dạng lỗi không chặn việc ghi data
        print(f"[warn] format lỗi (bỏ qua): {exc}")

    return has_alert


# ---------------------------------------------------------------------------
def main():
    cfg = load_config()
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip() or cfg.get("google_sheet_id", "").strip()
    if not sheet_id:
        sys.exit("[ERR] Chưa có GOOGLE_SHEET_ID (env hoặc config.json)")

    headers = get_auth_headers()
    all_rows, all_clusters = [], []
    with httpx.Client() as client:
        for w in cfg["watchlist"]:
            uid = int(w["uid"])
            items = fetch_lessons(client, headers, uid, cfg)
            rows = normalize(items)
            clusters = detect_clusters(rows, cfg.get("gap_minutes", 60), cfg.get("min_cluster", 2))
            print(f"[{w.get('ten', uid)}] {len(rows)} buổi, "
                  f"{sum(1 for r in rows if r['create_by']=='STUDENT')} tự đặt, "
                  f"{len(clusters)} cụm bất thường")
            all_rows.extend(rows)
            all_clusters.extend(clusters)

    has_alert = write_sheet(sheet_id, all_rows, all_clusters, cfg)
    print(f"[done] Ghi sheet xong. Trạng thái: {'CÓ BẤT THƯỜNG' if has_alert else 'bình thường'}")


if __name__ == "__main__":
    main()
