# Flowboard Project Memory & Rules

Tài liệu này lưu trữ các thông tin thiết kế kiến trúc, cấu hình hệ thống và bài học kinh nghiệm của dự án Flowboard để định hướng cho các AI Agent làm việc sau này.

## 1. Kiến trúc LLM Providers (REST API & 9Router)

- **REST API Migration:** Dự án đã loại bỏ hoàn toàn các cơ chế chạy CLI subprocess (Claude Code, Gemini CLI, OpenAI Codex CLI) để chuyển sang gọi REST API trực tiếp bằng API Key thông qua HTTP Client (`httpx`).
- **9Router Integration:** 
  - Local proxy 9Router chạy mặc định tại `http://localhost:20128`.
  - Endpoint completions: `/v1/chat/completions`.
  - Endpoint models: `/v1/models`.
  - **Lưu ý cực kỳ quan trọng:** Luôn đính kèm `"stream": False` trong payload gửi tới 9Router Proxy để tắt streaming (SSE) mặc định, giúp nhận về JSON chuẩn, tránh lỗi parse JSON.
  - **Giới hạn Timeout:** Thiết lập timeout cho các tác vụ LLM (như `prompt_synth.py`, `vision.py`, `nine_router.py`) ở mức **300.0s** (5 phút) để phòng tránh lỗi ngắt kết nối giữa chừng khi 9Router xoay vòng tải hoặc gặp độ trễ từ server gốc.
- **Secrets Management:** Cấu hình API keys và mô hình lựa chọn được lưu trữ tại `~/.flowboard/secrets.json` dưới dạng file JSON cục bộ.

## 2. Thiết lập Môi trường & Chạy dự án trên Windows

- **Ports: **
  - Backend Agent: chạy cổng `8101`.
  - Frontend Dev Server: chạy cổng `5173`.
  - 9Router: cổng `20128`.
- **Start Script:** Sử dụng file `start_dev.bat` ở gốc dự án để tự động chạy đồng thời Backend và Frontend trên hai cửa sổ cmd riêng biệt.
- **Cross-platform Tests:** Các bài kiểm tra quyền truy cập file `0o600` của secrets (`test_write_sets_mode_0600`) được thiết kế bỏ qua xác thực POSIX permissions khi chạy trên Windows (`os.name == 'nt'`).

- Mọi provider hiển thị trên frontend (`AiProvidersSection.tsx`) đều hỗ trợ form điền API Key trực tiếp và cho phép lựa chọn Model/Combo động được lấy trực tiếp từ API của từng nhà cung cấp.
- Tránh sử dụng các ký tự đặc biệt như `->` trực tiếp trong mã nguồn JSX (thay bằng mã HTML entity như `&rarr;` hoặc ký tự Unicode `→`) để tránh lỗi biên dịch esbuild.

## 4. Quy tắc về Project-Scoping trên Google Flow

- **GET /v1/media:** Tất cả các truy vấn lấy thông tin hoặc tải file media từ Google Flow đều yêu cầu đính kèm tham số `clientContext.projectId` trong query string (ví dụ: `?clientContext.tool=PINHOLE&clientContext.projectId=<project_id>`). Nếu thiếu, API sẽ trả về lỗi `400 Request contains an invalid argument` từ chối truy cập.
- **Workflow Polling:** Khi bóc tách kết quả từ `extract_video_workflows`, bắt buộc phải trích xuất cả trường `projectId` của Google Flow để truyền vào URL của các bước kiểm tra trạng thái video tiếp theo.
