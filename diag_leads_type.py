#!/usr/bin/env python3
"""Chẩn đoán 1 lần: lấy leads_type (+ consume_lessons, GVCN) cho vài UID để xác
nhận giả thuyết "bé mới = leads_type 4 / bé gia hạn = 5".

Dùng chung auth CRM với watch_schedule (token crm_tokens, giải mã Fernet).
Chỉ gọi leads/leadsinfo/get (endpoint NHẸ, KHÔNG phải cổng SĐT bị giới hạn).

UID truyền qua env (dispatch input), KHÔNG hardcode data khách vào repo:
  DIAG_UIDS_NEW      = "uid1,uid2,..."   (bé bán mới)
  DIAG_UIDS_RENEWAL  = "uid1,uid2,..."   (bé gia hạn)
"""
from __future__ import annotations
import os
import time

import httpx

from watch_schedule import get_auth_headers

LEADSINFO_URL = "https://sea.pri.ibanyu.com/opapi/crmi18n/crmopapii18n/leads/leadsinfo/get"


def _uids(env_name: str) -> list[str]:
    return [u.strip() for u in os.getenv(env_name, "").replace("\n", ",").split(",") if u.strip()]


def fetch_one(client, headers, uid: str) -> str:
    try:
        body = {"uid": int(uid), "zone_name": "Asia/Saigon", "h_lc": "vi"}
        j = client.post(LEADSINFO_URL, headers=headers, json=body, timeout=30).json()
        if j.get("ret") != 1:
            return f"ret={j.get('ret')} (token hết hạn? CS mở CRM rồi chạy lại)"
        li = ((j.get("data") or {}).get("ent") or {}).get("leads_info") or {}
        gvcn = (li.get("head_teacher") or {}).get("name")
        return (f"leads_type={li.get('leads_type')!r:>4}  "
                f"consume_lessons={li.get('consume_lessons')!r:>4}  "
                f"lession_left={li.get('lession_left')!r:>4}  "
                f"last_purchase={li.get('last_purchase_time')!r}  GVCN={gvcn!r}")
    except Exception as exc:  # noqa: BLE001
        return f"ERR {exc}"


def main():
    groups = [("BÁN MỚI", _uids("DIAG_UIDS_NEW")),
              ("GIA HẠN", _uids("DIAG_UIDS_RENEWAL"))]
    with httpx.Client() as client:
        for label, uids in groups:
            print(f"\n=== {label} ({len(uids)} bé) ===", flush=True)
            if not uids:
                continue
            headers = get_auth_headers()  # token tươi cho mỗi nhóm
            for uid in uids:
                print(f"  {uid}: {fetch_one(client, headers, uid)}", flush=True)
                time.sleep(0.4)


if __name__ == "__main__":
    main()
