"""
X 계정 트래커 — 클라우드 수집기 (시간창 방식, 상태 저장 없음)
================================================================
@dmjk001 + @jukan05 의 "직전 슬롯" 트윗만 수집 → new_tweets.json
- WINDOW_HOURS(현재 3)마다 실행되는 루틴 전제. 매 실행은 [직전 슬롯, 현재 슬롯) 구간만 채택.
- 상태 파일/토큰/ git push 불필요 (중복은 절대 시각 기준 비겹침 구간으로 방지).

타임존 안전성:
- X created_at 은 UTC(+0000) → _parse_created_at 이 절대 unix ts 로 변환.
- 윈도우 경계도 UTC 정시로 계산 후 .timestamp()(절대 ts) 비교 → 타임존 혼선 없음.
- created_kst 는 사람이 보기 위한 표시용일 뿐, 판정엔 ts 만 사용.

쿠키: 환경변수 X_COOKIES_JSON ({"auth_token":"...","ct0":"..."})

수집 방식: 브라우저(Playwright) 대신 X GraphQL 웹 API 를 HTTP(requests)로 직접 호출.
  - 클라우드 에이전트 프록시 환경에서 헤드리스 Chromium 은 프록시 경유 연결이
    net::ERR_CONNECTION_CLOSED 로 끊긴다(프록시가 브라우저 TLS 만 차단). requests 는
    HTTPS_PROXY / REQUESTS_CA_BUNDLE 환경변수를 자동 인식해 x.com 에 정상 도달한다.
  - 더 가볍고(브라우저 무구동) 빠르며 로컬에서도 그대로 동작.
  - X 가 GraphQL queryId 를 교체하면 *_QID 상수만 갱신하면 된다(쿠키 만료와 같은 등급의 유지보수).

산출물 new_tweets.json:
  {"collected_at_kst","window_kst","window_utc","total_new","new":{acc:[...]}}
"""
import json
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
_HERE = Path(__file__).parent
OUT_PATH = _HERE / "new_tweets.json"
ACCOUNTS = ["dmjk001", "jukan05"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
WINDOW_HOURS = 3  # 루틴 실행 주기(cron 0 */3 * * *)와 반드시 일치. 3시간 격자는 KST·UTC 양쪽 정렬됨.

# --- X GraphQL 웹 API 상수 ---------------------------------------------------
# 공개 웹앱 베어러(고정값) + GraphQL queryId. X 가 queryId 를 교체하면 아래만 갱신.
WEB_BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
              "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
USER_BY_SCREEN_NAME_QID = "1VOOyvKkiI3FMmkeDNxM9A"
USER_TWEETS_QID = "E3opETHurmVJflFsUBVuUQ"
TWEETS_PER_ACCOUNT = 40  # 3시간 창 + 핀고정/리트윗 여유. 보통 1페이지로 충분.

FEAT_USER = {
    "hidden_profile_subscriptions_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}
FEAT_TWEETS = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


class CookieExpired(Exception):
    """X 쿠키 만료/무효 — 재추출 필요."""


@dataclass
class Tweet:
    id: str
    author: str
    text: str
    created_at_ts: int
    favorite_count: int
    retweet_count: int
    reply_count: int
    url: str
    quoted_id: str = ""   # 인용한 원글 id
    rt_id: str = ""       # 리트윗한 원글 id


def _parse_created_at(s: str) -> int:
    """X 타임스탬프 'Fri Apr 25 14:32:01 +0000 2026'(UTC) → 절대 unix sec."""
    if not s:
        return 0
    try:
        return int(datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").timestamp())
    except Exception:
        return 0


def _best_text(res: dict) -> str:
    """긴 글(note tweet)이면 전체 텍스트, 아니면 legacy full_text."""
    nt = ((res.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    if isinstance(nt, dict) and nt.get("text"):
        return nt["text"]
    legacy = res.get("legacy") or {}
    return legacy.get("full_text") or legacy.get("text", "")


def _screen_name(res: dict) -> str:
    ur = (res.get("core") or {}).get("user_results", {}).get("result", {})
    return ((ur.get("core") or {}).get("screen_name")
            or (ur.get("legacy") or {}).get("screen_name", ""))


def _extract_tweets(obj, found: dict):
    if isinstance(obj, dict):
        if obj.get("__typename") == "Tweet" and isinstance(obj.get("legacy"), dict):
            try:
                legacy = obj["legacy"]
                screen = _screen_name(obj)
                tid = legacy.get("id_str") or obj.get("rest_id", "")
                if tid and screen and tid not in found:
                    # 인용/리트윗 원글 id 추출
                    quoted_id = legacy.get("quoted_status_id_str", "") or ""
                    rt_id = ""
                    rtr = (legacy.get("retweeted_status_result") or {}).get("result") or {}
                    if isinstance(rtr, dict):
                        rt_id = rtr.get("rest_id", "") or (rtr.get("legacy") or {}).get("id_str", "")
                    found[tid] = Tweet(
                        id=str(tid), author=screen, text=_best_text(obj),
                        created_at_ts=_parse_created_at(legacy.get("created_at", "")),
                        favorite_count=int(legacy.get("favorite_count", 0) or 0),
                        retweet_count=int(legacy.get("retweet_count", 0) or 0),
                        reply_count=int(legacy.get("reply_count", 0) or 0),
                        url=f"https://x.com/{screen}/status/{tid}",
                        quoted_id=str(quoted_id), rt_id=str(rt_id),
                    )
            except Exception:
                pass
        for v in obj.values():
            _extract_tweets(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _extract_tweets(v, found)


def load_cookies() -> dict:
    raw = os.environ.get("X_COOKIES_JSON", "").strip()
    if not raw:
        print("ERROR: 환경변수 X_COOKIES_JSON 비어 있음")
        sys.exit(2)
    c = json.loads(raw)
    if "auth_token" not in c:
        print("ERROR: X_COOKIES_JSON 에 auth_token 없음")
        sys.exit(2)
    return c


def compute_window():
    """[직전 슬롯, 현재 슬롯) 윈도우를 절대 ts 로 반환.
    실행 주기(WINDOW_HOURS)와 동일한 N시간 경계(UTC 0시 기준)로 정렬.
    cron `0 */N * * *` 와 짝이 맞아야 중복/누락이 없다.
    +5분 버퍼로 정시 직전/직후 지터를 흡수."""
    now = datetime.now(UTC) + timedelta(minutes=5)
    h = (now.hour // WINDOW_HOURS) * WINDOW_HOURS   # N시간 경계로 내림
    cur_slot = now.replace(hour=h, minute=0, second=0, microsecond=0)
    low_dt = cur_slot - timedelta(hours=WINDOW_HOURS)
    return low_dt, cur_slot


def _make_session(cookies: dict) -> requests.Session:
    """인증 쿠키/헤더가 박힌 requests 세션. HTTPS_PROXY·REQUESTS_CA_BUNDLE 는
    requests 가 환경변수에서 자동 인식(클라우드 프록시·CA 그대로 적용)."""
    auth, ct0 = cookies["auth_token"], cookies["ct0"]
    s = requests.Session()
    s.headers.update({
        "authorization": f"Bearer {WEB_BEARER}",
        "x-csrf-token": ct0,
        "cookie": f"auth_token={auth}; ct0={ct0}",
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "content-type": "application/json",
        "referer": "https://x.com/",
        "user-agent": UA,
    })
    return s


def _gql_get(s: requests.Session, qid: str, op: str, variables: dict, features: dict) -> dict:
    """X GraphQL GET. 인증 실패면 CookieExpired, 그 외 비정상이면 RuntimeError."""
    url = f"https://x.com/i/api/graphql/{qid}/{op}?" + urllib.parse.urlencode({
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(features, separators=(",", ":")),
    })
    last_err = None
    for attempt in range(3):
        try:
            r = s.get(url, timeout=25)
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise CookieExpired(f"{op} HTTP {r.status_code}")
        if r.status_code == 429:
            last_err = RuntimeError(f"{op} rate-limited (429)")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code != 200:
            last_err = RuntimeError(f"{op} HTTP {r.status_code}: {r.text[:160]}")
            time.sleep(2 * (attempt + 1))
            continue
        data = r.json()
        errs = data.get("errors") or []
        # code 32/63/64/89/215 류 = 인증/세션 무효
        if errs and any(int(e.get("code", 0)) in (32, 63, 64, 89, 215) for e in errs):
            raise CookieExpired(f"{op}: {errs[0].get('message','auth error')}")
        return data
    raise last_err or RuntimeError(f"{op} 실패")


def _user_id(s: requests.Session, screen_name: str) -> str:
    data = _gql_get(s, USER_BY_SCREEN_NAME_QID, "UserByScreenName",
                    {"screen_name": screen_name}, FEAT_USER)
    res = (((data.get("data") or {}).get("user") or {}).get("result") or {})
    uid = res.get("rest_id")
    if not uid:
        raise RuntimeError(f"@{screen_name} rest_id 없음(계정 비공개/정지 가능)")
    return uid


def fetch_all() -> dict:
    cookies = load_cookies()
    s = _make_session(cookies)
    result = {}
    for i, acc in enumerate(ACCOUNTS):
        if i > 0:
            time.sleep(1.5)
        uid = _user_id(s, acc)
        data = _gql_get(s, USER_TWEETS_QID, "UserTweets", {
            "userId": uid, "count": TWEETS_PER_ACCOUNT,
            "includePromotedContent": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True, "withV2Timeline": True,
        }, FEAT_TWEETS)

        found = {}
        _extract_tweets(data, found)
        rows = []
        for t in found.values():
            if t.author.lower() != acc.lower():
                continue  # 본인 게시물만 (인용/리트윗 원글은 아래에서 ref로 첨부)
            # 인용·리트윗된 원글 내용 첨부 (found 안에 이미 추출돼 있음)
            ref_id = t.rt_id or t.quoted_id
            ref = found.get(ref_id) if ref_id else None
            kind = "retweet" if t.rt_id else ("quote" if t.quoted_id else "")
            rows.append({
                "id": t.id, "author": t.author, "text": t.text,
                "created_ts": t.created_at_ts,
                "created_kst": datetime.fromtimestamp(t.created_at_ts, tz=KST).strftime("%Y-%m-%d %H:%M") if t.created_at_ts else "",
                "fav": t.favorite_count, "rt": t.retweet_count,
                "reply": t.reply_count, "url": t.url,
                "repost_kind": kind,                       # "", "quote", "retweet"
                "ref_author": ref.author if ref else "",   # 인용/리트윗된 원글 작성자
                "ref_text": ref.text if ref else "",       # 원글 본문 (핵심 내용)
                "ref_url": ref.url if ref else "",          # 원글 링크
            })
        rows.sort(key=lambda x: x["created_ts"], reverse=True)
        print(f"  @{acc}: {len(rows)}개 수집")
        result[acc] = rows
    return result


def main():
    low_dt, high_dt = compute_window()
    low_ts, high_ts = low_dt.timestamp(), high_dt.timestamp()
    now_kst = datetime.now(KST)
    win_kst = f"{low_dt.astimezone(KST):%Y-%m-%d %H:%M} ~ {high_dt.astimezone(KST):%H:%M} KST"
    win_utc = f"{low_dt:%Y-%m-%d %H:%M} ~ {high_dt:%H:%M} UTC"
    print(f"=== 수집 ({now_kst:%Y-%m-%d %H:%M} KST) | 윈도우: {win_kst} ===")

    try:
        fetched = fetch_all()
    except CookieExpired as e:
        print(f"ERROR: X 쿠키 무효 — 쿠키 재추출 필요 ({e})")
        sys.exit(3)

    new_by_acc = {}
    for acc, rows in fetched.items():
        new_rows = [r for r in rows if low_ts <= r["created_ts"] < high_ts]
        new_by_acc[acc] = new_rows
        ages = [f"{r['created_kst']}" for r in new_rows]
        print(f"  @{acc}: 윈도우 내 {len(new_rows)}개 / 전체 {len(rows)}개 {ages}")

    payload = {
        "collected_at_kst": now_kst.strftime("%Y-%m-%d %H:%M"),
        "window_kst": win_kst,
        "window_utc": win_utc,
        "accounts": ACCOUNTS,
        "total_new": sum(len(v) for v in new_by_acc.values()),
        "new": new_by_acc,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"신규(윈도우 내) 총 {payload['total_new']}개 → {OUT_PATH.name}")


if __name__ == "__main__":
    main()
