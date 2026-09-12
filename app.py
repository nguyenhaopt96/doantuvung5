import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from script_parser import parse_scripts


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "runtime"
JOBS_DIR = RUNTIME_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "900"))
JOB_TTL_HOURS = int(os.environ.get("JOB_TTL_HOURS", "72"))
UPLOAD_TTL_HOURS = int(os.environ.get("UPLOAD_TTL_HOURS", "6"))
UPLOAD_CHUNK_BYTES = max(
    128 * 1024,
    min(int(os.environ.get("UPLOAD_CHUNK_BYTES", str(768 * 1024))), 2 * 1024 * 1024),
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["JSON_AS_ASCII"] = False

_startup_lock = threading.Lock()
_startup_done = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def script_validation_error(parsed: dict) -> str | None:
    if not parsed["scripts"]:
        return "Chưa tìm thấy dòng từ vựng hợp lệ. Dùng mẫu: Tiếng Việt | English"
    if parsed["script_count"] > 60:
        return "Mỗi lượt tối đa 60 kịch bản."
    if parsed["word_count"] > 600:
        return "Mỗi lượt tối đa 600 từ/cụm từ."
    if any(len(script["items"]) > 50 for script in parsed["scripts"]):
        return "Mỗi kịch bản tối đa 50 từ. Hãy thêm một tiêu đề mới để tách video."
    return None


def build_config(values) -> dict:
    return {
        "title": (values.get("title") or "TỪ VỰNG HAY").strip()[:60].upper(),
        "subtitle": (values.get("subtitle") or "TEST NHANH").strip()[:60].upper(),
        "cta": (values.get("cta") or "Vào nhóm trong bình luận để luyện nghe nói cùng Hà")
        .replace("👇", "")
        .strip()[:180],
        "watermark": (values.get("watermark") or "").strip()[:80],
        "vi_voice": "vi-VN-HoaiMyNeural",
        "en_voice": "en-US-JennyNeural",
        "en_rate": "-15%",
        "width": 720,
        "height": 1280,
        "fps": 30,
    }


def valid_job_dir(job_id: str) -> Path | None:
    if not JOB_ID_RE.fullmatch(job_id or ""):
        return None
    job_dir = JOBS_DIR / job_id
    return job_dir if job_dir.is_dir() else None


def public_status(job_id: str, status: dict) -> dict:
    safe = {
        "job_id": job_id,
        "state": status.get("state", "queued"),
        "phase": status.get("phase", "Đang xếp hàng…"),
        "percent": int(status.get("percent", 0)),
        "script_count": int(status.get("script_count", 0)),
        "word_count": int(status.get("word_count", 0)),
        "current_script": int(status.get("current_script", 0)),
        "created_at": status.get("created_at"),
        "updated_at": status.get("updated_at"),
        "error": status.get("error"),
        "results": status.get("results", []),
    }
    if safe["state"] == "completed":
        safe["download_all_url"] = f"/api/jobs/{job_id}/download-all"
    return safe


def spawn_worker(job_id: str) -> None:
    job_dir = JOBS_DIR / job_id
    log_handle = open(job_dir / "worker.log", "a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, str(BASE_DIR / "worker.py"), job_id],
        cwd=str(BASE_DIR),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log_handle.close()


def cleanup_and_resume() -> None:
    now = time.time()
    for job_dir in JOBS_DIR.iterdir():
        if not job_dir.is_dir() or not JOB_ID_RE.fullmatch(job_dir.name):
            continue
        status_path = job_dir / "status.json"
        if not status_path.exists():
            continue
        try:
            status = read_json(status_path)
            age_hours = (now - status_path.stat().st_mtime) / 3600
            if status.get("state") == "uploading" and age_hours > UPLOAD_TTL_HOURS:
                shutil.rmtree(job_dir, ignore_errors=True)
            elif status.get("state") in {"completed", "failed"} and age_hours > JOB_TTL_HOURS:
                shutil.rmtree(job_dir, ignore_errors=True)
            elif status.get("state") in {"queued", "running"}:
                spawn_worker(job_dir.name)
        except Exception:
            continue


@app.before_request
def initialize_once() -> None:
    global _startup_done
    if _startup_done:
        return
    with _startup_lock:
        if not _startup_done:
            cleanup_and_resume()
            _startup_done = True


@app.get("/")
def index():
    return render_template("index.html", max_upload_mb=MAX_UPLOAD_MB)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "vocab-motion"})


@app.post("/api/normalize")
def normalize_script():
    payload = request.get_json(silent=True) or {}
    parsed = parse_scripts(str(payload.get("script", "")))
    return jsonify(parsed)


@app.post("/api/uploads")
def create_chunked_upload():
    payload = request.get_json(silent=True) or {}
    raw_script = str(payload.get("script", ""))
    if len(raw_script) > 250_000:
        return jsonify({"error": "Kịch bản quá dài."}), 400

    parsed = parse_scripts(raw_script)
    validation_error = script_validation_error(parsed)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return jsonify({"error": "Hãy tải lên ít nhất một video footage."}), 400
    if len(files) > 20:
        return jsonify({"error": "Mỗi lượt tối đa 20 file footage."}), 400

    normalized_files = []
    total_size = 0
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            return jsonify({"error": "Thông tin footage không hợp lệ."}), 400
        original_name = str(item.get("name") or f"footage-{index + 1}.mp4")
        suffix = Path(original_name).suffix.lower()
        if suffix not in ALLOWED_VIDEO_EXTENSIONS:
            return jsonify({"error": f"File {original_name} không phải định dạng video được hỗ trợ."}), 400
        try:
            expected_size = int(item.get("size", 0))
        except (TypeError, ValueError):
            expected_size = 0
        if expected_size <= 0:
            return jsonify({"error": f"File {original_name} đang rỗng."}), 400
        total_size += expected_size
        clean_name = secure_filename(original_name) or f"footage-{index + 1}{suffix}"
        clean_name = f"{index + 1:02d}-{clean_name}"
        normalized_files.append(
            {
                "index": index,
                "name": original_name[:180],
                "size": expected_size,
                "path": f"footage/{clean_name}",
            }
        )

    if total_size > MAX_UPLOAD_MB * 1024 * 1024:
        return jsonify({"error": f"Tổng dung lượng footage vượt quá {MAX_UPLOAD_MB} MB."}), 413

    job_id = uuid.uuid4().hex
    created_at = utc_now()
    job_dir = JOBS_DIR / job_id
    (job_dir / "footage").mkdir(parents=True)
    (job_dir / "output").mkdir()

    job_request = {
        "job_id": job_id,
        "created_at": created_at,
        "scripts": parsed["scripts"],
        "script_count": parsed["script_count"],
        "word_count": parsed["word_count"],
        "normalized_script": parsed["normalized"],
        "ignored": parsed["ignored"],
        "footage": [item["path"] for item in normalized_files],
        "upload_files": normalized_files,
        "upload_total_bytes": total_size,
        "config": build_config(payload),
    }
    atomic_json(job_dir / "request.json", job_request)
    atomic_json(
        job_dir / "status.json",
        {
            "state": "uploading",
            "phase": "Đang nhận footage theo từng phần…",
            "percent": 0,
            "script_count": parsed["script_count"],
            "word_count": parsed["word_count"],
            "current_script": 0,
            "results": [],
            "created_at": created_at,
            "updated_at": created_at,
            "error": None,
        },
    )
    return jsonify(
        {
            "job_id": job_id,
            "upload_id": job_id,
            "chunk_size": UPLOAD_CHUNK_BYTES,
            "total_bytes": total_size,
        }
    ), 201


@app.post("/api/uploads/<job_id>/chunk")
def upload_chunk(job_id: str):
    job_dir = valid_job_dir(job_id)
    if not job_dir:
        return jsonify({"error": "Lượt tải lên không còn tồn tại."}), 404

    try:
        file_index = int(request.form.get("file_index", "-1"))
        offset = int(request.form.get("offset", "-1"))
    except ValueError:
        return jsonify({"error": "Vị trí phần footage không hợp lệ."}), 400
    chunk = request.files.get("chunk")
    if chunk is None or offset < 0:
        return jsonify({"error": "Thiếu dữ liệu footage."}), 400

    lock_path = job_dir / "upload.lock"
    with open(lock_path, "a+") as upload_lock:
        fcntl.flock(upload_lock.fileno(), fcntl.LOCK_EX)
        job_request = read_json(job_dir / "request.json")
        status = read_json(job_dir / "status.json")
        if status.get("state") != "uploading":
            return jsonify({"error": "Lượt tải lên này đã kết thúc.", "state": status.get("state")}), 409

        upload_files = job_request.get("upload_files", [])
        if not 0 <= file_index < len(upload_files):
            return jsonify({"error": "Không tìm thấy footage cần tải."}), 400
        file_meta = upload_files[file_index]
        data = chunk.stream.read(UPLOAD_CHUNK_BYTES + 1)
        if not data:
            return jsonify({"error": "Phần footage đang rỗng."}), 400
        if len(data) > UPLOAD_CHUNK_BYTES:
            return jsonify({"error": "Phần footage vượt quá kích thước cho phép."}), 413

        expected_size = int(file_meta["size"])
        if offset + len(data) > expected_size:
            return jsonify({"error": "Phần footage vượt quá dung lượng file đã khai báo."}), 400

        destination = job_dir / file_meta["path"]
        current_size = destination.stat().st_size if destination.exists() else 0
        if current_size == offset:
            with open(destination, "ab") as target:
                target.write(data)
            current_size += len(data)
        elif current_size >= offset + len(data):
            # The previous response may have been lost after the bytes were saved.
            # Treat an exact retry as accepted instead of appending it twice.
            pass
        else:
            return jsonify(
                {
                    "error": "Thứ tự phần footage chưa khớp, app sẽ thử lại.",
                    "expected_offset": current_size,
                }
            ), 409

        total_received = 0
        for item in upload_files:
            path = job_dir / item["path"]
            if path.exists():
                total_received += min(path.stat().st_size, int(item["size"]))
        total_expected = max(1, int(job_request["upload_total_bytes"]))
        status.update(
            {
                "phase": f"Đang nhận footage · {total_received}/{total_expected} byte",
                "percent": min(99, round(total_received / total_expected * 100)),
                "updated_at": utc_now(),
            }
        )
        atomic_json(job_dir / "status.json", status)

    return jsonify(
        {
            "ok": True,
            "file_index": file_index,
            "file_received_bytes": current_size,
            "total_received_bytes": total_received,
            "total_bytes": total_expected,
        }
    )


@app.post("/api/uploads/<job_id>/complete")
def complete_chunked_upload(job_id: str):
    job_dir = valid_job_dir(job_id)
    if not job_dir:
        return jsonify({"error": "Lượt tải lên không còn tồn tại."}), 404

    should_spawn = False
    with open(job_dir / "upload.lock", "a+") as upload_lock:
        fcntl.flock(upload_lock.fileno(), fcntl.LOCK_EX)
        job_request = read_json(job_dir / "request.json")
        status = read_json(job_dir / "status.json")
        if status.get("state") in {"queued", "running", "completed"}:
            return jsonify(
                {
                    "job_id": job_id,
                    "message": "Dữ liệu đã được nhận và render đã bắt đầu.",
                    "status_url": f"/api/jobs/{job_id}",
                }
            ), 202
        if status.get("state") != "uploading":
            return jsonify({"error": "Lượt tải lên không thể hoàn tất ở trạng thái hiện tại."}), 409

        for item in job_request.get("upload_files", []):
            path = job_dir / item["path"]
            received = path.stat().st_size if path.exists() else 0
            if received != int(item["size"]):
                return jsonify(
                    {
                        "error": f"Footage {item['name']} chưa được tải đủ.",
                        "file_index": item["index"],
                        "expected_offset": received,
                    }
                ), 409

        status.update(
            {
                "state": "queued",
                "phase": "Đã nhận đủ dữ liệu · đang xếp hàng render",
                "percent": 1,
                "updated_at": utc_now(),
                "error": None,
            }
        )
        atomic_json(job_dir / "status.json", status)
        should_spawn = True

    if should_spawn:
        spawn_worker(job_id)
    return jsonify(
        {
            "job_id": job_id,
            "message": "Đã nhận đủ dữ liệu. Bây giờ có thể thoát app; render vẫn tiếp tục.",
            "status_url": f"/api/jobs/{job_id}",
        }
    ), 202


@app.post("/api/jobs")
def create_job():
    raw_script = request.form.get("script", "")
    parsed = parse_scripts(raw_script)
    validation_error = script_validation_error(parsed)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    uploads = request.files.getlist("footage")
    uploads = [item for item in uploads if item and item.filename]
    if not uploads:
        return jsonify({"error": "Hãy tải lên ít nhất một video footage."}), 400
    if len(uploads) > 20:
        return jsonify({"error": "Mỗi lượt tối đa 20 file footage."}), 400

    for upload in uploads:
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in ALLOWED_VIDEO_EXTENSIONS:
            return jsonify({"error": f"File {upload.filename} không phải định dạng video được hỗ trợ."}), 400

    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    footage_dir = job_dir / "footage"
    output_dir = job_dir / "output"
    footage_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    saved_footage = []
    for index, upload in enumerate(uploads, start=1):
        clean_name = secure_filename(upload.filename) or f"footage-{index}.mp4"
        clean_name = f"{index:02d}-{clean_name}"
        destination = footage_dir / clean_name
        upload.save(destination)
        if destination.stat().st_size == 0:
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify({"error": f"File {upload.filename} đang rỗng."}), 400
        saved_footage.append(str(destination.relative_to(job_dir)))

    config = build_config(request.form)
    job_request = {
        "job_id": job_id,
        "created_at": utc_now(),
        "scripts": parsed["scripts"],
        "script_count": parsed["script_count"],
        "word_count": parsed["word_count"],
        "normalized_script": parsed["normalized"],
        "ignored": parsed["ignored"],
        "footage": saved_footage,
        "config": config,
    }
    atomic_json(job_dir / "request.json", job_request)
    status = {
        "state": "queued",
        "phase": "Đã nhận đủ dữ liệu · đang xếp hàng render",
        "percent": 1,
        "script_count": parsed["script_count"],
        "word_count": parsed["word_count"],
        "current_script": 0,
        "results": [],
        "created_at": job_request["created_at"],
        "updated_at": utc_now(),
        "error": None,
    }
    atomic_json(job_dir / "status.json", status)
    spawn_worker(job_id)

    return jsonify(
        {
            "job_id": job_id,
            "message": "Đã nhận đủ dữ liệu. Bây giờ có thể thoát app; render vẫn tiếp tục.",
            "status_url": f"/api/jobs/{job_id}",
        }
    ), 202


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    job_dir = valid_job_dir(job_id)
    if not job_dir:
        return jsonify({"error": "Không tìm thấy lượt render này."}), 404
    try:
        return jsonify(public_status(job_id, read_json(job_dir / "status.json")))
    except Exception:
        return jsonify({"error": "Trạng thái lượt render đang được cập nhật, thử lại sau một chút."}), 503


@app.post("/api/jobs/<job_id>/retry")
def retry_job(job_id: str):
    job_dir = valid_job_dir(job_id)
    if not job_dir:
        return jsonify({"error": "Không tìm thấy lượt render này."}), 404
    if not (job_dir / "request.json").is_file():
        return jsonify({"error": "Lượt render thiếu dữ liệu nguồn."}), 409

    with open(job_dir / "retry.lock", "a+") as retry_lock:
        fcntl.flock(retry_lock.fileno(), fcntl.LOCK_EX)
        status = read_json(job_dir / "status.json")
        state = status.get("state")
        if state == "uploading":
            return jsonify({"error": "Footage chưa được tải đủ."}), 409
        if state == "completed":
            return jsonify(public_status(job_id, status)), 200
        if state in {"queued", "running"}:
            return jsonify(public_status(job_id, status)), 202

        if state != "failed":
            return jsonify({"error": "Chỉ có thể thử lại lượt render đã bị lỗi."}), 409

        status.update(
            {
                "state": "queued",
                "phase": "Đang thử lại render, dữ liệu cũ vẫn được giữ…",
                "percent": max(1, min(97, int(status.get("percent", 1)))),
                "updated_at": utc_now(),
                "error": None,
            }
        )
        atomic_json(job_dir / "status.json", status)

    spawn_worker(job_id)
    response = public_status(job_id, read_json(job_dir / "status.json"))
    response["message"] = "Đã thử lại render; không cần tải footage lần nữa."
    return jsonify(response), 202


@app.get("/api/jobs/<job_id>/files/<path:filename>")
def download_result(job_id: str, filename: str):
    job_dir = valid_job_dir(job_id)
    if not job_dir:
        return jsonify({"error": "Không tìm thấy lượt render này."}), 404
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.endswith(".mp4"):
        return jsonify({"error": "Tên file không hợp lệ."}), 400
    output_dir = job_dir / "output"
    if not (output_dir / safe_name).is_file():
        return jsonify({"error": "Video chưa sẵn sàng."}), 404
    return send_from_directory(output_dir, safe_name, as_attachment=True)


@app.get("/api/jobs/<job_id>/download-all")
def download_all(job_id: str):
    job_dir = valid_job_dir(job_id)
    if not job_dir:
        return jsonify({"error": "Không tìm thấy lượt render này."}), 404
    zip_path = job_dir / "output" / "tu-vung-hay-tat-ca.zip"
    if not zip_path.is_file():
        return jsonify({"error": "File ZIP chưa sẵn sàng."}), 404
    return send_from_directory(zip_path.parent, zip_path.name, as_attachment=True)


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": f"Tổng dung lượng footage vượt quá {MAX_UPLOAD_MB} MB."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
