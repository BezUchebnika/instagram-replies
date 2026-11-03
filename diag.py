"""Диагностика (только чтение): кто написал кодовое слово с SINCE, точный текст,
получил ли автор сообщение от нас после комментария и есть ли наш ответ под комментарием."""
import os
from datetime import datetime, timedelta, timezone
import json
import autoreply as a

CHECKUP_LINK = "Find-Your-Weak-Spot"

MADRID = timezone(timedelta(hours=1))


def main():
    rules = a.load_keywords()
    since = datetime.strptime(os.environ.get("SINCE", "2026-11-02 20:15"), "%Y-%m-%d %H:%M").replace(tzinfo=MADRID)
    # Наши последние переписки: кому и когда мы писали
    convs = a.get_all(f"{a.PAGE_ID}/conversations",
                      {"platform": "instagram", "fields": "participants,updated_time,messages.limit(10){from,created_time,message}", "limit": 50},
                      stop=lambda c: a.parse_ts(c["updated_time"]) < since, max_pages=40)
    last_out = {}
    for cv in convs:
        others = [p.get("username", "").lower() for p in cv.get("participants", {}).get("data", [])
                  if p.get("username", "").lower() != a.OWN_USERNAME]
        for msg in cv.get("messages", {}).get("data", []):
            # Считаем только сообщения со ссылкой на чекап, обычная переписка не в счёт.
            if (msg.get("from", {}).get("username") or "").lower() == a.OWN_USERNAME \
                    and CHECKUP_LINK in (msg.get("message") or ""):
                t = a.parse_ts(msg["created_time"])
                for u in others:
                    last_out[u] = max(last_out.get(u, t), t)
    media = a.api("GET", f"{a.IG_USER_ID}/media", {"fields": "id", "limit": a.MEDIA_TO_SCAN}).get("data", [])
    st = json.loads(a.STATE_FILE.read_text(encoding="utf-8")) if a.STATE_FILE.exists() else {}
    known = set(st.get("sent_users", {})) | set(st.get("skip_users", {}))
    rows = []
    for m in media:
        cs = a.get_all(f"{m['id']}/comments",
                       {"fields": "id,text,timestamp,username,replies{username}", "limit": 50},
                       stop=lambda c: a.parse_ts(c["timestamp"]) < since)
        for c in cs:
            u = (c.get("username") or "").lower()
            if u == a.OWN_USERNAME or a.parse_ts(c["timestamp"]) < since or not a.match(c.get("text"), rules):
                continue
            t = a.parse_ts(c["timestamp"])
            dm = u in last_out and last_out[u] >= t
            pub = any((r.get("username") or "").lower() == a.OWN_USERNAME for r in c.get("replies", {}).get("data", []))
            rows.append((t, c.get("text", "").replace("\n", " ")[:40], dm, pub, u in known))
    rows.sort()
    for t, txt, dm, pub, kn in rows:
        print(f"{t.astimezone(MADRID):%d.%m %H:%M}  директ:{'ДА ' if dm else 'нет'}  ответ:{'ДА ' if pub else 'нет'}  "
              f"раньше писали:{'ДА ' if kn else 'нет'}  «{txt}»")
    print(f"Итого: {len(rows)}, директ получили {sum(r[2] for r in rows)}, публичный ответ {sum(r[3] for r in rows)}")


if __name__ == "__main__":
    main()
