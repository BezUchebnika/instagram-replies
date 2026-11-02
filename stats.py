"""Статистика: сколько людей написали кодовые слова за период. Только чтение, ничего не отправляет.

Запуск: Actions → Instagram stats → Run workflow (дата начала в формате 2026-11-02).
"""

import collections
import os
from datetime import datetime, timedelta, timezone

import autoreply as a

MADRID = timezone(timedelta(hours=1))  # зимнее время Мадрида


def main():
    rules = a.load_keywords()
    since_s = os.environ.get("SINCE") or datetime.now(MADRID).strftime("%Y-%m-%d")
    since = datetime.strptime(since_s, "%Y-%m-%d").replace(tzinfo=MADRID)
    media = a.api("GET", f"{a.IG_USER_ID}/media",
                  {"fields": "id,permalink,timestamp,comments_count", "limit": a.MEDIA_TO_SCAN}).get("data", [])
    users, total, per_media, per_hour = set(), 0, collections.Counter(), collections.Counter()
    links = {}
    for m in media:
        comments = a.get_all(f"{m['id']}/comments",
                             {"fields": "id,text,timestamp,username,replies{id,text,timestamp,username}", "limit": 50},
                             stop=lambda c: a.parse_ts(c["timestamp"]) < since)
        flat = []
        for c in comments:
            flat.append(c)
            flat.extend(c.get("replies", {}).get("data", []))
        for c in flat:
            t = a.parse_ts(c["timestamp"])
            if t < since or (c.get("username") or "").lower() == a.OWN_USERNAME:
                continue
            if a.match(c.get("text"), rules):
                total += 1
                users.add((c.get("username") or "").lower())
                per_media[m["id"]] += 1
                links[m["id"]] = m.get("permalink", m["id"])
                per_hour[t.astimezone(MADRID).strftime("%Y-%m-%d %H:00")] += 1
    now = datetime.now(MADRID).strftime("%Y-%m-%d %H:%M")
    print(f"Период: с {since_s} 00:00 до {now} (Мадрид)")
    print(f"Комментариев с кодовым словом: {total}")
    print(f"Разных людей: {len(users)}")
    print("По публикациям:")
    for mid, n in per_media.most_common():
        print(f"  {n:5d}  {links[mid]}")
    print("По часам:")
    for h in sorted(per_hour):
        print(f"  {h}  {per_hour[h]}")


if __name__ == "__main__":
    main()
