"""Только публичные ответы под комментариями с кодовым словом. Директ НЕ отправляется:
его отправляет встроенная автоматизация Meta («Comment to message»).

Правила безопасности:
- отвечаем только если автору уже ушёл директ от нашего аккаунта (Meta или бот);
- не отвечаем, если под комментарием уже есть ответ нашего аккаунта;
- не больше MAX_PER_RUN за запуск и DAILY_CAP в сутки, пауза между ответами;
- 2 ошибки подряд или ошибка лимита/доступа: остановка до следующего запуска (красный запуск).
"""
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import autoreply as a

STATE = Path(__file__).parent / "public_state.json"
MAX_PER_RUN = 3
DAILY_CAP = 60
PAUSE = (20, 40)  # секунд между ответами
WAIT_FOR_DM = timedelta(hours=2)  # сколько ждём директ от Meta, прежде чем пропустить
LOOKBACK = timedelta(days=3)  # новые комментарии не старше
FIRST_SINCE = "2026-11-02T20:15:00+0100"
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")


def log(m):
    print(m, flush=True)


def outgoing_dm_times(days=8):
    """username -> время последнего сообщения от нашего аккаунта."""
    since = a.now_utc() - timedelta(days=days)
    convs = a.get_all(f"{a.PAGE_ID}/conversations",
                      {"platform": "instagram",
                       "fields": "participants,updated_time,messages.limit(5){from,created_time}", "limit": 50},
                      stop=lambda c: a.parse_ts(c["updated_time"]) < since, max_pages=30)
    out = {}
    for cv in convs:
        others = [p.get("username", "").lower() for p in cv.get("participants", {}).get("data", [])
                  if p.get("username", "").lower() != a.OWN_USERNAME]
        for msg in cv.get("messages", {}).get("data", []):
            if (msg.get("from", {}).get("username") or "").lower() == a.OWN_USERNAME:
                t = a.parse_ts(msg["created_time"])
                for u in others:
                    out[u] = max(out.get(u, t), t)
    return out


def main():
    if not a.TOKEN:
        sys.exit("Нет ключа IG_PAGE_TOKEN")
    rules = a.load_keywords()
    now = a.now_utc()
    today = now.strftime("%Y-%m-%d")
    if STATE.exists():
        st = json.loads(STATE.read_text(encoding="utf-8"))
    else:
        old = []
        if a.STATE_FILE.exists():  # хвост вчерашней очереди: им директ ушёл от бота
            old = json.loads(a.STATE_FILE.read_text(encoding="utf-8")).get("public_queue", [])
        st = {"since": FIRST_SINCE, "handled": [], "queue": old, "daily": {}}
        log(f"Первый запуск: в очереди {len(old)} ответов со вчерашнего дня")
    handled = set(st["handled"])
    queued = {q["comment_id"] for q in st["queue"]}
    since = max(a.parse_ts(st["since"]), now - LOOKBACK)
    done_today = st["daily"].get(today, 0)

    def mark(cid):
        if cid not in handled:
            handled.add(cid)
            st["handled"].append(cid)

    dms = outgoing_dm_times()
    log(f"Директ от нашего аккаунта за 8 дней: {len(dms)} человек")

    # 1. Новые комментарии с кодовым словом -> в очередь (если директ уже ушёл)
    media = a.api("GET", f"{a.IG_USER_ID}/media", {"fields": "id", "limit": a.MEDIA_TO_SCAN}).get("data", [])
    new_n = waiting = no_dm = 0
    for m in media:
        cs = a.get_all(f"{m['id']}/comments",
                       {"fields": "id,text,timestamp,username,replies{username}", "limit": 50},
                       stop=lambda c: a.parse_ts(c["timestamp"]) < since)
        for c in cs:
            cid = c["id"]
            u = (c.get("username") or "").lower()
            if cid in handled or cid in queued or u == a.OWN_USERNAME:
                continue
            t = a.parse_ts(c["timestamp"])
            if t < since or not a.match(c.get("text"), rules):
                continue
            if any((r.get("username") or "").lower() == a.OWN_USERNAME for r in c.get("replies", {}).get("data", [])):
                mark(cid)  # уже ответили
                continue
            if u in dms:
                st["queue"].insert(0, {"comment_id": cid, "author": c.get("username"), "rule": "чекап", "new": True})
                queued.add(cid)
                mark(cid)
                new_n += 1
            elif now - t < WAIT_FOR_DM:
                waiting += 1  # Meta ещё может прислать директ, проверим в следующий раз
            else:
                no_dm += 1
                mark(cid)  # директ так и не ушёл: публично не отвечаем
    log(f"Новых в очередь: {new_n}, ждут директа от Meta: {waiting}, без директа (пропущены): {no_dm}")

    # 2. Публикуем понемногу
    posted, errors, stopped = 0, 0, None
    rule = next(iter(rules.values()), None)
    replies = rule["comment_replies"] if rule else []
    while st["queue"] and posted < MAX_PER_RUN and done_today + posted < DAILY_CAP and replies:
        item = st["queue"][0]
        target = item.get("parent") or item["comment_id"]
        text = random.choice(replies)
        if item.get("parent"):
            text = f"@{item['author']} {text}"
        try:
            if not item.get("parent"):
                info = a.api("GET", item["comment_id"], {"fields": "replies{username}"})
                if any((r.get("username") or "").lower() == a.OWN_USERNAME
                       for r in info.get("replies", {}).get("data", [])):
                    st["queue"].pop(0)
                    continue
            if DRY_RUN:
                log(f"[проверка] ответил бы @{item['author']}: {text}")
            else:
                a.api("POST", f"{target}/replies", {"message": text})
                log(f"Ответ под комментарием @{item['author']}")
            posted += 1
            errors = 0
            st["queue"].pop(0)
            time.sleep(random.uniform(*PAUSE))
        except a.StopSending as e:
            stopped = str(e)
            break
        except RuntimeError as e:
            errors += 1
            log(f"Ошибка @{item['author']}: {e}")
            if '"code":100' in str(e) and "does not exist" in str(e):
                st["queue"].pop(0)  # комментарий удалён
            if errors >= 2:
                stopped = f"2 ошибки подряд: {e}"
                break

    if not DRY_RUN:
        st["daily"] = {today: done_today + posted}
        st["handled"] = st["handled"][-20000:]
        st["since"] = (now - WAIT_FOR_DM).strftime("%Y-%m-%dT%H:%M:%S%z")
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log(f"Готово. Ответов: {posted}, сегодня всего: {done_today + posted}/{DAILY_CAP}, в очереди: {len(st['queue'])}")
    if stopped:
        log(f"ОСТАНОВКА: {stopped}")
        sys.exit(1)


if __name__ == "__main__":
    main()
