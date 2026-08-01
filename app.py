import json
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import yt_dlp
from flask import Flask, jsonify, render_template, request
from static_ffmpeg import run as static_ffmpeg_run
from yt_dlp.utils import sanitize_filename

PORT = 5002
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "영상소스"
SETTINGS_FILE = Path(__file__).parent / "settings.json"  # 개인 설정이라 git에 안 올라감
MAX_PARALLEL = 3  # 사이트 차단 방지를 위해 동시에 3개까지만
RETRIES = 3


def get_download_dir():
    try:
        return Path(json.loads(SETTINGS_FILE.read_text())["download_dir"])
    except Exception:
        return DEFAULT_DOWNLOAD_DIR

app = Flask(__name__)

# venv 안에 내장된 ffmpeg 사용 (친구 컴퓨터에 ffmpeg 설치 안 해도 됨)
_ffmpeg_path, _ = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
FFMPEG_DIR = str(Path(_ffmpeg_path).parent)

# 브라우저 흉내(impersonate)에 필요한 부품이 있는지 확인
try:
    import curl_cffi  # noqa: F401
    HAS_IMPERSONATE = True
except ImportError:
    HAS_IMPERSONATE = False

# 봇 차단이 심해서 처음부터 브라우저 흉내가 필요한 사이트들
BOT_BLOCKING_SITES = ("tiktok.com", "douyin.com", "xiaohongshu.com", "xhslink.com", "instagram.com")

jobs = {}  # job_id -> 상태 정보
jobs_order = []
lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL)


def friendly_error(err):
    """yt-dlp 에러를 친구가 읽을 수 있는 한국어로 바꾼다."""
    msg = str(err)
    low = msg.lower()
    if "rehydration" in low or "webpage video data" in low:
        return "사이트가 잠깐 차단했어요(로봇인지 확인 중). 같은 주소를 한 번 더 넣으면 되는 경우가 많아요. 계속 안 되면 몇 분 뒤에 다시 해보세요."
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
    dest = get_download_dir()
    path = dest / f"{base}.mp4"
    n = 2
    while path.exists():
        path = dest / f"{base} ({n}).mp4"
        n += 1
    return path


def build_opts(impersonate_target):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": FFMPEG_DIR,
        "retries": RETRIES,
        "fragment_retries": RETRIES,
        "socket_timeout": 30,
    }
    if impersonate_target:
        # 틱톡·도우인·샤오홍슈 등은 봇을 차단해서, 진짜 브라우저인 척해야 받아진다
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget.from_str(impersonate_target)
    return opts


def pick_sub_track(info):
    """자막 트랙 고르기: 수동 자막 > 자동 자막, 한국어 > 영어 > 아무거나"""
    for source in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        if not source:
            continue
        for pref in ("ko", "en"):
            for lang, tracks in source.items():
                if lang.lower().startswith(pref) and tracks:
                    return tracks
        for tracks in source.values():
            if tracks:
                return tracks
    return None


def parse_vtt(text):
    """VTT 자막을 [{'t': '0:03', 'text': '...'}] 목록으로 바꾼다."""
    lines_out = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        for idx, line in enumerate(lines):
            if "-->" not in line:
                continue
            m = re.match(r"(?:(\d+):)?(\d+):(\d+)[.,]\d+", line.strip())
            if not m:
                break
            h, mnt, sec = m.groups()
            total = int(h or 0) * 3600 + int(mnt) * 60 + int(sec)
            if total < 3600:
                stamp = f"{total // 60}:{total % 60:02d}"
            else:
                stamp = f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
            txt = " ".join(lines[idx + 1:])
            txt = re.sub(r"<[^>]+>", "", txt).strip()  # <c>, <00:00:01.000> 같은 태그 제거
            # 자동 자막은 같은 문장이 반복돼서 붙는 경우가 많아 걸러낸다
            if txt and (not lines_out or lines_out[-1]["text"] != txt):
                lines_out.append({"t": stamp, "text": txt})
            break
    return lines_out


def fetch_subtitles(info, opts):
    """영상 정보에서 자막을 내려받아 파싱한다. 실패해도 다운로드에는 영향 없음."""
    tracks = pick_sub_track(info)
    if not tracks:
        return None
    track = next((t for t in tracks if t.get("ext") == "vtt"), None)
    if not track or not track.get("url"):
        return None
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.urlopen(track["url"]).read().decode("utf-8", "replace")
    return parse_vtt(data) or None


def is_permanent_error(err):
    """재시도해도 소용없는 에러 (삭제됨, 비공개, 지원 안 함 등)"""
    low = str(err).lower()
    return any(k in low for k in (
        "unsupported url", "private", "unavailable", "404",
        "not exist", "removed", "login", "sign in", "cookies",
    ))


def download_one(job_id, url):
    get_download_dir().mkdir(parents=True, exist_ok=True)
    # 봇 차단은 간헐적이라 재시도 자체가 효과가 있고, 브라우저 종류를 바꾸면 뚫리기도 한다
    if HAS_IMPERSONATE:
        if any(site in url for site in BOT_BLOCKING_SITES):
            strategies = ["chrome", "chrome", "safari", "chrome", "safari"]
        else:
            strategies = [None, "chrome", "safari"]
    else:
        strategies = [None]

    last_err = None
    for i, target in enumerate(strategies):
        if i:
            set_job(job_id, status=f"다시 시도 중 ({i}/{len(strategies) - 1})", progress=0)
            time.sleep(3 * i)
        try:
            attempt(job_id, url, target)
            return
        except Exception as e:
            last_err = e
            if is_permanent_error(e):
                break

    error = friendly_error(last_err)
    if not HAS_IMPERSONATE:
        error += " ('처음-설치.command'를 다시 더블클릭하면 차단을 뚫는 부품이 설치돼요)"
    set_job(job_id, status="실패", error=error)


def attempt(job_id, url, impersonate_target):
    common_opts = build_opts(impersonate_target)
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
        # 위에서 이미 가져온 정보를 재활용 — 페이지를 다시 접속하면 봇 차단에 또 노출된다
        ydl.process_ie_result(info, download=True)

    # 합친 결과가 mp4가 아닌 경우(사이트에 따라 webm 등)를 대비해 실제 파일을 찾는다
    if not out_path.exists():
        candidates = list(out_path.parent.glob(out_path.stem + ".*"))
        if candidates:
            out_path = candidates[0]
        else:
            raise RuntimeError("다운로드는 끝났는데 파일을 찾지 못했어요.")

    # 자막이 있으면 같이 가져온다 (없거나 실패해도 다운로드는 성공 처리)
    try:
        subs = fetch_subtitles(info, common_opts)
    except Exception:
        subs = None
    if subs:
        set_job(job_id, subs=subs)
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
            jobs[job_id] = {"id": job_id, "url": url, "title": None, "status": "대기 중",
                            "progress": 0, "error": None, "filename": None, "subs": None}
            jobs_order.append(job_id)
        executor.submit(download_one, job_id, url)
    return jsonify({"count": len(urls)})


@app.route("/api/status")
def api_status():
    with lock:
        job_list = [jobs[j] for j in jobs_order]
    return jsonify({"jobs": job_list, "download_dir": str(get_download_dir())})


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    dest = get_download_dir()
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["open", str(dest)])
    return jsonify({"ok": True})


@app.route("/api/choose-folder", methods=["POST"])
def api_choose_folder():
    """맥 기본 폴더 선택창을 띄워서 저장 위치를 바꾼다."""
    script = 'POSIX path of (choose folder with prompt "영상을 저장할 폴더를 고르세요")'
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=180)
        chosen = result.stdout.strip()
        if result.returncode == 0 and chosen:
            SETTINGS_FILE.write_text(
                json.dumps({"download_dir": chosen.rstrip("/")}, ensure_ascii=False))
    except subprocess.TimeoutExpired:
        pass  # 선택창을 오래 안 닫으면 그냥 기존 설정 유지
    return jsonify({"download_dir": str(get_download_dir())})


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
