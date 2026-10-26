# palfish-lich-bat-thuong

Theo dõi **đặt lịch học bất thường** trên PalFish CRM và cảnh báo CS qua Google Sheet.

Bối cảnh: học viên/PH có thể tự đặt một loạt buổi học dồn liền nhau trong cùng
ngày (đặt nhầm) → cháy tiết, rối lịch. CRM không tự phát hiện. Tool này quét định
kỳ, phát hiện và đổ ra một Google Sheet để CS fulltime + CS part-time nhận ca cùng
theo dõi, có **dải ô cảnh báo tô đỏ** khi có bất thường.

## Cách hoạt động

```
GitHub Actions (cron 15')  →  đọc token CRM từ Supabase (crm_tokens, mã hoá Fernet)
   →  gọi API PalFish appoint/formal/list cho từng UID trong watchlist
   →  luật phát hiện  →  ghi Google Sheet (tab Cảnh báo + tab Theo dõi)
```

Không dùng ECS / Render — chạy hoàn toàn trên GitHub Actions.

## Luật phát hiện

Trong **cùng 1 ngày**, học viên/PH **tự đặt** (`create_by = "STUDENT"`) từ
`min_cluster` buổi trở lên mà các giờ bắt đầu cách nhau `<= gap_minutes` phút
(nối chuỗi liền kề) → cờ **bất thường**. Buổi do hệ thống tự đặt theo lịch cố
định (`系统按设置自动预约`) không tính.

Ngưỡng ở `config.json`:
- `gap_minutes`: 60
- `min_cluster`: 2
- `date_window_days_back` / `date_window_days_ahead`: cửa sổ ngày quét.

### Mã trạng thái (`action`)

Đối chiếu trực tiếp với nhãn CRM (xem `watch_schedule.py` → `ACTION_STATUS`):

| action | Trạng thái |
|---|---|
| 1 | Chờ lên lớp |
| 2 | Đã kết thúc lớp |
| 98 | Học sinh huỷ (kèm thời điểm huỷ) |
| 99 | Giáo viên huỷ (kèm thời điểm huỷ) |
| 101 | Học sinh vắng |

Mã lạ (chưa gặp) → fallback an toàn theo `cancel_time`/`stamp`, không bịa nhãn.

## Google Sheet (bắt buộc làm tay 1 lần)

Service Account **không tự tạo** được sheet (bị chặn quota Drive), nên:

1. Tạo 1 Google Sheet trống.
2. Share cho SA **`sodoanhthu-import@pf-gmv-manager.iam.gserviceaccount.com`** quyền **Editor**.
3. Đặt chia sẻ **"Bất kỳ ai có đường liên kết → Người xem"**.
4. Lấy Sheet ID (phần giữa URL `/d/<ID>/edit`) → đặt vào secret `GOOGLE_SHEET_ID`.

Tool tự tạo 2 tab: `Cảnh báo vận hành` và `Theo dõi`.

## Secrets (GitHub repo → Settings → Secrets → Actions)

| Secret | Nguồn |
|---|---|
| `SUPABASE_URL` | `https://jozcvbbypwvzaefteoxn.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Project Settings → API → service_role |
| `CRM_ENCRYPT_KEY` | Render (env của backend GMV) |
| `GOOGLE_SA_JSON` | Toàn bộ nội dung file JSON của Service Account |
| `GOOGLE_SHEET_ID` | ID sheet vừa tạo |
| `WATCHLIST_JSON` | *(tuỳ chọn)* ghi đè watchlist nếu muốn giấu UID khỏi repo |

## Thêm 1 học viên cần theo dõi

Chỉ cần `uid`. Thêm 1 dòng vào `config.json` → `watchlist`:
```json
{ "uid": 3316701347, "ten": "Max-1c7" }
```
Hoặc đặt cả danh sách vào secret `WATCHLIST_JSON` (dạng JSON array y hệt).

## Chi phí Actions

- **Public repo:** phút Actions **miễn phí không giới hạn** → chạy 15' thoải mái.
- **Private repo:** free 2000 phút/tháng; quét 15' cả ngày có thể sát/vượt → nếu
  private, nên nới cron lên 20–30' hoặc thu hẹp khung giờ.

## Chạy thử local

```bash
pip install -r requirements.txt
# token: dùng cookie CRM thủ công (khi không có CRM_ENCRYPT_KEY)
export CRM_COOKIE='...cookie CRM...'
export GOOGLE_SERVICE_ACCOUNT_JSON='D:\...\pf-gmv-manager-xxxx.json'
export GOOGLE_SHEET_ID='...'
python watch_schedule.py
```

---

# Job 2 — Học viên mới cần chăm trước buổi học đầu tiên (chị Huyền)

`watch_first_lesson.py` + workflow `first-lesson.yml` — **dùng chung** reader lịch
CRM + token + sheets service của `watch_schedule.py` (import, không sửa engine).
Sheet **riêng**, không liên quan sheet lịch bất thường.

## Cách hoạt động (2 lớp cho nhẹ tải)

```
Supabase so_doanh_thu  →  lọc "học viên mới"  ─┐
                                               ├─ quét CRM CHỈ (pool ∪ bé mới)
tab ẩn _pool (bé đang chờ, lần chạy trước)  ───┘      → đếm buổi đã học (action==2)
   → bé 0 buổi học + mua gần đây giữ trong pool · bé đã học buổi đầu rút khỏi pool
   → ghi tab "Cần chăm hôm nay" + "Danh sách chờ"
```

- **Học viên mới** = mọi đơn của 1 uid rơi vào cùng 1 lần mua (1 đơn, hoặc nhiều
  bé mua cùng lúc trong `FIRST_LESSON_SAME_BUY_MINUTES` phút). Có đơn ở lần mua
  khác (tái ký) → loại. Nguồn `so_doanh_thu` đã gồm cả team **HCM (Online)**.
- **Chưa học buổi đầu** = đếm buổi `action==2` ("Đã kết thúc lớp") có ngày học >
  thời điểm mua = 0. **Vắng (101) không tính** là đã học.
- **Pool** (tab ẩn `_pool`): giữ danh sách bé đang chờ qua các lần chạy nên mỗi
  lần chỉ quét CRM nhóm nhỏ, không quét lại toàn bộ (~522) học viên mới.

## Thiết lập (làm tay 1 lần)

1. Tạo 1 Google Sheet **mới** (tách khỏi sheet lịch bất thường), share Editor cho
   SA `sodoanhthu-import@pf-gmv-manager.iam.gserviceaccount.com`.
2. Secret `GOOGLE_SHEET_ID_FIRST` = ID sheet đó. (Các secret `SUPABASE_*`,
   `CRM_ENCRYPT_KEY`, `GOOGLE_SA_JSON` dùng chung với job 1.)
3. *(tuỳ chọn)* Repo Variables: `FIRST_LESSON_ARRIVAL_DAYS` (mặc định 3),
   `FIRST_LESSON_SAME_BUY_MINUTES` (60), `FIRST_LESSON_POOL_MAX_DAYS` (45).
4. **Seed lần đầu:** Actions → `first-lesson-care` → Run workflow → `seed_days` = 45
   (gom hết bé đã mua 45 ngày qua còn chưa học vào pool). Các lần sau chạy tự động.

## Lịch

Cron `first-lesson.yml`: **mỗi 30 phút, 08:30–22:00 VN** (sau 22h ngừng).

## Test

```bash
python test_first_lesson.py   # 9 test logic lọc (không cần mạng)
```
