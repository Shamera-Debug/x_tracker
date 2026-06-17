"""
X 계정 트래커 — 클라우드 수집기 (시간창 방식, 상태 저장 없음)
================================================================
@dmjk001 + @jukan05 의 "직전 1시간 슬롯" 트윗만 수집 → new_tweets.json
- 1시간마다 실행되는 루틴 전제. 매 실행은 [직전 정시, 현재 정시) 구간 트윗만 채택.
- 상태 파일/토큰/ git push 불필요 (중복은 절대 시각 기준 비겹침 구간으로 방지).

타임존 안전성:
- X created_at 은 UTC(+0000) → _parse_created_at 이 절대 unix ts 로 변환.
- 윈도우 경계도 UTC 정시로 계산 후 .timestamp()(절대 ts) 비교 → 타임존 혼선 없음.
- created_kst 는 사람이 보기 위한 표시용일 뿐, 판정엔 ts 만 사용.

쿠키: 환경변수 X_COOKIES_JSON ({"auth_token":"...","ct0":"..."})
인증서: 클라우드 TLS 프록시 우회 (--ignore-certificate-errors)

산출물 new_tweets.json:
  {"collected_at_kst","window_kst","window_utc","total_new","new":{acc:[...]}}
"""
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
_HERE = Path(__file__).parent
OUT_PATH = _HERE / "new_tweets.json"
ACCOUNTS = ["dmjk001", "jukan05"]
GRAPHQL = ["/UserTweets", "/UserByScreenName", "/SearchTimeline"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
WINDOW_HOURS = 1  # 루틴 실행 주기와 일치


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


def _parse_created_at(s: str) -> int:
    """X 타임스탬프 'Fri Apr 25 14:32:01 +0000 2026'(UTC) → 절대 unix sec."""
    if not s:
        return 0
    try:
        return int(datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").timestamp())
    except Exception:
        return 0


def _extract_tweets(obj, found: dict):
    if isinstance(obj, dict):
        if obj.get("__typename") == "Tweet" and isinstance(obj.get("legacy"), dict):
            try:
                legacy = obj["legacy"]
                ur = obj.get("core", {}).get("user_results", {}).get("result", {})
                screen = (ur.get("core", {}).get("screen_name")
                          or ur.get("legacy", {}).get("screen_name", ""))
                tid = legacy.get("id_str") or obj.get("rest_id", "")
                if tid and screen and tid not in found:
                    found[tid] = Tweet(
                        id=str(tid), author=screen,
                        text=legacy.get("full_text") or legacy.get("text", ""),
                        created_at_ts=_parse_created_at(legacy.get("created_at", "")),
                        favorite_count=int(legacy.get("favorite_count", 0) or 0),
                        retweet_count=int(legacy.get("retweet_count", 0) or 0),
                        reply_count=int(legacy.get("reply_count", 0) or 0),
                        url=f"https://x.com/{screen}/status/{tid}",
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
    """[직전 정시, 현재 정시) 윈도우를 절대 ts 로 반환.
    +5분 버퍼로 정시 직전/직후 지터를 흡수해 슬롯을 올바르게 판정."""
    now = datetime.now(UTC)
    cur_hour = (now + timedelta(minutes=5)).replace(minute=0, second=0, microsecond=0)
    low_dt = cur_hour - timedelta(hours=WINDOW_HOURS)
    high_dt = cur_hour
    return low_dt, high_dt


async def fetch_all() -> dict:
    cookies = load_cookies()
    result = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--ignore-certificate-errors"])
        ctx = await browser.new_context(
            user_agent=UA, viewport={"width": 1280, "height": 900},
            ignore_https_errors=True)
        cobjs = []
        for n, v in cookies.items():
            for dom in [".x.com", ".twitter.com"]:
                cobjs.append({"name": n, "value": str(v), "domain": dom, "path": "/",
                              "secure": True, "httpOnly": n == "auth_token",
                              "sameSite": "Lax"})
        await ctx.add_cookies(cobjs)
        page = await ctx.new_page()

        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        if "/login" in page.url or "/i/flow/login" in page.url:
            await browser.close()
            print("ERROR: X 쿠키 무효(로그인 리다이렉트) — 쿠키 재추출 필요")
            sys.exit(3)
        print(f"로그인 OK: {page.url}")

        for i, acc in enumerate(ACCOUNTS):
            if i > 0:
                await asyncio.sleep(6)
            captured = []

            async def on_resp(r, _c=captured):
                if any(p in r.url for p in GRAPHQL):
                    try:
                        _c.append(await r.json())
                    except Exception:
                        pass

            page.on("response", on_resp)
            try:
                await page.goto(f"https://x.com/{acc}", wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)
                for _ in range(4):
                    await page.evaluate("window.scrollBy(0, 1800)")
                    await asyncio.sleep(2.5)
            except Exception as e:
                print(f"  @{acc} nav 실패: {str(e)[:80]}")
            page.remove_listener("response", on_resp)

            found = {}
            for d in captured:
                _extract_tweets(d, found)
            rows = []
            for t in found.values():
                if t.author.lower() != acc.lower():
                    continue
                rows.append({
                    "id": t.id, "author": t.author, "text": t.text,
                    "created_ts": t.created_at_ts,
                    "created_kst": datetime.fromtimestamp(t.created_at_ts, tz=KST).strftime("%Y-%m-%d %H:%M") if t.created_at_ts else "",
                    "fav": t.favorite_count, "rt": t.retweet_count,
                    "reply": t.reply_count, "url": t.url,
                })
            rows.sort(key=lambda x: x["created_ts"], reverse=True)
            print(f"  @{acc}: {len(rows)}개 수집")
            result[acc] = rows
        await browser.close()
    return result


async def main():
    low_dt, high_dt = compute_window()
    low_ts, high_ts = low_dt.timestamp(), high_dt.timestamp()
    now_kst = datetime.now(KST)
    win_kst = f"{low_dt.astimezone(KST):%Y-%m-%d %H:%M} ~ {high_dt.astimezone(KST):%H:%M} KST"
    win_utc = f"{low_dt:%Y-%m-%d %H:%M} ~ {high_dt:%H:%M} UTC"
    print(f"=== 수집 ({now_kst:%Y-%m-%d %H:%M} KST) | 윈도우: {win_kst} ===")

    fetched = await fetch_all()

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
    asyncio.run(main())
