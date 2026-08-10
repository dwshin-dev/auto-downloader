"""인스타 핫 릴스 찾기 — 로그인 세션을 '빌려서' 읽기 전용으로만 쓴다.

⚠️ 이건 인스타 공식 허용 범위 밖(비공식 API)이라, 쓰는 계정이 정지·인증요구를
   받을 수 있다. 그래서 여기 있는 모든 코드는 "그나마 덜 걸리는" 조건을 지킨다:
   - 로그인은 우리가 안 한다. 친구가 브라우저에서 이미 로그인한 세션을 빌려 쓴다
     (재로그인 때 새 기기처럼 보이는 게 정지 1순위 원인이라, 아예 로그인을 안 한다)
   - 요청은 한 번에 하나씩(순차) + 사이에 사람처럼 랜덤 지연
   - 하루 요청 수 상한. 같은 걸 자꾸 안 묻게 10분 캐시
   - 인증요구(challenge)가 한 번이라도 뜨면 그날은 통째로 쉰다 (더 긁으면 정지로 간다)

이 파일은 app.py의 다운로드 기능과 완전히 독립이다. 여기가 다 죽어도 다운로드는 된다.
"""
import random
import re
import threading
import time
from datetime import datetime, date

from curl_cffi import requests

IG_APP_ID = "936619743392459200"  # 인스타 웹이 쓰는 공개 앱 ID
PROFILE_API = "https://www.instagram.com/api/v1/users/web_profile_info/?username="
HASHTAG_API = "https://www.instagram.com/api/v1/tags/web_info/?tag_name="

# 안전 한도 (사람이 구경하는 수준을 넘지 않게 보수적으로)
DAILY_CAP = 120          # 하루 총 요청 상한 (인스타 플랫폼 한도 시간당 200보다 한참 아래)
MIN_DELAY, MAX_DELAY = 2.5, 6.0   # 요청 사이 랜덤 지연(초)
CACHE_TTL = 600          # 같은 대상 재조회는 10분간 캐시로 답한다


class RestingError(RuntimeError):
    """인증요구가 떠서 그날 쉬는 중이거나, 하루 상한을 넘었을 때."""


class SessionError(RuntimeError):
    """세션이 없거나 만료됐을 때 (브라우저에서 다시 로그인 필요)."""


class SafetyLimiter:
    """모든 인스타 요청이 반드시 통과하는 안전 관문."""

    def __init__(self):
        self.lock = threading.Lock()   # 요청을 한 번에 하나씩만
        self.day = date.today()
        self.count = 0
        self.resting_until = None       # datetime: 이 시각까지 인스타 요청 전면 중단
        self.rest_reason = None
        self.cache = {}                 # key -> (받은시각, 데이터)

    def _roll_day(self):
        today = date.today()
        if today != self.day:
            self.day = today
            self.count = 0
            # 날짜가 바뀌면 '오늘은 쉬기'도 풀린다
            if self.resting_until and datetime.now() >= self.resting_until:
                self.resting_until = None
                self.rest_reason = None

    def status(self):
        self._roll_day()
        resting = bool(self.resting_until and datetime.now() < self.resting_until)
        return {
            "resting": resting,
            "rest_reason": self.rest_reason if resting else None,
            "used_today": self.count,
            "daily_cap": DAILY_CAP,
        }

    def start_resting(self, reason, hours=24):
        # 다음 날 아침까지(최소 지정 시간) 인스타 요청을 전면 중단한다
        self.resting_until = datetime.now().replace(microsecond=0)
        from datetime import timedelta
        self.resting_until += timedelta(hours=hours)
        self.rest_reason = reason

    def get(self, url, cache_key, cookies):
        """캐시 → 상한 확인 → 지연 → 요청 → 위험신호 감지, 이 순서로만 인스타에 접근."""
        self._roll_day()

        if self.resting_until and datetime.now() < self.resting_until:
            raise RestingError(self.rest_reason or
                "인스타가 계정을 확인 중이라 오늘은 이 기능을 쉬어요. 내일 다시 해주세요.")

        # 캐시가 신선하면 인스타를 아예 안 건드린다
        hit = self.cache.get(cache_key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]

        with self.lock:
            # 락을 잡는 사이 다른 요청이 캐시를 채웠을 수 있다
            hit = self.cache.get(cache_key)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]

            if self.count >= DAILY_CAP:
                raise RestingError(
                    f"오늘 인스타 조회를 {DAILY_CAP}번 다 썼어요. 너무 자주 부르면 계정이 위험해서 막아둔 거예요. "
                    "내일 다시 해주세요.")

            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))  # 사람처럼 뜸을 들인다

            headers = {
                "x-ig-app-id": IG_APP_ID,
                "x-requested-with": "XMLHttpRequest",
                "Referer": "https://www.instagram.com/",
                "Accept": "application/json",
            }
            try:
                r = requests.get(url, headers=headers, cookies=cookies,
                                 impersonate="chrome", timeout=30)
            except Exception as e:
                raise RuntimeError(f"인스타에 연결하지 못했어요. 인터넷을 확인해주세요. ({str(e)[:60]})")

            self.count += 1
            data = self._read(r)
            self.cache[cache_key] = (time.time(), data)
            return data

    def _read(self, r):
        """응답을 읽으면서 위험 신호(인증요구·세션만료)를 먼저 걸러낸다."""
        body = r.text or ""
        low = body.lower()

        # 인증요구(challenge/checkpoint) — 가장 위험한 신호. 즉시 그날 쉰다.
        if ("checkpoint_required" in low or "challenge_required" in low
                or "/challenge/" in low):
            self.start_resting(
                "⚠️ 부계정에 '본인 확인' 요구가 떴어요. 지금 그 계정으로 인스타에 직접 로그인해서 "
                "확인 절차를 끝내주세요. 그리고 오늘은 이 기능을 쉬어야 계정이 안전해요.")
            raise RestingError(self.rest_reason)

        # JSON이 아니면(로그인 페이지 HTML 등) 세션이 죽은 것
        if not body.strip().startswith("{"):
            if r.status_code in (401, 403) or "login" in low:
                raise SessionError(
                    "인스타 로그인이 풀렸어요. 브라우저에서 그 부계정으로 다시 로그인한 다음, "
                    "'연결 확인'을 눌러주세요.")
            raise RuntimeError(f"인스타가 예상과 다른 응답을 줬어요 (HTTP {r.status_code}).")

        try:
            data = r.json()
        except Exception:
            raise RuntimeError("인스타 응답을 읽지 못했어요.")

        if r.status_code == 429 or (isinstance(data, dict) and data.get("message") == "rate_limited"):
            self.start_resting("인스타가 잠깐 너무 많다고 해서 오늘은 쉬어요. 내일 다시 해주세요.", hours=12)
            raise RestingError(self.rest_reason)
        if isinstance(data, dict) and data.get("status") == "fail":
            raise RuntimeError(data.get("message") or "인스타가 요청을 거절했어요.")
        return data


limiter = SafetyLimiter()


# ── 브라우저에서 로그인 세션(쿠키) 빌려오기 ─────────────────────────────

def load_cookies(browser):
    """친구가 지정한 브라우저에서 instagram.com 쿠키만 읽어온다.

    비밀번호는 절대 만지지 않는다. 이미 로그인된 세션 쿠키만 빌린다.
    """
    from yt_dlp.cookies import extract_cookies_from_browser
    from yt_dlp import YoutubeDL

    class _Q:  # yt-dlp가 조용히 돌게 하는 최소 로거
        def debug(self, m): pass
        def info(self, m): pass
        def warning(self, m): pass
        def error(self, m): pass

    try:
        jar = extract_cookies_from_browser(browser, logger=_Q())
    except Exception as e:
        raise SessionError(
            f"'{browser}' 브라우저에서 로그인 정보를 읽지 못했어요. 그 브라우저가 맞는지, "
            f"인스타에 로그인돼 있는지 확인해주세요. ({str(e)[:50]})")

    cookies = {}
    for c in jar:
        if "instagram.com" in (c.domain or ""):
            cookies[c.name] = c.value
    if not cookies.get("sessionid"):
        raise SessionError(
            "그 브라우저에 인스타 로그인이 안 돼 있어요. 브라우저에서 부계정으로 로그인한 뒤 다시 시도해주세요.")
    return cookies


# ── 릴스 정보 뽑기 ──────────────────────────────────────────────

def _reel_from_node(node):
    like = (node.get("edge_liked_by") or node.get("edge_media_preview_like") or {}).get("count")
    comment = (node.get("edge_media_to_comment") or {}).get("count")
    cap_edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
    caption = cap_edges[0]["node"]["text"] if cap_edges else ""
    shortcode = node.get("shortcode") or node.get("code")
    return {
        "shortcode": shortcode,
        "is_video": bool(node.get("is_video")),
        "views": node.get("video_view_count") or node.get("video_play_count"),
        "likes": like,
        "comments": comment,
        "timestamp": node.get("taken_at_timestamp") or node.get("taken_at"),
        "thumb": node.get("display_url") or node.get("thumbnail_src"),
        "caption": caption[:120],
        "url": f"https://www.instagram.com/reel/{shortcode}/" if shortcode else None,
        "source": None,  # 어느 계정/해시태그에서 나왔는지는 호출부에서 채운다
    }


def fetch_account_reels(username, cookies):
    """벤치마킹 계정 하나의 최근 게시물에서 영상(릴스)만 뽑는다."""
    username = username.strip().lstrip("@")
    data = limiter.get(PROFILE_API + username, f"acct:{username}", cookies)
    user = ((data.get("data") or {}).get("user")) or data.get("user") or {}
    edges = ((user.get("edge_owner_to_timeline_media") or {}).get("edges")) or []
    reels = []
    for e in edges:
        node = e.get("node") or {}
        if not node.get("is_video"):
            continue
        r = _reel_from_node(node)
        r["source"] = f"@{username}"
        reels.append(r)
    return reels


def _walk_media(obj, found, depth=0):
    """해시태그 응답은 구조가 자주 바뀌어서, 릴스처럼 생긴 조각을 재귀로 찾아낸다."""
    if depth > 8 or len(found) > 60:
        return
    if isinstance(obj, dict):
        # 미디어 한 건처럼 보이면(코드+통계) 담는다
        if (obj.get("code") or obj.get("shortcode")) and (
                "like_count" in obj or "comment_count" in obj or "edge_liked_by" in obj):
            found.append(obj)
        for v in obj.values():
            _walk_media(v, found, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk_media(v, found, depth + 1)


def _reel_from_mobile(m):
    """모바일 API 형태의 미디어(해시태그 응답)를 공통 형식으로."""
    caption = ""
    cap = m.get("caption")
    if isinstance(cap, dict):
        caption = cap.get("text") or ""
    imgs = ((m.get("image_versions2") or {}).get("candidates")) or []
    return {
        "shortcode": m.get("code") or m.get("shortcode"),
        "is_video": m.get("media_type") == 2 or bool(m.get("video_versions")),
        "views": m.get("play_count") or m.get("view_count") or m.get("ig_play_count"),
        "likes": m.get("like_count"),
        "comments": m.get("comment_count"),
        "timestamp": m.get("taken_at") or m.get("taken_at_timestamp"),
        "thumb": imgs[0]["url"] if imgs else m.get("display_url"),
        "caption": caption[:120],
        "url": f"https://www.instagram.com/reel/{m.get('code') or m.get('shortcode')}/",
        "source": None,
    }


def fetch_hashtag_reels(tag, cookies):
    """해시태그 하나에서 최근·인기 영상을 뽑는다 (구조 변화에 견디게 재귀 파싱)."""
    tag = tag.strip().lstrip("#")
    data = limiter.get(HASHTAG_API + tag, f"tag:{tag}", cookies)
    raw = []
    _walk_media(data, raw)
    reels = []
    seen = set()
    for m in raw:
        r = _reel_from_mobile(m)
        if not r["is_video"] or not r["shortcode"] or r["shortcode"] in seen:
            continue
        seen.add(r["shortcode"])
        r["source"] = f"#{tag}"
        reels.append(r)
    return reels


# ── 가변 기준으로 거르고 순위 매기기 ───────────────────────────────

SORTS = {
    "velocity": ("시간당 좋아요", lambda r: r.get("likes_per_hour") or 0),
    "likes": ("좋아요", lambda r: r.get("likes") or 0),
    "comments": ("댓글", lambda r: r.get("comments") or 0),
    "views": ("조회수", lambda r: r.get("views") or 0),
    "newest": ("최신", lambda r: r.get("timestamp") or 0),
}


def rank_reels(reels, max_age_hours=0, min_likes=0, min_comments=0,
               min_views=0, sort_by="velocity"):
    """친구가 정한 가변 기준(예: 최근 8시간 + 댓글 1000개)으로 걸러 순위표를 만든다."""
    now = time.time()
    out, seen = [], set()
    for r in reels:
        ts = r.get("timestamp")
        if not ts or r["shortcode"] in seen:
            continue
        age_h = (now - ts) / 3600
        if max_age_hours and age_h > max_age_hours:
            continue
        if min_likes and (r.get("likes") or 0) < min_likes:
            continue
        if min_comments and (r.get("comments") or 0) < min_comments:
            continue
        if min_views and (r.get("views") or 0) < min_views:
            continue
        seen.add(r["shortcode"])
        r = dict(r)
        r["age_hours"] = round(age_h, 1)
        r["likes_per_hour"] = round((r.get("likes") or 0) / max(age_h, 0.1))
        out.append(r)
    _, keyfn = SORTS.get(sort_by, SORTS["velocity"])
    out.sort(key=keyfn, reverse=True)
    return out


# ── 세션이 살아있는지 확인 (연결 확인 버튼) ────────────────────────

def test_session(browser):
    """요청 1번으로 세션이 실제로 통하는지 본다. 되면 로그인된 계정 이름도 알려준다."""
    cookies = load_cookies(browser)
    # 안정적인 공개 계정 하나를 인증 상태로 조회 → JSON이 오면 세션 정상
    data = limiter.get(PROFILE_API + "instagram", "acct:instagram", cookies)
    user = ((data.get("data") or {}).get("user")) or {}
    if not user:
        raise SessionError("세션은 읽었지만 인스타가 정보를 주지 않았어요. 잠시 후 다시 시도해주세요.")
    return {"ok": True, "ds_user_id": cookies.get("ds_user_id")}
