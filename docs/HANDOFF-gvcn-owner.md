# Handoff — GVCN để trống khi chưa gán + cột "GVCN cần gắn"

> Người nhận: Phạm Anh Minh (làm CS backend). Nên finish ở **session local** (có CS
> backend thật để test auth + có skill `/plan-first`). Doc này để bắt nhịp ngay,
> không phải code lại từ đầu.

## Yêu cầu gốc (chị Huyền / M)
1. Khi CRM **chưa gán GVCN**, `head_teacher.name` trả về tên **advisor/sale**
   (Ellie Advisor, Ruby Advisor…) → đang hiện sai. Yêu cầu: **để trống** cột GVCN.
2. Thêm cột **"GVCN cần gắn"** = CS **sở hữu** UID (từ backend) để CS lên CRM gắn bù.
   Đã chốt: không rõ owner thì **để trống** (choice A).

## Chẩn đoán đã có (quan trọng nhất)
Nguồn owner = CS app `GET {PALFISH_CS_SYNC_URL}?resource=gvcn_sync&uids=...`
header `x-cs-sync-key` → `{assignments:[{uid,gvcn}]}` (giống `backend/cs_gvcn_client.py`
của GMV).

Chạy thật trên GitHub Actions trả về:
```
[cs_owner] debug rỗng: HTTP 403 host=47.84.41.240 ct=application/json keys=['error'] body='...{"error":"cs_forbidden"}...'
```
→ **Gọi tới được nhưng bị CS app từ chối quyền (403 cs_forbidden)**. KHÔNG phải lỗi
code. 2 khả năng (M là người làm CS backend nên biết ngay):
- **IP whitelist**: CS app chỉ cho IP Render (backend GMV) gọi; IP GitHub Actions
  (nơi bot chạy) không nằm trong allowlist. (GMV backend trên Render dùng cùng
  `cs_gvcn_client` → nếu cột GVCN tab bàn giao CS vẫn điền được thì key đúng → vấn
  đề của bot là IP.)
- **Key lệch**: `PALFISH_CS_SYNC_KEY` bên GitHub khác key CS app chấp nhận.

### Hướng fix (phía CS backend, không đụng bot)
- Nếu IP: whitelist IP GitHub Actions, **hoặc** cho bot đi vòng qua backend GMV
  (Render, đã được phép): thêm endpoint `GET /api/first-lesson/owners?uids=...` ở
  GMV backend gọi `cs_gvcn_client.fetch_gvcn()` rồi trả `{uid:gvcn}`; bot trỏ sang
  đó thay vì gọi thẳng CS app. Cách này fix được CẢ IP lẫn key.
- Nếu key: sửa secret `PALFISH_CS_SYNC_KEY` của repo bot cho khớp.

## Trạng thái repo bot HIỆN TẠI
- `master` đã **revert** phần GVCN-owner về mốc sau PR #11 (sạch, không có code treo).
- **GIỮ** 2 việc chị Huyền đã nhận: PR #10 (sort buổi gần nhất lên đầu) + PR #11
  (mỗi bé 1 dòng = buổi đầu). Đang chạy tốt.
- 2 secret `PALFISH_CS_SYNC_URL` + `PALFISH_CS_SYNC_KEY` vẫn còn trong repo settings
  (không gỡ) — dùng lại khi re-activate.

## Code GVCN-owner (ĐÃ VIẾT, test 27/27) — nằm ở đâu
Toàn bộ triển khai nằm trong 3 PR đã merge rồi bị revert:
- **PR #12** `feat: GVCN để trống + cột GVCN cần gắn` (`518a51b`) — phần chính.
- **PR #13** `fix: endpoint owner trả 0 -> giữ head_teacher, không xoá cột` (`fcdc3d9`) — guard chống xoá trắng cột.
- **PR #14** `chore: log chẩn đoán` (`715ba0d`) — scaffolding, **bỏ** khi finish.

### Tái dùng ở local
```bash
git fetch origin
git checkout -b feat/gvcn-owner main   # hoặc master
git cherry-pick 518a51b fcdc3d9        # lấy lại feature + guard (BỎ 715ba0d diagnostic)
# sửa auth phía CS app, rồi chạy test:
python3 test_first_lesson.py           # kỳ vọng 26/26 pass (không có test diagnostic)
```
(Hoặc đơn giản: `git revert <commit revert này>` để lấy lại cả 3, rồi xoá block
`debug rỗng` trong `fetch_cs_owner`.)

### Tóm tắt thiết kế (trong `watch_first_lesson.py` của PR #12/#13)
- `fetch_cs_owner(uids, client=None)` — batch 1 call CS app, trả `{uid: owner}`.
  **KHÔNG đụng CRM** → không ảnh hưởng rate-limit cổng SĐT.
- `resolve_gvcn(prof, owner_on) -> (gvcn, gvcn_cần_gắn)`:
  - `owner_on` False → giữ `head_teacher` như cũ.
  - True → GVCN chỉ hiện khi `head_teacher == owner`; còn lại để trống, `cần gắn = owner`.
- `owner_display_on(profiles, uids, owner_on)` — **guard**: chỉ bật luật để-trống khi
  THẬT SỰ có ≥1 owner; endpoint trả 0 (như 403 hiện tại) → giữ `head_teacher`, KHÔNG
  xoá trắng cột. (Chính guard này giúp production không bị cháy khi mình push lúc đầu.)
- Cache `_profile` thêm cột `owner`. Cột "GVCN cần gắn" thêm vào tab `Cần chăm tuần
  này` + `Danh sách chờ`; banner merge endColumnIndex 11→12. Log owner-aware nhưng
  **giữ layout** (tránh lệch cột ghi chú CS).

## Verify khi auth đã thông
Chạy 1 run (workflow_dispatch) rồi xem log:
- `[cs_owner] owner lấy N/… bé` với **N > 0** → thành công.
- Sheet: bé có advisor → cột GVCN trống + "GVCN cần gắn" có tên CS.

---
*Bài học: task này cần CS backend + test auth thật → đáng ra làm ở local từ đầu như M
đã yêu cầu. Doc này để finish gọn ở local.*
