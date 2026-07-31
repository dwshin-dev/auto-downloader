import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import yt_dlp
from flask import Flask, jsonify, render_template, request
from static_ffmpeg import run as static_ffmpeg_run
from yt_dlp.utils import sanitize_filename

PORT = 5002
DOWNLOAD_DIR = Path.home() / "Downloads" / "영상소스"
MAX_PARALLEL = 3  # 사이트 차단 방지를 위해 동시에 3개까지만
RETRIES = 3

app = Flask(__name__)

# venv 안에 내장된 ffmpeg 사용 (친구 컴퓨터에 ffmpeg 설치 안 해도 됨)
_ffmpeg_path, _ = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
FFMPEG_DIR = str(Path(_ffmpeg_path).parent)

jobs = {}  # job_id -> 상태 정보
jobs_order = []
lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL)


def friendly_error(err):
    """yt-dlp 에러를 친구가 읽을 수 있는 한국어로 바꾼다."""
    msg = str(err)
    low = msg.lower()
    if "unsupported url" in low:
        return "지원하지 않는 주소예요. URL을 복사할 때 잘린 게 아닌지 확인해주세요."
    if "private" in low or "login" in low or "cookies" in low or "sign in" in low:
        return "로그인해야 볼 수 있는 비공개 영상이라 받을 수 없어요."
    if "unavailable" in low or "404" in low or "not exist" in low or "removed" in low:
        return "삭제됐거나 존재하지 않는 영상이에요."
    if "geo" in low or "country" in low or "region" in low:
        return "지역 제한이 걸린 영상이라 한국에서는 받을 수 없어요."
    if "urlopen" in low or "timed out" in low or "connection" in low or "network" in low:
        return "인터넷 연결에 문제가 있어요. 잠시 후 다시 시도해주세요."
    return f"오류가 발생했어요: {msg[:150]}"


def set_job(job_id, **fields):
    with lock:
        jobs[job_id].update(fields)


def unique_path(date_str, title):
    """날짜_제목.mp4 형태로, 이미 있으면 (2), (3)... 을 붙인다."""
    safe_title = sanitize_filename(title, restricted=False)[:80].strip() or "제목없음"
    base = f"{date_str}_{safe_title}"
    path = DOWNLOAD_DIR / f"{base}.mp4"
    n = 2
    while path.exists():
        path = DOWNLOAD_DIR / f"{base} ({n}).mp4"
        n += 1
    return path


def build_opts(impersonate):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": FFMPEG_DIR,
        "retries": RETRIES,
        "fragment_retries": RETRIES,
    }
    if impersonate:
        # 틱톡·도우인·샤오홍슈 등은 봇을 차단해서, 진짜 브라우저인 척해야 받아진다
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget.from_str("chrome")
    return opts


def download_one(job_id, url):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        try:
            attempt(job_id, url, impersonate=False)
        except Exception:
            set_job(job_id, status="다른 방법으로 재시도 중", progress=0)
            attempt(job_id, url, impersonate=True)
    except Exception as e:
        set_job(job_id, status="실패", error=friendly_error(e))


def attempt(job_id, url, impersonate):
    common_opts = build_opts(impersonate)
    set_job(job_id, status="정보 확인 중")
    with yt_dlp.YoutubeDL(common_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = info.get("title") or url
    upload_date = info.get("upload_date")  # "20260731" 형태
    if upload_date:
        date_str = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")
    out_path = unique_path(date_str, title)
    set_job(job_id, title=title, filename=out_path.name)

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                pct = round(d.get("downloaded_bytes", 0) / total * 100)
                set_job(job_id, status="다운로드 중", progress=min(pct, 100))
        elif d["status"] == "finished":
            set_job(job_id, status="합치는 중", progress=100)

    dl_opts = {
        **common_opts,
        # 원본 최고 화질: 최고 영상 + 최고 소리를 받아서 mp4로 합침
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_path.with_suffix("")) + ".%(ext)s",
        "progress_hooks": [progress_hook],
    }
    set_job(job_id, status="다운로드 중")
    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.extract_info(url, download=True)

    # 합친 결과가 mp4가 아닌 경우(사이트에 따라 webm 등)를 대비해 실제 파일을 찾는다
    if not out_path.exists():
        candidates = list(DOWNLOAD_DIR.glob(out_path.stem + ".*"))
        if candidates:
            out_path = candidates[0]
        else:
            raise RuntimeError("다운로드는 끝났는데 파일을 찾지 못했어요.")
    set_job(job_id, status="완료", progress=100, filename=out_path.name)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def api_download():
    text = (request.json or {}).get("urls", "")
    urls, seen = [], set()
    for token in text.split():
        if token.startswith("http") and token not in seen:
            seen.add(token)
            urls.append(token)
    if not urls:
        return jsonify({"error": "URL을 찾지 못했어요. http로 시작하는 주소를 붙여넣어주세요."}), 400
    for url in urls:
        job_id = uuid.uuid4().hex[:8]
        with lock:
            jobs[job_id] = {"id": job_id, "url": url, "title": None,
                            "status": "대기 중", "progress": 0, "error": None, "filename": None}
            jobs_order.append(job_id)
        executor.submit(download_one, job_id, url)
    return jsonify({"count": len(urls)})


@app.route("/api/status")
def api_status():
    with lock:
        job_list = [jobs[j] for j in jobs_order]
    return jsonify({"jobs": job_list, "download_dir": str(DOWNLOAD_DIR)})


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["open", str(DOWNLOAD_DIR)])
    return jsonify({"ok": True})


@app.route("/api/clear-done", methods=["POST"])
def api_clear_done():
    with lock:
        done = [j for j in jobs_order if jobs[j]["status"] in ("완료", "실패")]
        for j in done:
            jobs_order.remove(j)
            del jobs[j]
    return jsonify({"ok": True})


if __name__ == "__main__":
    print(f"영상 소스 다운로더 실행: http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
