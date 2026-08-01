import glob as globlib
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
FFMPEG, FFPROBE = static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
FFMPEG_DIR = str(Path(FFMPEG).parent)

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
    # 봇 차단 문구가 "로그인하세요"처럼 보여도 실제로는 잠깐 막힌 것뿐이라 여기서 먼저 걸러낸다
    if ("rehydration" in low or "webpage video data" in low
            or "not a bot" in low or "captcha" in low):
        return "사이트가 잠깐 차단했어요(로봇인지 확인 중). 같은 주소를 한 번 더 넣으면 되는 경우가 많아요. 계속 안 되면 몇 분 뒤에 다시 해보세요."
    if "unsupported url" in low:
        return "지원하지 않는 주소예요. URL을 복사할 때 잘린 게 아닌지 확인해주세요."
    if "is private" in low or "private video" in low or "members-only" in low:
        return "로그인해야 볼 수 있는 비공개 영상이라 받을 수 없어요."
    if ("video unavailable" in low or "has been removed" in low
            or "does not exist" in low or "no longer available" in low):
        return "삭제됐거나 존재하지 않는 영상이에요."
    if "geo-restricted" in low or "in your country" in low or "your location" in low:
        return "지역 제한이 걸린 영상이라 한국에서는 받을 수 없어요."
    if "urlopen" in low or "timed out" in low or "connection" in low or "network" in low:
        return "인터넷 연결에 문제가 있어요. 잠시 후 다시 시도해주세요."
    return f"오류가 발생했어요: {msg[:150]}"


def set_job(job_id, **fields):
    with lock:
        if job_id in jobs:  # '끝난 항목 지우기'로 이미 사라졌을 수 있다
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


def save_subs_file(video_path, subs):
    """자막을 영상 옆에 .txt로도 남긴다. 프로그램을 껐다 켜도 안 사라지게."""
    try:
        txt = "\n".join(f"[{s['t']}] {s['text']}" for s in subs)
        video_path.with_suffix(".txt").write_text(txt, encoding="utf-8")
    except Exception:
        pass  # 저장에 실패해도 화면에는 자막이 보이니까 그냥 넘어간다


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


def has_video_track(info):
    """영상 트랙이 있는지 확인. 틱톡샵 연결 영상은 소리만 나오는 경우가 있다."""
    fmts = info.get("formats") or [info]
    return any(f.get("vcodec") not in (None, "none") for f in fmts)


def _frac(s):
    """'98100/4049' 같은 분수 문자열을 숫자로. 못 읽으면 0."""
    try:
        if "/" in str(s):
            a, b = str(s).split("/")
            return float(a) / float(b) if float(b) else 0.0
        return float(s)
    except Exception:
        return 0.0


def probe_media(path):
    """ffprobe로 영상 정보를 읽는다. 메타데이터만 보므로 파일이 커도 순식간이다."""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries",
             "stream=index,codec_type,codec_name,r_frame_rate,avg_frame_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20)
        return json.loads(result.stdout) if result.returncode == 0 else None
    except Exception:
        return None


def vfr_target_fps(probe):
    """가변 프레임이면 목표 속도(정수)를, 아니면 None을 준다.

    캡컷은 타임라인이 고정 프레임을 가정해서, 가변 프레임 영상은 미리보기는 맞는데
    내보내면 소리가 밀린다. r_frame_rate(가장 빠른 구간)와 avg_frame_rate(평균)가
    많이 다르면 가변으로 본다.
    """
    if not probe:
        return None
    v = next((s for s in (probe.get("streams") or [])
              if s.get("codec_type") == "video"), None)
    if not v:
        return None
    r, avg = _frac(v.get("r_frame_rate")), _frac(v.get("avg_frame_rate"))
    if avg <= 0 or r <= 0 or abs(r - avg) / avg <= 0.02:
        return None
    # 평균값을 쓰면 실제 프레임이 버려진다. 가장 빠른 구간을 담을 수 있는 값으로.
    return 60 if avg > 45 else 30


def tiktok_mp4_fallback(url):
    """yt-dlp가 소리만 줄 때 쓰는 안전망.

    틱톡샵에 연결된 영상은 틱톡 웹페이지에 영상 주소가 아예 없어서(yt-dlp 이슈 #13928)
    공개 API를 거쳐야 mp4를 받을 수 있다. 흔한 다운로드 사이트들이 쓰는 방식.
    """
    from curl_cffi import requests
    r = requests.post("https://www.tikwm.com/api/", data={"url": url, "hd": 1},
                      impersonate="chrome", timeout=30)
    data = (r.json() or {}).get("data") or {}
    video_url = data.get("hdplay") or data.get("play")
    if not video_url:
        return None, None
    if video_url.startswith("/"):
        video_url = "https://www.tikwm.com" + video_url
    return video_url, data.get("title")


def download_via_fallback(job_id, url, out_path):
    from curl_cffi import requests
    video_url, _ = tiktok_mp4_fallback(url)
    if not video_url:
        raise RuntimeError("영상 주소를 찾지 못했어요.")
    set_job(job_id, status="다운로드 중", progress=0)
    resp = requests.get(video_url, impersonate="chrome", timeout=60, stream=True)
    resp.raise_for_status()  # 에러 페이지(HTML)를 영상인 척 저장하는 걸 막는다
    total = int(resp.headers.get("Content-Length") or 0)
    done = 0
    tmp = out_path.with_suffix(".part")
    with open(tmp, "wb") as f:
        for chunk in resp.iter_content(1024 * 256):
            f.write(chunk)
            done += len(chunk)
            if total:
                set_job(job_id, progress=min(round(done / total * 100), 100))

    # 중간에 끊긴 파일을 '완료'로 속이지 않는다 (편집하다가 알면 늦다).
    # 예외를 던지면 바깥 재시도 사다리가 알아서 다시 받는다.
    if total and done != total:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("파일을 받다가 중간에 끊겼어요.")
    if done < 100_000:  # 영상이라기엔 너무 작다 = 에러 페이지일 가능성
        tmp.unlink(missing_ok=True)
        raise RuntimeError("영상 파일을 제대로 받지 못했어요.")
    tmp.rename(out_path)


def is_permanent_error(err):
    """재시도해도 소용없는 에러 (삭제됨, 비공개, 지원 안 함 등)

    주의: 유튜브 봇 차단 문구가 "Sign in to confirm you're not a bot"이라서
    'sign in' 같은 짧은 조각으로 판단하면 멀쩡한 공개 영상을 비공개로 오해한다.
    애매하면 재시도하는 쪽이 낫다 — 헛재시도는 30초 낭비지만, 헛포기는 거짓말이다.
    """
    low = str(err).lower()
    if "not a bot" in low or "captcha" in low or "rehydration" in low:
        return False
    return any(k in low for k in (
        "unsupported url", "is private", "private video", "members-only",
        "video unavailable", "has been removed", "does not exist",
        "no longer available", "account has been terminated",
    ))


def download_one(job_id, url):
    # 여기서 예외가 새어나가면 작업이 영원히 '대기 중'에 멈춘 채로 아무도 모른다
    try:
        _download_one(job_id, url)
    except Exception as e:
        set_job(job_id, status="실패", error=friendly_error(e))


def _download_one(job_id, url):
    dest = get_download_dir()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        set_job(job_id, status="실패", error=(
            f"저장 폴더를 열 수 없어요: {dest}\n"
            "외장하드가 빠졌거나 폴더가 사라진 것 같아요. "
            "'📁 저장 위치 바꾸기'로 폴더를 다시 골라주세요."))
        return

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

    if has_video_track(info):
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
    else:
        # 소리만 있는 경우 (틱톡샵 연결 영상 등) — 다른 경로로 mp4를 받는다
        set_job(job_id, status="영상 찾는 중")
        download_via_fallback(job_id, url, out_path)

    # 합친 결과가 mp4가 아닌 경우(사이트에 따라 webm 등)를 대비해 실제 파일을 찾는다.
    # 제목에 [4K] 같은 대괄호가 있으면 glob이 특수문자로 읽어버리니 escape 필수.
    # yt-dlp 중간 파일(name.f137.mp4)이 아니라 진짜 결과물을 골라야 한다.
    if not out_path.exists():
        candidates = [p for p in out_path.parent.glob(globlib.escape(out_path.stem) + ".*")
                      if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
                      and not re.search(r"\.f\d+$", p.stem)]
        if candidates:
            out_path = max(candidates, key=lambda p: p.stat().st_size)
        else:
            raise RuntimeError("다운로드는 끝났는데 파일을 찾지 못했어요.")

    # 자막이 있으면 같이 가져온다 (없거나 실패해도 다운로드는 성공 처리)
    try:
        subs = fetch_subtitles(info, common_opts)
    except Exception:
        subs = None
    if subs:
        save_subs_file(out_path, subs)
        set_job(job_id, subs=subs, sub_source="영상 자막")

    # 가변 프레임이면 알려준다 (캡컷에서 소리가 밀리는 원인). 실패해도 다운로드엔 영향 없음
    try:
        edit_fps = vfr_target_fps(probe_media(out_path))
    except Exception:
        edit_fps = None
    set_job(job_id, status="완료", progress=100, edit_fps=edit_fps,
            filename=out_path.name, path=str(out_path))


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
                            "progress": 0, "error": None, "filename": None, "path": None,
                            "subs": None, "sub_source": None, "sub_status": None,
                            "sub_running": False, "edit_fps": None,
                            "edit_status": None, "edit_running": False, "edit_file": None}
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
    try:
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(dest)], check=True)
    except Exception:
        return jsonify({"error": f"저장 폴더를 열 수 없어요: {dest}\n"
                                 "외장하드가 빠졌거나 폴더가 사라진 것 같아요. "
                                 "'📁 저장 위치 바꾸기'로 다시 골라주세요."}), 400
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


_whisper_model = None
_whisper_lock = threading.Lock()
# 음성인식은 CPU를 전부 쓴다. 여러 개를 동시에 돌리면 서로 느려지기만 하므로 한 번에 하나씩.
whisper_executor = ThreadPoolExecutor(max_workers=1)


def get_whisper():
    """음성인식 모델은 처음 쓸 때 한 번만 불러온다 (첫 사용 시 다운로드 ~500MB)."""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
    return _whisper_model


def transcribe_job(job_id, video_path):
    try:
        set_job(job_id, sub_status="음성 인식 준비 중 (처음 한 번은 오래 걸려요)")
        model = get_whisper()
        set_job(job_id, sub_status="소리 듣는 중...")
        segments, _ = model.transcribe(str(video_path), beam_size=1, vad_filter=True)
        lines = []
        for seg in segments:
            total = int(seg.start)
            stamp = (f"{total // 60}:{total % 60:02d}" if total < 3600
                     else f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}")
            text = seg.text.strip()
            if text:
                lines.append({"t": stamp, "text": text})
                set_job(job_id, sub_status=f"소리 듣는 중... {len(lines)}줄")
        if lines:
            save_subs_file(video_path, lines)
            set_job(job_id, subs=lines, sub_source="음성인식", sub_status=None)
        else:
            set_job(job_id, sub_status="말소리를 찾지 못했어요 (음악만 있는 영상일 수 있어요). 다시 시도할 수 있어요.")
    except Exception as e:
        set_job(job_id, sub_status=f"음성 인식에 실패했어요 ({str(e)[:60]}). 다시 눌러보세요.")
    finally:
        # 무슨 일이 있어도 다시 시도할 수 있게 풀어준다
        set_job(job_id, sub_running=False)


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    job_id = (request.json or {}).get("job_id")
    with lock:
        job = jobs.get(job_id)
    if not job or not job.get("filename"):
        return jsonify({"error": "먼저 영상을 받아야 해요."}), 400
    # 받을 때 저장해둔 실제 경로를 쓴다. 저장 위치를 바꿔도 옛날 작업이 안 깨지게.
    path = Path(job["path"]) if job.get("path") else get_download_dir() / job["filename"]
    if not path.exists():
        return jsonify({"error": "영상 파일을 찾지 못했어요. 파일을 옮기거나 지우셨나요?"}), 400
    with lock:  # 확인과 표시를 한 번에 (버튼 두 번 눌러도 두 번 안 돌게)
        if jobs[job_id].get("sub_running"):
            return jsonify({"ok": True})
        jobs[job_id].update(sub_running=True, sub_status="대기 중")
    whisper_executor.submit(transcribe_job, job_id, path)
    return jsonify({"ok": True})


edit_executor = ThreadPoolExecutor(max_workers=1)  # 변환도 CPU를 많이 쓰니 하나씩


def edit_copy_path(src):
    path = src.with_name(src.stem + "_편집용.mp4")
    n = 2
    while path.exists():
        path = src.with_name(f"{src.stem}_편집용 ({n}).mp4")
        n += 1
    return path


def make_edit_copy_job(job_id, src, fps):
    dst = edit_copy_path(src)
    tmp = dst.with_suffix(".part.mp4")
    try:
        probe = probe_media(src)
        duration = 0.0
        try:
            result = subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(src)],
                capture_output=True, text=True, timeout=20)
            duration = float(result.stdout.strip() or 0)
        except Exception:
            pass

        cmd = [
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1", "-i", str(src),
            # 스트림 번호를 가정하면 안 된다 — 틱톡샵 우회로 받은 파일은 소리가 0번이다
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", f"fps={fps}", "-fps_mode", "cfr",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-af", "aresample=async=1:first_pts=0",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", str(tmp),
        ]
        set_job(job_id, edit_status="편집용 파일 만드는 중... 0%")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            if line.startswith("out_time_us=") and duration > 0:
                try:
                    pct = round(int(line.split("=")[1]) / 1_000_000 / duration * 100)
                    set_job(job_id, edit_status=f"편집용 파일 만드는 중... {min(pct, 100)}%")
                except Exception:
                    pass
        stderr = proc.stderr.read()
        proc.wait()
        if proc.returncode != 0 or not tmp.exists():
            raise RuntimeError(stderr[-200:] or "변환 실패")

        # 진짜 고정 프레임이 됐는지 확인하고 넘긴다 (아니면 준 의미가 없다)
        check = probe_media(tmp)
        cv = next((s for s in (check.get("streams") or [])
                   if s.get("codec_type") == "video"), {}) if check else {}
        if cv.get("r_frame_rate") != cv.get("avg_frame_rate"):
            raise RuntimeError("고정 프레임으로 안 바뀜")

        tmp.rename(dst)
        set_job(job_id, edit_status=None, edit_file=dst.name)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        msg = str(e).lower()
        if "no space" in msg or "space left" in msg:
            set_job(job_id, edit_status="저장 공간이 부족한 것 같아요. 공간을 확보하고 다시 눌러주세요.")
        else:
            set_job(job_id, edit_status="편집용 파일을 만들다가 문제가 생겼어요. 한 번 더 눌러보세요.")
    finally:
        set_job(job_id, edit_running=False)


@app.route("/api/make-edit-copy", methods=["POST"])
def api_make_edit_copy():
    job_id = (request.json or {}).get("job_id")
    with lock:
        job = jobs.get(job_id)
    if not job or not job.get("path"):
        return jsonify({"error": "먼저 영상을 받아야 해요."}), 400
    src = Path(job["path"])
    if not src.exists():
        return jsonify({"error": "영상 파일을 찾지 못했어요. 파일을 옮기거나 지우셨나요?"}), 400
    fps = job.get("edit_fps")
    if not fps:
        return jsonify({"error": "이 영상은 편집용 파일을 만들 필요가 없어요."}), 400
    with lock:
        if jobs[job_id].get("edit_running"):
            return jsonify({"ok": True})
        jobs[job_id].update(edit_running=True, edit_status="대기 중")
    edit_executor.submit(make_edit_copy_job, job_id, src, fps)
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
