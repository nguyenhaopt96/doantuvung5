# Vocab Motion — PandaStack

App web tạo hàng loạt video dọc kiểm tra từ vựng theo mẫu video tham chiếu.

## Tính năng đã khóa sẵn

- Tiêu đề mặc định: `TỪ VỰNG HAY` (luôn chuyển thành chữ in hoa khi render).
- CTA mặc định: `Vào nhóm trong bình luận để luyện nghe nói cùng Hà`; cụm `Vào nhóm` được tô vàng nổi bật và không có icon.
- Giọng Việt: Microsoft Edge TTS `vi-VN-HoaiMyNeural`.
- Giọng Anh: Microsoft Edge TTS `en-US-JennyNeural`, tốc độ `-15%`.
- Có nhịp tích tắc nhanh 0,2 giây trong thời gian suy nghĩ và tiếng ting đúng lúc đáp án tiếng Anh xuất hiện.
- Thanh vàng chạy theo đúng 2,8 giây suy nghĩ, giữ đầy 0,12 giây; chỉ sau đó mới hiện đáp án và phát tiếng ting nên không nháy sớm.
- Gợi ý giữ chữ cái đầu, có đúng một gạch vàng cho mỗi chữ cái còn lại. Khi mở đáp án, các chữ thay ngay vào đúng vị trí các gạch; cỡ đáp án mặc định 46 px (tăng 15%).
- Không hiển thị IPA; cột thứ ba trong dữ liệu dán vào sẽ tự bỏ.
- CTA dùng chữ nghiêng 29 px, được hạ tổng khoảng 0,8 cm so với mẫu gốc và đặt trên nền chữ nhật mờ để luôn rõ với mọi footage.
- Cụm `TEST NHANH` vụt lên trong 0,24 giây rồi biến mất đúng lúc đáp án xuất hiện.
- Video dọc 720 × 1280, H.264 + AAC.
- Nhận nhiều kịch bản và nhiều footage trong một lượt.
- Có sẵn 2 kịch bản mẫu, mỗi kịch bản 2 từ; nội dung chỉ mất khi bấm **Xóa kịch bản**.
- Footage được tải theo từng phần nhỏ và tự thử lại khi mạng chập chờn, tránh lỗi giới hạn request của host.
- Render chạy ở tiến trình máy chủ, không phụ thuộc tab trình duyệt. Sau khi web báo đã nhận đủ dữ liệu, có thể thoát sang app khác và quay lại xem tiếp tiến độ.
- Mỗi MP4 hoàn tất được lưu thành checkpoint; nếu worker khởi động lại, app giữ các bài đã xong và tiếp tục phần còn lại.
- Khi Edge TTS lỗi tạm thời, có thể bấm **Thử lại render** mà không phải tải footage lần nữa.
- Tải từng MP4 hoặc tải toàn bộ bằng một file ZIP.

## Mẫu kịch bản

```text
# Học nhanh từ vựng
Từ chối | Reject | reject
Chấp nhận | Accept | accept


# Học nhanh từ vựng
Đăng ký | Register | register
Sửa chữa | Repair | repair
```

Mỗi lần lặp lại dòng `# Học nhanh từ vựng` sẽ bắt đầu một video mới. Có thể dùng `---`, `===` hoặc hai dòng trống để tách video. App cũng nhận dữ liệu dán từ bảng bằng tab. Khi chuẩn hóa, app chỉ giữ lại hai cột Việt và Anh.

## Đưa lên GitHub và PandaStack

1. Giải nén file ZIP.
2. Đưa **toàn bộ file bên trong** lên thư mục gốc của một repository GitHub. `Dockerfile` phải nằm ngay ở trang đầu của repo.
3. Trong PandaStack, tạo app mới từ GitHub và chọn repository đó.
4. Bấm Deploy. File `pandastack.json` đã khai báo sẵn Start Command.
5. Nếu giao diện vẫn bắt nhập thủ công: Install `pip install -r requirements.txt`; Build để trống; Start `gunicorn app:app --workers 1 --threads 8 --timeout 0 --graceful-timeout 30 --no-control-socket --bind 0.0.0.0:$PORT`; Root Directory để trống; Port để Auto.
6. Không cần nhập API key. PandaStack chỉ cần cho phép app truy cập Internet để Edge TTS hoạt động.

PandaStack thường tự cấp biến `PORT`; app đã tự nhận biến này. Có thể đặt thêm:

- `MAX_UPLOAD_MB=900`: giới hạn tổng dung lượng footage trong một lượt.
- `JOB_TTL_HOURS=72`: số giờ giữ video kết quả trên ổ đĩa tạm của app.
- `UPLOAD_CHUNK_BYTES=786432`: kích thước mỗi phần upload (mặc định 768 KB).

Kiểm tra app sau khi deploy tại đường dẫn `/health`; kết quả đúng là JSON có `"ok": true`.

## Lưu ý vận hành

- Chỉ thoát trang sau khi thấy thông báo **“Đã nhận đủ dữ liệu”**. Nếu thoát trong lúc footage còn đang tải lên thì trình duyệt có thể hủy upload.
- Khi mở lại đúng link bằng cùng trình duyệt, app tự nối lại lượt render gần nhất.
- Dữ liệu và video nằm trên ổ đĩa tạm của máy chủ. Nếu PandaStack xóa hoặc khởi động lại container bằng một ổ đĩa hoàn toàn mới, kết quả cũ có thể mất; các lượt đang dở sẽ tự tiếp tục nếu thư mục runtime vẫn còn.
- Edge TTS là dịch vụ mạng miễn phí, đôi lúc có thể giới hạn tạm thời. App tạo giọng tuần tự, tự thử lại sáu lần; nếu vẫn lỗi, nút **Thử lại render** sẽ tiếp tục từ dữ liệu cũ.

## Chạy thử trên máy tính

Máy cần Python 3.12. App ưu tiên FFmpeg của hệ thống và có FFmpeg dự phòng từ gói Python.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/gunicorn app:app --workers 1 --threads 8 --timeout 0 --no-control-socket --bind 0.0.0.0:8080
```

Mở `http://localhost:8080`.
