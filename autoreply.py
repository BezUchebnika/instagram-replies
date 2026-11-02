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
# Повторная попытка для комментариев, на которые директ не ушёл из-за сбоя Meta.
# Пока файл есть, скрипт осторожно (сначала по 5 за запуск) пробует их снова.
RETRY_FILE = ROOT / "retry.json"

MEDIA_TO_SCAN = 50  # сколько последних публикаций проверять
REPLY_WINDOW = timedelta(days=7)  # Meta разрешает личный ответ в течение 7 дней
SAFETY_MARGIN = timedelta(hours=2)  # не пытаемся отвечать на самом краю окна
MAX_SENDS_PER_RUN = 10  # директ за запуск; при запуске раз в 30 минут это ~20 в час
DAILY_CAP = 200  # не больше директов в сутки, сколько бы ни набралось
MAX_PUBLIC_PER_RUN = 3  # публичные ответы за запуск (~6 в час), остальные ждут в очереди
PAUSE_SECONDS = (8, 20)  # случайная пауза между отправками
FAIL_STREAK_STOP = 5  # столько одинаковых ошибок подряд = сбой у Meta, останавливаемся
RETRY_START = 5  # повторных попыток за запуск, пока не убедимся, что Meta принимает
REMEMBER_IDS = 30000  # сколько id обработанных комментариев хранить

TOKEN = os.environ.get("IG_PAGE_TOKEN", "")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
# Что делать, если state.json ещё нет: remember (только запомнить) или backlog
# (ответить за последние 7 дней). Пусто: ничего не делать, ждать ручного запуска.
FIRST_RUN = os.environ.get("FIRST_RUN", "")

# Коды ошибок Meta, при которых надо остановиться и попробовать в следующий раз:
# лимиты запросов, временные блокировки, нет доступа к сообщениям, недействительный ключ.
STOP_CODES = {4, 10, 17, 32, 190, 200, 368, 613, 80002, 80006}


# Ошибки доступа: ключ недействителен или нет прав. Ждать бесполезно, нужен человек.
AUTH_CODES = {10, 190, 200}


class StopSending(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


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
            raise StopSending(
                f"{method} {label} -> лимит, блокировка или нет доступа: {detail}", code
            ) from None
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
    sent_today = state.get("daily", {}).get(today, 0)  # сколько директов уже ушло сегодня (UTC)

    def cap_reached():
        return stats["sent"] >= MAX_SENDS_PER_RUN or sent_today + stats["sent"] >= DAILY_CAP
    stopped = None

    def done(cid):
        if cid not in handled:
            handled.add(cid)
            state["handled"].append(cid)

    retry = None
    if RETRY_FILE.exists() and not first_run:
        retry = json.loads(RETRY_FILE.read_text(encoding="utf-8"))
        retry.setdefault("limit", RETRY_START)
        retry.setdefault("skip", [])
        log(f"Повторная попытка для не получивших директ: до {retry['limit']} за запуск")
    retry_skip = set(retry["skip"]) if retry else set()
    rstats = dict(tried=0, ok=0, left=0)
    streak = []  # комментарии подряд с одинаковой ошибкой

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
            if not first_run and not retry and counts.get(mid) == total and mid not in pending_media:
                continue  # новых комментариев нет

            # Комментарии приходят от новых к старым. Дальше 7 дней не читаем, а если
            # на этой публикации не осталось хвостов, останавливаемся на первом уже
            # обработанном комментарии: всё, что старше, мы уже видели.
            quick = not first_run and not retry and mid not in pending_media
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
                author = c.get("username") or c.get("from", {}).get("username", "")
                key = author.lower()
                is_retry = False
                if cid in handled:
                    # Повтор: комментарий с кодовым словом, автор директ так и не получил.
                    if not (retry and cid not in retry_skip and key != OWN_USERNAME
                            and key not in sent_users and key not in already_talking
                            and parse_ts(c["timestamp"]) >= cutoff and match(c.get("text"), rules)):
                        continue
                    if rstats["tried"] >= retry["limit"] or stopped or cap_reached():
                        rstats["left"] += 1
                        continue
                    is_retry = True
                    rstats["tried"] += 1
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
                if stopped or cap_reached():
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
                    log(f"Директ @{author}" + (" (повтор)" if is_retry else ""))
                    stats["sent"] += 1
                    if is_retry:
                        rstats["ok"] += 1
                    # Ошибки перед успехом были у отдельных людей, а не сбой: закрываем их.
                    for f_cid in streak:
                        done(f_cid)
                        retry_skip.add(f_cid)
                    streak = []
                    sent_users[key] = today
                    if rule["comment_replies"] and not is_retry:
                        entry = {
                            "comment_id": cid,
                            "parent": c.get("_parent"),
                            "author": author,
                            "rule": rule["name"],
                        }
                        # Новым комментариям публичный ответ вперёд очереди,
                        # старым (первый запуск) в конец, они идут медленно.
                        if backlog or is_retry:
                            state["public_queue"].append(entry)
                        else:
                            state["public_queue"].insert(0, entry)
                    done(cid)
                    time.sleep(random.uniform(*PAUSE_SECONDS))
                except StopSending as e:
                    stopped = e
                    leftovers = True
                    log(f"Остановка отправки до следующего запуска: {e}")
                except RuntimeError as e:
                    stats["failed"] += 1
                    log(f"Не отправилось @{author}: {e}")
                    if '"error_subcode":2534025' in str(e):
                        # Комментарий не подходит для личного ответа (удалён, старый,
                        # уже отвечен, закрыты сообщения). Это точно про этого человека.
                        done(cid)
                        retry_skip.add(cid)
                        continue
                    # Остальные ошибки могут быть сбоем у Meta: пока не помечаем.
                    streak.append(cid)
                    if len(streak) >= FAIL_STREAK_STOP:
                        stopped = StopSending(
                            f"{FAIL_STREAK_STOP} ошибок подряд, похоже на сбой у Meta: {e}"
                        )
                        leftovers = True
                        log(f"Остановка отправки до следующего запуска: {stopped}")
                    continue

            counts[mid] = total
            if streak and not stopped:
                leftovers = True  # не помеченные ошибки попробуем в следующий раз
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
        state["daily"] = {today: sent_today + stats["sent"]}
        finish(state, counts, pending_media, sent_users, media, today)
        if retry and not DRY_RUN:
            if rstats["tried"] == 0 and rstats["left"] == 0 and not stopped and media:
                RETRY_FILE.unlink()
                log("Повторная попытка завершена: больше некому отправлять")
            else:
                if rstats["ok"] >= 3 and not stopped:
                    retry["limit"] = MAX_SENDS_PER_RUN  # Meta принимает, идём в обычном темпе
                elif stopped:
                    retry["limit"] = RETRY_START
                retry["skip"] = sorted(retry_skip)
                RETRY_FILE.write_text(json.dumps(retry, ensure_ascii=False) + "\n", encoding="utf-8")

    if DRY_RUN:
        log(f"Проверка: директ ушёл бы {stats['would']} людям.")
    log(
        f"Готово. Директ: {stats['sent']}, публичных ответов: {public}, "
        f"в очереди публичных: {len(state['public_queue'])}. "
        f"Пропущено: старше 7 дней {stats['old']}, уже есть переписка {stats['talking']}, "
        f"повторно тому же человеку {stats['repeat']}, ошибок {stats['failed']}."
    )
    if retry:
        log(f"Повтор: попробовали {rstats['tried']}, ушло {rstats['ok']}, ждут {rstats['left']}.")
    # Есть ли ещё работа: хвост очереди директа или публичных ответов.
    # Workflow по этому флагу сам запускает следующий шаг (пока расписание не работает).
    more = bool(state["pending_media"] or state["public_queue"] or RETRY_FILE.exists())
    if stopped and getattr(stopped, "code", None) in AUTH_CODES:
        more = False  # без доступа крутиться бессмысленно
    out = os.environ.get("GITHUB_OUTPUT")
    if out and not DRY_RUN:
        with open(out, "a") as f:
            f.write(f"more={'true' if more else 'false'}\n")
    if stopped:
        sys.exit(1)  # красный запуск в Actions, чтобы было видно


if __name__ == "__main__":
    main()
