"""
텔레그램 전송기 (self-contained, 의존성 없음)
================================================================
Claude가 작성한 메시지 파일을 텔레그램 채널로 전송.
- 토큰/채널: 환경변수 TELEGRAM_BOT_TOKEN, TELEGRAM_XTRACK_CHAT_ID
- 인증서: 클라우드 TLS 가로채기 프록시 우회 (검증 비활성 컨텍스트)
- 4096자 초과 시 빈 줄(\n\n) 경계로 분할 전송
- parse_mode=HTML, disable_web_page_preview=true

사용:
    python send_tg.py message.txt
"""
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

MAX = 4000  # 4096 여유


def normalize_chat_id(raw: str) -> str:
    """t.me/X 또는 @X → @X. -100... 숫자면 그대로."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.lstrip("-").isdigit():
        return raw  # 숫자 채널 ID
    if "t.me/" in raw:
        raw = raw.split("t.me/")[-1]
    raw = raw.lstrip("@").strip("/")
    return "@" + raw


def split_message(text: str) -> list:
    """4096자 제한 고려, \n\n 경계로 청크 분할."""
    blocks = text.split("\n\n")
    chunks, cur = [], ""
    for b in blocks:
        add = (("\n\n" if cur else "") + b)
        if len(cur) + len(add) > MAX and cur:
            chunks.append(cur)
            cur = b
        else:
            cur += add
    if cur:
        chunks.append(cur)
    return chunks


def send_chunk(token: str, chat_id: str, text: str, ctx) -> tuple:
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            body = r.read().decode()
            ok = json.loads(body).get("ok", False)
            return ok, body[:200]
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def main():
    msg_path = sys.argv[1] if len(sys.argv) > 1 else "message.txt"
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = normalize_chat_id(os.environ.get("TELEGRAM_XTRACK_CHAT_ID", ""))

    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN 없음")
        sys.exit(2)
    if not chat_id:
        print("채널 미설정(TELEGRAM_XTRACK_CHAT_ID 비어있음) → 전송 생략")
        sys.exit(0)

    with open(msg_path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        print("메시지 비어있음 → 전송 생략")
        sys.exit(0)

    # 프록시 인증서 우회용 비검증 SSL 컨텍스트
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    chunks = split_message(text)
    print(f"채널 {chat_id} 로 {len(chunks)}개 청크 전송...")
    ok_all = True
    for i, ch in enumerate(chunks, 1):
        ok, info = send_chunk(token, chat_id, ch, ctx)
        print(f"  청크 {i}/{len(chunks)}: {'OK' if ok else 'FAIL ' + info}")
        if not ok:
            ok_all = False
    print("전송 완료" if ok_all else "일부 전송 실패")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
