"""
X 계정 트래커 — 클라우드 수집기 (self-contained)
================================================================
@dmjk001 + @jukan05 신규 트윗만 수집 → new_tweets.json
- 쿠키: 환경변수 X_COOKIES_JSON ({"auth_token":"...","ct0":"..."})
- 인증서: 클라우드 TLS 가로채기 프록시 우회 (--ignore-certificate-errors)
- 상태: xtrack_state.json (리포에 커밋해 실행 간 보존). 비어있으면 자동 baseline(전송 없음)

산출물 new_tweets.json:
  {"collected_at_kst": "...", "accounts": [...], "total_new": N,
   "new": {"dmjk001":[...], "jukan05":[...]}}
각 트윗: id, author, text, created_kst, fav, rt, reply, url
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
_HERE = Path(__file__).parent
STATE_PATH = _HERE / "xtrack_state.json"
OUT_PATH = _HERE / "new_tweets.json"
ACCOUNTS = ["dmjk001", "jukan05"]
GRAPHQL = ["/UserTweets", "/UserByScreenName", "/SearchTimeline"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ───────────── 트윗 추출 (src/x_playwright.py 로직 인라인) ─────────────
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


# ───────────── 상태 ─────────────
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"accounts": {}, "last_run": ""}


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ───────────── 쿠키 ─────────────
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


# ───────────── X 수집 ─────────────
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

        # 로그인 검증
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
                    continue  # 본인 원글만
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


def write_out(now: datetime, new_by_acc: dict):
    payload = {
        "collected_at_kst": now.strftime("%Y-%m-%d %H:%M"),
        "accounts": ACCOUNTS,
        "total_new": sum(len(v) for v in new_by_acc.values()),
        "new": new_by_acc,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def main():
    now = datetime.now(KST)
    print(f"=== 수집 ({now.strftime('%Y-%m-%d %H:%M')} KST) ===")
    state = load_state()

    fetched = await fetch_all()

    # 최초 실행(상태 비어있음) → baseline: 전부 seen 처리, 전송 없음
    if not state.get("accounts"):
        print("상태 비어있음 → 자동 BASELINE (이번엔 전송 없음)")
        for acc, rows in fetched.items():
            state["accounts"][acc] = {"seen": [r["id"] for r in rows][:500]}
        state["last_run"] = now.isoformat(timespec="seconds")
        save_state(state)
        write_out(now, {acc: [] for acc in ACCOUNTS})
        print("BASELINE 완료. total_new=0")
        return

    new_by_acc = {}
    for acc, rows in fetched.items():
        seen = set(state["accounts"].get(acc, {}).get("seen", []))
        new_rows = [r for r in rows if r["id"] not in seen]
        new_by_acc[acc] = new_rows
        merged = list(dict.fromkeys([r["id"] for r in rows] + list(seen)))[:500]
        state["accounts"].setdefault(acc, {"seen": []})["seen"] = merged
        print(f"  @{acc}: 신규 {len(new_rows)}개 / 전체 {len(rows)}개")

    state["last_run"] = now.isoformat(timespec="seconds")
    save_state(state)
    write_out(now, new_by_acc)
    total = sum(len(v) for v in new_by_acc.values())
    print(f"신규 총 {total}개 → {OUT_PATH.name}")


if __name__ == "__main__":
    asyncio.run(main())
