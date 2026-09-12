import fcntl
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from renderer import render_job


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "runtime"
JOBS_DIR = RUNTIME_DIR / "jobs"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, payload: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def main(job_id: str) -> int:
    job_dir = JOBS_DIR / job_id
    if not job_dir.is_dir():
        return 2

    with open(job_dir / "job.lock", "a+") as job_lock:
        try:
            fcntl.flock(job_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        status_path = job_dir / "status.json"
        request_path = job_dir / "request.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("state") == "completed":
            return 0

        render_lock_path = RUNTIME_DIR / "render.lock"
        render_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(render_lock_path, "a+") as render_lock:
            status.update(
                {
                    "state": "queued",
                    "phase": "Đang chờ lượt render trên máy chủ…",
                    "updated_at": now_iso(),
                    "error": None,
                }
            )
            atomic_write(status_path, status)
            fcntl.flock(render_lock.fileno(), fcntl.LOCK_EX)

            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
                render_job(job_dir, payload, status_path)
                return 0
            except Exception as exc:
                try:
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                except Exception:
                    status = {}
                status.update(
                    {
                        "state": "failed",
                        "phase": "Render bị lỗi",
                        "updated_at": now_iso(),
                        "error": str(exc)[:1800],
                    }
                )
                atomic_write(status_path, status)
                traceback.print_exc()
                return 1


if __name__ == "__main__":
    if len(sys.argv) != 2 or len(sys.argv[1]) != 32:
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
