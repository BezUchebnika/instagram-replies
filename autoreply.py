"""Автоответ на кодовые слова в комментариях Instagram (bezuchebnikaeng).

Каждые ~10 минут (через GitHub Actions) скрипт:
1. смотрит последние публикации и находит те, где выросло число комментариев;
2. читает новые комментарии (и ответы в ветках);
3. если в комментарии есть кодовое слово из keywords.yml, отправляет автору
   одно личное сообщение (Private Reply) через Instagram API;
4. ставит публичный ответ под комментарием в очередь и публикует понемногу;
5. запоминает обработанное в state.json, чтобы не писать дважды.

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
SAFETY_MARGIN = timedelta(hours=2)  # не пытаемся отвечать на самом краю окна
MAX_SENDS_PER_RUN = 40  # директ: до ~240 в час при запуске раз в 10 минут (лимит Meta 750)
MAX_PUBLIC_PER_RUN = 5  # публичные ответы: до ~30 в час, остальные ждут в очереди
PAUSE_SECONDS = (3, 5)  # случайная пауза между отправками
REMEMBER_IDS = 30000  # сколько id обработанных комментариев хранить

TOKEN = os.environ.get("IG_PAGE_TOKEN", "")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
# Что делать, если state.json ещё нет: remember (только запомнить) или backlog
# (ответить за последние 7 дней). Пусто: ничего не делать, ждать ручного запуска.
FIRST_RUN = os.environ.get("FIRST_RUN", "")

# Коды ошибок Meta, при которых надо остановиться и попробовать в следующий раз:
# лимиты запросов и временные блокировки действий.
STOP_CODES = {4, 17, 32, 368, 613, 80002, 80006}


class StopSending(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def api(method, path, params=None, body=None):
    params = dict(params or {})
    params["access_token"] = TOKEN
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    return _request(method, url, path, body)


def _request(method, url, label, body=None):
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
        try:
            code = json.loads(detail).get("error", {}).get("code")
        except ValueError:
            code = None
        if code in STOP_CODES or e.code == 429:
            raise StopSending(f"{method} {label} -> лимит или блокировка Meta: {detail}") from None
        raise RuntimeError(f"{method} {label} -> HTTP {e.code}: {detail}") from None


def get_all(path, params, stop=None, max_pages=100):
    """Постранично собирает список; stop(item) -> True прекращает чтение."""
    out = []
    data = api("GET", path, params)
    for _ in range(max_pages):
        for item in data.get("data", []):
            if stop and stop(item):
                return out
            out.append(item)
        nxt = data.get("paging", {}).get("next")
        if not nxt:
            return out
        data = _request("GET", nxt, f"{path} (следующая страница)")
    return out


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
    rules = {}
    for name, entry in (cfg.get("keywords") or {}).items():
        entry = entry or {}
        message = (entry.get("message") or "").strip()
        if not message or entry.get("enabled") is False:
            log(f"Кодовое слово «{name}» пропущено: выключено или нет текста сообщения")
            continue
        words = [name] + list(entry.get("also") or [])
        rules[name] = {
            "name": name,
            "words": [normalize(w) for w in words],
            "message": message,
            "comment_replies": [
                r.strip() for r in (entry.get("comment_replies") or []) if r and r.strip()
            ],
        }
    return rules


def match(text, rules):
    norm = f" {normalize(text)} "
    for rule in rules.values():
        for w in rule["words"]:
            if w and f" {w} " in norm:
                return rule
    return None


def now_utc():
    return datetime.now(timezone.utc)


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
        max_pages=80,
    )
    names = set()
    recent = 0
    for conv in convs:
        if parse_ts(conv["updated_time"]) < since:
            continue
        recent += 1
        for p in conv.get("participants", {}).get("data", []):
            if p.get("username") and p["username"].lower() != OWN_USERNAME:
                names.add(p["username"].lower())
    log(f"Переписок в директе за последние {days} дн.: {recent}, людей: {len(names)}")
    return names


def publish_queue(state, rules):
    """Публикует несколько публичных ответов из очереди. Новые стоят первыми."""
    queue = state["public_queue"]
    posted = 0
    while queue and posted < MAX_PUBLIC_PER_RUN:
        item = queue[0]
        rule = rules.get(item["rule"])
        if not rule or not rule["comment_replies"]:
            queue.pop(0)
            continue
        text = random.choice(rule["comment_replies"])
        target = item["comment_id"]
        if item.get("parent"):
            # На ответ в ветке Instagram отвечать не даёт: отвечаем в ту же ветку
            # с упоминанием человека.
            target = item["parent"]
            text = f"@{item['author']} {text}"
        if DRY_RUN:
            log(f"[проверка] публичный ответ @{item['author']}: {text}")
            queue.pop(0)
            posted += 1
            continue
        try:
            api("POST", f"{target}/replies", {"message": text})
            log(f"Публичный ответ @{item['author']}")
            posted += 1
        except RuntimeError as e:
            log(f"Публичный ответ @{item['author']} не опубликован: {e}")
        queue.pop(0)
        time.sleep(random.uniform(*PAUSE_SECONDS))
    return posted


def finish(state, counts, pending_media, sent_users, media, today):
    media_ids = {m["id"] for m in media}
    if media_ids:
        state["comment_counts"] = {k: v for k, v in counts.items() if k in media_ids}
        state["pending_media"] = sorted(pending_media & media_ids)
    state["sent_users"] = sent_users
    state["last_run"] = today
    if not DRY_RUN:
        save_state(state)


def main():
    if not TOKEN:
        sys.exit("Нет ключа: секрет IG_PAGE_TOKEN не задан")

    rules = load_keywords()
    state = load_state()
    first_run = state is None
    backlog = False
    if first_run:
        if FIRST_RUN == "remember":
            log("Первый запуск: запоминаю существующие комментарии без отправки")
        elif FIRST_RUN == "backlog":
            log("Первый запуск: отвечаю на комментарии за последние 7 дней")
            backlog = True
        else:
            log("Ждём первого ручного запуска (Run workflow): пока ничего не делаю")
            return
        state = {"handled": [], "comment_counts": {}}
    state.setdefault("sent_users", {})
    state.setdefault("public_queue", [])
    state.setdefault("pending_media", [])

    # Список «уже есть переписка» берём при первом запуске и помним 8 дней,
    # чтобы хвост очереди в следующих запусках тоже их пропускал.
    if backlog:
        until = (now_utc() + timedelta(days=8)).strftime("%Y-%m-%d")
        state["skip_users"] = {u: until for u in recent_conversation_usernames()}
    today_str = now_utc().strftime("%Y-%m-%d")
    state["skip_users"] = {u: d for u, d in state.get("skip_users", {}).items() if d >= today_str}
    already_talking = set(state["skip_users"])
    remember_only = first_run and not backlog

    handled = set(state["handled"])
    counts = state["comment_counts"]
    pending_media = set(state["pending_media"])
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    cutoff = now - REPLY_WINDOW + SAFETY_MARGIN
    week_ago = (now - REPLY_WINDOW).strftime("%Y-%m-%d")
    # Кому уже отправляли за последние 7 дней: второй раз тому же человеку не шлём.
    sent_users = {u: d for u, d in state["sent_users"].items() if d >= week_ago}
    stats = dict(sent=0, would=0, old=0, talking=0, repeat=0, failed=0)
    stopped = None

    def done(cid):
        handled.add(cid)
        state["handled"].append(cid)

    media = []
    public = 0
    try:
        media = api(
            "GET",
            f"{IG_USER_ID}/media",
            {"fields": "id,comments_count,timestamp", "limit": MEDIA_TO_SCAN},
        ).get("data", [])

        for m in media:
            mid = m["id"]
            total = m.get("comments_count", 0)
            if not first_run and counts.get(mid) == total and mid not in pending_media:
                continue  # новых комментариев нет

            # Комментарии приходят от новых к старым. Дальше 7 дней не читаем, а если
            # на этой публикации не осталось хвостов, останавливаемся на первом уже
            # обработанном комментарии: всё, что старше, мы уже видели.
            quick = not first_run and mid not in pending_media
            comments = get_all(
                f"{mid}/comments",
                {
                    "fields": "id,text,timestamp,username,from,"
                    "replies{id,text,timestamp,username,from}",
                    "limit": 50,
                },
                stop=lambda c: parse_ts(c["timestamp"]) < cutoff or (quick and c["id"] in handled),
            )
            flat = []
            for c in comments:
                flat.append(c)
                for r in c.get("replies", {}).get("data", []):
                    r["_parent"] = c["id"]
                    flat.append(r)

            leftovers = False
            for c in flat:
                cid = c["id"]
                if cid in handled:
                    continue
                author = c.get("username") or c.get("from", {}).get("username", "")
                key = author.lower()
                if key == OWN_USERNAME:
                    done(cid)
                    continue
                rule = match(c.get("text"), rules)
                if remember_only or not rule:
                    done(cid)
                    continue
                if parse_ts(c["timestamp"]) < cutoff:
                    stats["old"] += 1
                    done(cid)
                    continue
                if key in already_talking:
                    stats["talking"] += 1
                    done(cid)
                    continue
                if key in sent_users:
                    stats["repeat"] += 1
                    done(cid)
                    continue
                if DRY_RUN:
                    stats["would"] += 1
                    log(f"[проверка] директ @{author} на «{c.get('text')}»")
                    sent_users[key] = today
                    continue
                if stopped or stats["sent"] >= MAX_SENDS_PER_RUN:
                    leftovers = True
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
                    log(f"Директ @{author}")
                    stats["sent"] += 1
                    sent_users[key] = today
                    if rule["comment_replies"]:
                        entry = {
                            "comment_id": cid,
                            "parent": c.get("_parent"),
                            "author": author,
                            "rule": rule["name"],
                        }
                        # Новым комментариям публичный ответ вперёд очереди,
                        # старым (первый запуск) в конец, они идут медленно.
                        if backlog:
                            state["public_queue"].append(entry)
                        else:
                            state["public_queue"].insert(0, entry)
                    done(cid)
                    time.sleep(random.uniform(*PAUSE_SECONDS))
                except StopSending as e:
                    stopped = str(e)
                    leftovers = True
                    log(f"Остановка отправки до следующего запуска: {e}")
                except RuntimeError as e:
                    # Например, ответ на этот комментарий уже отправлен или человек
                    # закрыл сообщения. Запоминаем и идём дальше.
                    stats["failed"] += 1
                    log(f"Не отправилось @{author}: {e}")
                    done(cid)

            counts[mid] = total
            if leftovers:
                pending_media.add(mid)
            else:
                pending_media.discard(mid)

        public = 0
        if not stopped:
            try:
                public = publish_queue(state, rules)
            except StopSending as e:
                log(f"Остановка публичных ответов до следующего запуска: {e}")
    finally:
        # Сохраняем, даже если запуск упал на середине: кому ушло, тому не повторяем.
        finish(state, counts, pending_media, sent_users, media, today)

    if DRY_RUN:
        log(f"Проверка: директ ушёл бы {stats['would']} людям.")
    log(
        f"Готово. Директ: {stats['sent']}, публичных ответов: {public}, "
        f"в очереди публичных: {len(state['public_queue'])}. "
        f"Пропущено: старше 7 дней {stats['old']}, уже есть переписка {stats['talking']}, "
        f"повторно тому же человеку {stats['repeat']}, ошибок {stats['failed']}."
    )
    if stopped:
        sys.exit(1)  # красный запуск в Actions, чтобы было видно


if __name__ == "__main__":
    main()
