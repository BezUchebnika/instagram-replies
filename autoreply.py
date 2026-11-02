"""Автоответ на кодовые слова в комментариях Instagram (bezuchebnikaeng).

Раз в несколько минут (через GitHub Actions) скрипт:
1. смотрит последние публикации аккаунта и находит те, где выросло число комментариев;
2. читает новые комментарии (и ответы в ветках);
3. если в комментарии есть кодовое слово из keywords.yml, отправляет автору
   одно личное сообщение (Private Reply) через Instagram API;
4. запоминает обработанные комментарии в state.json, чтобы не писать дважды.

Ключ доступа берётся из секрета репозитория IG_PAGE_TOKEN.
"""

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

API = "https://graph.facebook.com/v26.0"
IG_USER_ID = "17841479961455146"  # bezuchebnikaeng
PAGE_ID = "1370667766133512"  # Facebook-страница, привязанная к Instagram
OWN_USERNAME = "bezuchebnikaeng"

ROOT = Path(__file__).parent
KEYWORDS_FILE = ROOT / "keywords.yml"
STATE_FILE = ROOT / "state.json"

MEDIA_TO_SCAN = 50  # сколько последних публикаций проверять
REPLY_WINDOW = timedelta(days=7)  # Meta разрешает личный ответ в течение 7 дней
MAX_SENDS_PER_RUN = 40  # до ~240 в час при запуске раз в 10 минут; лимит Meta 750
PAUSE_SECONDS = (3, 5)  # случайная пауза между отправками
REMEMBER_IDS = 20000  # сколько id обработанных комментариев хранить

TOKEN = os.environ.get("IG_PAGE_TOKEN", "")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
# Что делать, если state.json ещё нет: remember (только запомнить) или backlog
# (ответить за последние 7 дней). Пусто: ничего не делать, ждать ручного запуска.
FIRST_RUN = os.environ.get("FIRST_RUN", "")


def log(msg):
    print(msg, flush=True)


def api(method, path, params=None, body=None):
    params = dict(params or {})
    params["access_token"] = TOKEN
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from None


def get_all(path, params, stop=None):
    """Постранично собирает список; stop(item) -> True прекращает чтение."""
    out = []
    data = api("GET", path, params)
    while True:
        for item in data.get("data", []):
            if stop and stop(item):
                return out
            out.append(item)
        nxt = data.get("paging", {}).get("next")
        if not nxt:
            return out
        with urllib.request.urlopen(nxt, timeout=30) as resp:
            data = json.loads(resp.read())


# Латинские буквы, которые выглядят как русские. В словах, где есть кириллица,
# их заменяем на русские: «чекaп» с латинской «a» = «чекап».
LOOKALIKES = str.maketrans("aceopxkmtyb", "асеорхкмтуь")
CYRILLIC = re.compile(r"[а-я]")


def normalize(text):
    text = (text or "").lower().replace("ё", "е")
    words = re.sub(r"[^\w]+", " ", text).split()
    words = [w.translate(LOOKALIKES) if CYRILLIC.search(w) else w for w in words]
    return " ".join(words)


def load_keywords():
    cfg = yaml.safe_load(KEYWORDS_FILE.read_text(encoding="utf-8")) or {}
    rules = []
    for name, entry in (cfg.get("keywords") or {}).items():
        entry = entry or {}
        message = (entry.get("message") or "").strip()
        if not message or entry.get("enabled") is False:
            log(f"Кодовое слово «{name}» пропущено: выключено или нет текста сообщения")
            continue
        words = [name] + list(entry.get("also") or [])
        rules.append(
            {
                "name": name,
                "words": [normalize(w) for w in words],
                "message": message,
                "comment_replies": [
                    r.strip() for r in (entry.get("comment_replies") or []) if r and r.strip()
                ],
            }
        )
    return rules


def match(text, rules):
    norm = f" {normalize(text)} "
    for rule in rules:
        for w in rule["words"]:
            if w and f" {w} " in norm:
                return rule
    return None


def post_public_reply(comment, author, rule):
    """Публичный ответ под комментарием. Ошибка здесь не мешает директу."""
    if not rule["comment_replies"]:
        return
    text = random.choice(rule["comment_replies"])
    target = comment["id"]
    if comment.get("_parent"):
        # На ответ в ветке Instagram отвечать не даёт: отвечаем в ту же ветку с упоминанием.
        target = comment["_parent"]
        text = f"@{author} {text}"
    try:
        api("POST", f"{target}/replies", {"message": text})
    except RuntimeError as e:
        log(f"Публичный ответ @{author} не опубликован: {e}")


def parse_ts(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(state):
    state["handled"] = state["handled"][-REMEMBER_IDS:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def recent_conversation_usernames(days=8):
    """Ники людей, с которыми за последние дни уже была переписка в директе
    (Артём писал вручную или сработал Instant Reply). Им старый чекап не шлём."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    convs = get_all(
        f"{PAGE_ID}/conversations",
        {"platform": "instagram", "fields": "participants,updated_time", "limit": 50},
        stop=lambda c: parse_ts(c["updated_time"]) < since,
    )
    names = set()
    for conv in convs:
        for p in conv.get("participants", {}).get("data", []):
            if p.get("username") and p["username"] != OWN_USERNAME:
                names.add(p["username"].lower())
    log(f"Переписок за последние {days} дн.: {len(convs)}, людей: {len(names)}")
    return names


def main():
    if not TOKEN:
        sys.exit("Нет ключа: секрет IG_PAGE_TOKEN не задан")

    rules = load_keywords()
    state = load_state()
    first_run = state is None
    backlog = False
    if first_run:
        if FIRST_RUN == "remember":
            # Всё, что уже есть, считаем обработанным, ничего не отправляем.
            log("Первый запуск: запоминаю существующие комментарии без отправки")
        elif FIRST_RUN == "backlog":
            # Отвечаем на кодовые слова за последние 7 дней, кроме тех,
            # с кем уже есть переписка в директе.
            log("Первый запуск: отвечаю на комментарии за последние 7 дней")
            backlog = True
        else:
            log("Ждём первого ручного запуска (Run workflow): пока ничего не делаю")
            return
        state = {"handled": [], "comment_counts": {}, "sent_users": {}}
    state.setdefault("sent_users", {})

    already_talking = recent_conversation_usernames() if backlog else set()
    remember_only = first_run and not backlog

    handled = set(state["handled"])
    counts = state["comment_counts"]
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    # Кому уже отправляли за последние 7 дней: второй раз тому же человеку не шлём.
    week_ago = (now - REPLY_WINDOW).strftime("%Y-%m-%d")
    sent_users = {u: d for u, d in state["sent_users"].items() if d >= week_ago}
    sent = would_send = skipped_old = skipped_talking = skipped_repeat = 0

    def done(cid):
        handled.add(cid)
        state["handled"].append(cid)

    media = api(
        "GET",
        f"{IG_USER_ID}/media",
        {"fields": "id,comments_count,timestamp,permalink", "limit": MEDIA_TO_SCAN},
    ).get("data", [])

    for m in media:
        mid = m["id"]
        total = m.get("comments_count", 0)
        if not first_run and counts.get(mid) == total:
            continue  # новых комментариев нет

        comments = get_all(
            f"{mid}/comments",
            {
                "fields": "id,text,timestamp,username,from,"
                "replies{id,text,timestamp,username,from}",
                "limit": 50,
            },
        )
        flat = []
        for c in comments:
            flat.append(c)
            for r in c.get("replies", {}).get("data", []):
                r["_parent"] = c["id"]
                flat.append(r)

        all_done = True
        for c in flat:
            cid = c["id"]
            if cid in handled:
                continue
            author = c.get("username") or c.get("from", {}).get("username", "")
            key = author.lower()
            if author == OWN_USERNAME:
                done(cid)
                continue
            rule = match(c.get("text"), rules)
            if remember_only or not rule:
                done(cid)
                continue
            if now - parse_ts(c["timestamp"]) > REPLY_WINDOW - timedelta(hours=2):
                skipped_old += 1
                done(cid)
                continue
            if key in already_talking:
                skipped_talking += 1
                done(cid)
                continue
            if key in sent_users:
                skipped_repeat += 1
                done(cid)
                continue
            if DRY_RUN:
                would_send += 1
                log(f"[проверка] ответил бы @{author} на «{c.get('text')}» ({rule['name']})")
                sent_users[key] = today
                continue
            if sent >= MAX_SENDS_PER_RUN:
                all_done = False
                continue
            try:
                api(
                    "POST",
                    f"{PAGE_ID}/messages",
                    body={
                        "recipient": {"comment_id": cid},
                        "message": {"text": rule["message"]},
                    },
                )
                log(f"Отправлено @{author} ({rule['name']})")
                sent += 1
                sent_users[key] = today
                time.sleep(random.uniform(*PAUSE_SECONDS))
                post_public_reply(c, author, rule)
                time.sleep(random.uniform(*PAUSE_SECONDS))
            except RuntimeError as e:
                # Например, ответ на этот комментарий уже отправлен (Meta не даёт
                # отправить второй). Запоминаем и идём дальше.
                log(f"Не отправилось @{author}: {e}")
            done(cid)

        if all_done:
            counts[mid] = total

    state["comment_counts"] = {m["id"]: counts[m["id"]] for m in media if m["id"] in counts}
    state["sent_users"] = sent_users
    state["last_run"] = today
    if DRY_RUN:
        log(f"Проверка: отправилось бы {would_send}.")
    else:
        save_state(state)
    log(
        f"Готово. Отправлено: {sent}. Пропущено: старше 7 дней {skipped_old}, "
        f"уже есть переписка {skipped_talking}, тому же человеку повторно {skipped_repeat}."
    )


if __name__ == "__main__":
    main()
