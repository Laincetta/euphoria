#!/usr/bin/env python3
"""
Сервер заявок салона «Эйфория»: страница записи + приём заявок + бот в Telegram + отчёт.
Только стандартный Python 3.8+, ничего устанавливать не нужно. Без Cloudflare и облаков.

Запуск:  python3 server.py      (Windows: двойной щелчок по start.bat)
Первый запуск создаёт config.json — впишите туда токен бота и пароль, запустите снова.

Что внутри:
  http://АДРЕС:8080/                     — страница салона с записью
  POST /api/lead                          — сюда страница отправляет заявки
  http://АДРЕС:8080/admin?key=ПАРОЛЬ      — список заявок, статусы, сводка
  http://АДРЕС:8080/report.csv?key=ПАРОЛЬ — все заявки таблицей (Excel, Яндекс Таблицы)
Бот: заявки приходят администратору с кнопками статуса; команды /report и /admin ПАРОЛЬ.
Бот сам опрашивает Telegram, поэтому для него не нужен внешний адрес.

Обновления (с разрешения владельца): сервер берёт новую версию из репозитория GitHub (UPDATE_REPO в config.json) —
сам раз в несколько минут (UPDATE_AUTO: true) или по команде /update в боте. Скачиваются только server.py и
страница, проверяются контрольные суммы, затем сервер перезапускается. config.json и leads.db не трогаются,
прошлая версия сохраняется в папке backup. Выключить: UPDATE_REPO = "".
"""
import csv
import hashlib
import html
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
DB_PATH = HERE / "leads.db"
PAGE_PATH = HERE / "public" / "index.html"
VERSION_PATH = HERE / "VERSION"
UPDATE_FILES = ["server.py", "public/index.html"]  # только эти файлы может заменить обновление
RESTART_CODE = 3  # start.bat / start.command перезапускают сервер с этим кодом выхода
MSK = timezone(timedelta(hours=3))

DEFAULT_CONFIG = {
    "BOT_TOKEN": "",
    "REPORT_KEY": "",
    "CHAT_ID": "",
    "HOST": "0.0.0.0",
    "PORT": 8080,
    "SALON_NAME": "Эйфория",
    "SALON_ADDRESS": "Реутов, Парковая улица, 8",
    "SALON_PHONE": "",
    "SITE_URL": "",  # адрес страницы записи, когда сервер будет в интернете
    "REVIEW_URL": "",  # ссылка на отзывы в Яндекс Картах — бот попросит отзыв после визита
    "TELEGRAM_API": "https://api.telegram.org",
    "UPDATE_REPO": "Laincetta/euphoria",  # пусто — обновления выключены
    "UPDATE_BRANCH": "main",
    "UPDATE_AUTO": True,  # false — только по команде /update
    "UPDATE_EVERY_MIN": 5,
    "UPDATE_TOKEN": "",  # нужен только если репозиторий приватный (токен GitHub с правом чтения)
}
STATUS = {"new": "Новая", "confirmed": "Подтверждена", "noanswer": "Не дозвонились", "came": "Пришёл", "cancelled": "Отменена"}
WIN = {"morning": "утро, 10:00–13:00", "day": "день, 13:00–17:00", "evening": "вечер, 17:00–21:00", "any": "любое время"}
MARK = {"new": "🆕", "confirmed": "✅", "noanswer": "📵", "came": "💚", "cancelled": "❌"}
CONTACT = {"call": "позвонить", "telegram": "написать в Telegram", "max": "написать в MAX"}
DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]

cfg = {}
db_lock = threading.Lock()
update_lock = threading.Lock()
tg_offset = 0
BOT = {"username": ""}
rate = {}


def log(*a):
    print(datetime.now(MSK).strftime("%d.%m %H:%M:%S"), *a, flush=True)


def load_config():
    global cfg
    if not CONFIG_PATH.exists():
        c = dict(DEFAULT_CONFIG, REPORT_KEY="euph_" + secrets.token_urlsafe(9).replace("-", "x"))
        CONFIG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nСоздан {CONFIG_PATH.name}. Впишите в него BOT_TOKEN (токен от @BotFather) и запустите сервер снова.\n"
              f"Пароль для отчёта уже придуман: {c['REPORT_KEY']} (можно поменять).\n")
        sys.exit(0)
    cfg = dict(DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", cfg["REPORT_KEY"] or ""):
        sys.exit("REPORT_KEY в config.json: минимум 6 символов, только латиница, цифры, _ и -")


def save_config():
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_lock, db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS leads(
            id TEXT PRIMARY KEY, created_at TEXT, name TEXT, phone TEXT, services TEXT, total INTEGER, total_from INTEGER,
            day TEXT, win TEXT, contact TEXT, comment TEXT, status TEXT DEFAULT 'new', status_at TEXT, status_by TEXT, message_id INTEGER)""")
        have = [r[1] for r in con.execute("PRAGMA table_info(leads)")]
        for col, typ in [("handle", "TEXT"), ("tok", "TEXT"), ("client_chat", "TEXT"), ("appt_at", "TEXT"),
                         ("rem24", "INTEGER DEFAULT 0"), ("rem2", "INTEGER DEFAULT 0")]:  # обновление старой базы
            if col not in have:
                con.execute(f"ALTER TABLE leads ADD COLUMN {col} {typ}")
        # какое сообщение в чате админа к какой заявке относится: вопрос о времени, пересланное сообщение клиента
        con.execute("CREATE TABLE IF NOT EXISTS msgmap(admin_msg INTEGER PRIMARY KEY, lead_id TEXT, kind TEXT)")


def norm_handle(contact, s):
    """Ник клиента в мессенджере. Telegram: @username (принимаем и ссылку t.me/...)."""
    s = str(s or "").strip()[:80]
    if contact == "telegram":
        m = re.fullmatch(r"(?:https?://)?(?:t\.me/|telegram\.me/)?@?([A-Za-z0-9_]{4,32})/?", s)
        return "@" + m.group(1) if m else s
    return s if contact == "max" else ""


def handle_html(l):
    h = l["handle"] or ""
    if not h:
        return " · ник не указан, искать по номеру телефона" if l["contact"] == "max" else ""
    if l["contact"] == "telegram" and re.fullmatch(r"@[A-Za-z0-9_]{4,32}", h):
        return f' · <a href="https://t.me/{h[1:]}">{esc(h)}</a>'
    if re.match(r"https?://\S+$", h) or re.match(r"(web\.)?max\.ru/\S+$", h):
        url = h if h.startswith("http") else "https://" + h
        return f' · <a href="{html.escape(url)}">{esc(h)}</a>'
    return " · " + esc(h)


# ---------- Telegram ----------
def tg(method, payload, timeout=15):
    if not cfg.get("BOT_TOKEN"):
        return {"ok": False, "description": "BOT_TOKEN не задан"}
    req = urllib.request.Request(f"{cfg['TELEGRAM_API']}/bot{cfg['BOT_TOKEN']}/{method}",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": str(e)}
    except Exception as e:
        return {"ok": False, "description": str(e)}


esc = lambda s: html.escape(str(s or ""), quote=False)
rub = lambda n: f"{int(n or 0):,}".replace(",", " ")


def fmt_time(iso):
    return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m, %H:%M")


def fmt_day(d):
    try:
        x = datetime.strptime(d, "%Y-%m-%d")
        return f"{DAYS[x.weekday()]}, {x.day} {MONTHS[x.month - 1]}"
    except ValueError:
        return d


def fmt_appt(iso):
    x = datetime.fromisoformat(iso).astimezone(MSK)
    return f"{DAYS[x.weekday()]}, {x.day} {MONTHS[x.month - 1]}, {x:%H:%M}"


def parse_appt(text, day):
    """«21.10 14:30», «14:30», «21.10 в 14.30» → datetime МСК. day — желаемый день из заявки (если дата не указана)."""
    s = text.lower().replace(" в ", " ")
    tm = re.search(r"(\d{1,2})[:.](\d{2})\s*$", s.strip())
    if not tm:
        return None
    rest = s.strip()[:tm.start()].strip()
    now = datetime.now(MSK)
    try:
        if rest:
            dm = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", rest)
            if not dm:
                return None
            y = int(dm.group(3)) if dm.group(3) else now.year
            y = y + 2000 if y < 100 else y
            d = datetime(y, int(dm.group(2)), int(dm.group(1)), tzinfo=MSK)
            if not dm.group(3) and d.date() < (now - timedelta(days=1)).date():
                d = d.replace(year=y + 1)
        else:
            d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=MSK)
        return d.replace(hour=int(tm.group(1)), minute=int(tm.group(2)))
    except ValueError:
        return None


def lead_text(l):
    services = json.loads(l["services"])
    head = "Новая заявка" if l["status"] == "new" else f"Заявка · {STATUS[l['status']]}"
    out = [f"{MARK[l['status']]} <b>{head}</b> · {fmt_time(l['created_at'])}", "", "<b>Услуги:</b>"]
    out += [f"• {esc(s['name'])} — {esc(s['price'])}" for s in services]
    out += [f"Итого: {'от ' if l['total_from'] else ''}{rub(l['total'])} ₽", "",
            f"<b>Когда удобно:</b> {esc(fmt_day(l['day']))}, {esc(WIN.get(l['win'], l['win']))}",
            f"<b>Имя:</b> {esc(l['name'])}", f"<b>Телефон:</b> {esc(l['phone'])}",
            f"<b>Связь:</b> {esc(CONTACT.get(l['contact'], l['contact']))}{handle_html(l)}"]
    if l["comment"]:
        out.append(f"<b>Комментарий:</b> {esc(l['comment'])}")
    if l["contact"] == "telegram":
        out.append("<b>Уведомления:</b> " + ("🔔 клиент подключил бота — подтверждение и напоминания уйдут сами" if l["client_chat"]
                                              else "⏳ клиент ещё не открыл бота — пока пишите ему сами"))
    if l["appt_at"]:
        out.append(f"<b>Записан(а):</b> {fmt_appt(l['appt_at'])}")
    if l["status"] and l["status"] != "new":
        out += ["", f"<b>Статус: {STATUS[l['status']]}</b> · {esc(l['status_by'] or '')} {fmt_time(l['status_at'])}"]
    return "\n".join(out)


def keyboard(i, closed=False):
    b = lambda t, s: {"text": t, "callback_data": f"st:{i}:{s}"}
    if closed:  # статус выбран — кнопки свёрнуты, остаётся одна, чтобы исправить ошибку
        return {"inline_keyboard": [[b("Изменить статус", "edit")]]}
    return {"inline_keyboard": [[b("Подтвердил запись", "confirmed"), b("Не дозвонился", "noanswer")],
                                [b("Клиент пришёл", "came"), b("Отмена", "cancelled")]]}


def refresh_admin_msg(l):
    if l and l["message_id"] and cfg.get("CHAT_ID"):
        tg("editMessageText", {"chat_id": cfg["CHAT_ID"], "message_id": l["message_id"], "text": lead_text(l), "parse_mode": "HTML",
                               "reply_markup": keyboard(l["id"], closed=l["status"] != "new")})


def map_msg(admin_msg, lead_id, kind):
    with db_lock, db() as con:
        con.execute("INSERT OR REPLACE INTO msgmap VALUES(?,?,?)", (admin_msg, lead_id, kind))


def ask_time(l):
    r = tg("sendMessage", {"chat_id": cfg["CHAT_ID"], "text": f"Во сколько записан(а) {l['name']}? Ответьте на это сообщение, например: "
                           f"«{datetime.strptime(l['day'], '%Y-%m-%d'):%d.%m} 14:30» или просто «14:30».",
                           "reply_markup": {"force_reply": True, "input_field_placeholder": "21.10 14:30"}})
    if r.get("ok"):
        map_msg(r["result"]["message_id"], l["id"], "ask")


def services_line(l):
    return ", ".join(s["name"] for s in json.loads(l["services"]))


def to_client(l, text):
    if l and l["client_chat"]:
        r = tg("sendMessage", {"chat_id": l["client_chat"], "text": text})
        if not r.get("ok"):
            log("Клиенту", l["name"], "не доставлено:", r.get("description"))
        return r.get("ok")
    return False


def set_appt(l, when, admin_chat):
    left = when - datetime.now(MSK)
    with db_lock, db() as con:  # напоминание, время которого уже прошло, не шлём
        con.execute("UPDATE leads SET appt_at=?, rem24=?, rem2=?, status=CASE WHEN status IN ('new','noanswer') THEN 'confirmed' ELSE status END, "
                    "status_at=COALESCE(status_at, ?) WHERE id=?",
                    (when.isoformat(), int(left < timedelta(hours=25)), int(left < timedelta(hours=2, minutes=30)),
                     datetime.now(timezone.utc).isoformat(), l["id"]))
    l = get_lead(l["id"])
    refresh_admin_msg(l)
    sent = to_client(l, f"✅ Вы записаны в салон «{cfg['SALON_NAME']}»\n{fmt_appt(l['appt_at'])}\n{services_line(l)}\n\n"
                        f"Адрес: {cfg['SALON_ADDRESS']}\nЕсли планы изменятся — просто напишите сюда.")
    tg("sendMessage", {"chat_id": admin_chat, "text": f"Готово: {l['name']} — {fmt_appt(l['appt_at'])}. " + (
        "Клиенту ушло подтверждение, напомним за сутки и за 2 часа." if sent else
        "Клиент не подключил бота — подтвердите и напомните ему сами.")})
    log("Время записи", l["id"], "→", fmt_appt(l["appt_at"]))


def client_start(chat, tok, first_name):
    with db_lock, db() as con:
        l = con.execute("SELECT * FROM leads WHERE tok=?", (tok,)).fetchone()
        if l and not l["client_chat"]:
            con.execute("UPDATE leads SET client_chat=? WHERE id=?", (str(chat), l["id"]))
    if not l or (l["client_chat"] and l["client_chat"] != str(chat)):
        return client_hello(chat)
    l = get_lead(l["id"])
    w = WIN.get(l["win"], "")
    tg("sendMessage", {"chat_id": chat, "text": f"Здравствуйте, {l['name']}! Заявка в салон «{cfg['SALON_NAME']}» получена:\n"
                       f"{services_line(l)}\nКогда удобно: {fmt_day(l['day'])}, {w}\n\n"
                       "Администратор свяжется с вами и подтвердит точное время. Подтверждение и напоминание придут сюда."})
    refresh_admin_msg(l)
    log("Клиент подключил уведомления:", l["id"], l["name"])


def client_hello(chat):
    tg("sendMessage", {"chat_id": chat, "text": f"Здравствуйте! Это бот салона «{cfg['SALON_NAME']}», {cfg['SALON_ADDRESS']}. "
                       "Сюда приходят подтверждения записи и напоминания." +
                       (f"\nЗаписаться: {cfg['SITE_URL']}" if cfg.get("SITE_URL") else "")})


def client_message(chat, text, m):
    with db_lock, db() as con:
        l = con.execute("SELECT * FROM leads WHERE client_chat=? ORDER BY created_at DESC LIMIT 1", (str(chat),)).fetchone()
    if not l or not cfg.get("CHAT_ID"):
        return client_hello(chat)
    r = tg("sendMessage", {"chat_id": cfg["CHAT_ID"], "text": f"💬 {l['name']} ({l['phone']}) пишет:\n{text}\n\n"
                           "Ответьте на это сообщение — ответ уйдёт клиенту."})
    if r.get("ok"):
        map_msg(r["result"]["message_id"], l["id"], "fwd")
        tg("sendMessage", {"chat_id": chat, "text": "Передали администратору ✓ Ответ придёт сюда."})


def admin_reply(chat, text, reply_to):
    with db_lock, db() as con:
        mm = con.execute("SELECT * FROM msgmap WHERE admin_msg=?", (reply_to,)).fetchone()
        if not mm:  # ответ прямо на сообщение с заявкой — тоже считаем временем записи
            row = con.execute("SELECT id FROM leads WHERE message_id=? ORDER BY created_at DESC LIMIT 1", (reply_to,)).fetchone()
            mm = {"lead_id": row["id"], "kind": "ask"} if row else None
    if not mm:
        return False
    l = get_lead(mm["lead_id"])
    if mm["kind"] == "fwd":
        ok = to_client(l, f"Администратор салона «{cfg['SALON_NAME']}»:\n{text}")
        tg("sendMessage", {"chat_id": chat, "text": "Отправлено клиенту ✓" if ok else "Не получилось отправить клиенту — позвоните ему."})
        return True
    when = parse_appt(text, l["day"])
    if not when:
        r = tg("sendMessage", {"chat_id": chat, "text": "Не понял время. Ответьте на это сообщение, например: 21.10 14:30",
                               "reply_markup": {"force_reply": True, "input_field_placeholder": "21.10 14:30"}})
        if r.get("ok"):
            map_msg(r["result"]["message_id"], l["id"], "ask")
        return True
    set_appt(l, when, chat)
    return True


def reminder_loop():
    while True:
        time.sleep(60)
        try:
            now = datetime.now(MSK)
            with db_lock, db() as con:
                rows = con.execute("SELECT * FROM leads WHERE appt_at IS NOT NULL AND client_chat IS NOT NULL AND status='confirmed' "
                                   "AND (rem24=0 OR rem2=0)").fetchall()
            for l in rows:
                when = datetime.fromisoformat(l["appt_at"])
                if when < now:
                    continue
                for col, before, text in [("rem24", timedelta(hours=24), "Напоминаем: завтра ждём вас в салоне «{n}»\n{t}"),
                                          ("rem2", timedelta(hours=2), "Через 2 часа ждём вас в салоне «{n}»\n{t}")]:
                    if not l[col] and now >= when - before:
                        t = fmt_appt(l["appt_at"]) + "\n" + services_line(l) + "\nАдрес: " + cfg["SALON_ADDRESS"]
                        ok = to_client(l, text.format(n=cfg["SALON_NAME"], t=t) + "\nЕсли не получается прийти — напишите сюда.")
                        with db_lock, db() as con:
                            con.execute(f"UPDATE leads SET {col}=1 WHERE id=?", (l["id"],))
                        log("Напоминание", col, l["id"], "отправлено" if ok else "не доставлено")
                        break
        except Exception as e:
            log("Ошибка напоминаний:", e)


def set_commands():
    tg("setMyCommands", {"commands": [{"command": "start", "description": "О салоне"}]})
    if cfg.get("CHAT_ID"):
        tg("setMyCommands", {"scope": {"type": "chat", "chat_id": int(cfg["CHAT_ID"])},
                             "commands": [{"command": "report", "description": "Сводка по заявкам"},
                                          {"command": "update", "description": "Обновить сервер"}]})


def get_lead(i):
    with db_lock, db() as con:
        return con.execute("SELECT * FROM leads WHERE id=?", (i,)).fetchone()


def all_leads(days=0):
    with db_lock, db() as con:
        rows = con.execute("SELECT * FROM leads ORDER BY created_at DESC").fetchall()
    if days:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = [r for r in rows if datetime.fromisoformat(r["created_at"]) >= since]
    return rows


def summary(days):
    ls = all_leads(days)
    cnt = lambda s: sum(1 for l in ls if l["status"] == s)
    top = {}
    for l in ls:
        for s in json.loads(l["services"]):
            top[s["name"]] = top.get(s["name"], 0) + 1
    best = ", ".join(f"{n} ({c})" for n, c in sorted(top.items(), key=lambda x: -x[1])[:3]) or "—"
    return (f"За {days} дней: заявок {len(ls)}, подтверждено {cnt('confirmed') + cnt('came')}, пришли {cnt('came')}, "
            f"не дозвонились {cnt('noanswer')}, отмен {cnt('cancelled')}, без статуса {cnt('new')}.\nЧаще выбирали: {best}")


def handle_update(u):
    q = u.get("callback_query")
    if q:
        parts = str(q.get("data", "")).split(":")
        if len(parts) != 3 or (parts[2] not in STATUS and parts[2] != "edit") or str(q["message"]["chat"]["id"]) != str(cfg["CHAT_ID"]):
            tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Нет доступа"})
            return
        _, i, st = parts
        if st == "edit":  # развернуть кнопки обратно
            tg("editMessageReplyMarkup", {"chat_id": cfg["CHAT_ID"], "message_id": q["message"]["message_id"],
                                          "reply_markup": keyboard(i)})
            tg("answerCallbackQuery", {"callback_query_id": q["id"]})
            return
        prev = get_lead(i)
        with db_lock, db() as con:
            con.execute("UPDATE leads SET status=?, status_at=?, status_by=? WHERE id=?",
                        (st, datetime.now(timezone.utc).isoformat(), q["from"].get("first_name", ""), i))
        l = get_lead(i)
        if l:
            tg("editMessageText", {"chat_id": cfg["CHAT_ID"], "message_id": q["message"]["message_id"], "text": lead_text(l),
                                   "parse_mode": "HTML", "reply_markup": keyboard(i, closed=True)})
        tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Статус: " + STATUS[st]})
        log("Статус заявки", i, "→", STATUS[st])
        if l and st == "confirmed" and not l["appt_at"]:
            ask_time(l)
        if l and st == "cancelled" and prev and prev["status"] == "confirmed" and l["appt_at"]:
            to_client(l, f"Запись в салон «{cfg['SALON_NAME']}» на {fmt_appt(l['appt_at'])} отменена. "
                         "Если захотите выбрать другое время — напишите сюда.")
        if l and st == "came" and prev and prev["status"] != "came" and cfg.get("REVIEW_URL"):
            to_client(l, f"Спасибо, что были у нас! Будем благодарны за отзыв — это пара минут: {cfg['REVIEW_URL']}")
        return
    m = u.get("message") or {}
    text, chat = m.get("text", ""), (m.get("chat") or {}).get("id")
    if not text or chat is None:
        return
    is_admin = str(chat) == str(cfg["CHAT_ID"])
    if is_admin and (m.get("reply_to_message") or {}).get("message_id") and not text.startswith("/"):
        if admin_reply(chat, text, m["reply_to_message"]["message_id"]):
            return
    if text.startswith("/start ") and len(text.split()) == 2:  # клиент пришёл по кнопке со страницы (админ тоже может проверить на себе)
        return client_start(chat, text.split(maxsplit=1)[1].strip(), (m.get("from") or {}).get("first_name", ""))
    if text.startswith("/admin"):
        if text.split(maxsplit=1)[-1].strip() == cfg["REPORT_KEY"]:
            cfg["CHAT_ID"] = str(chat)
            save_config()
            tg("sendMessage", {"chat_id": chat, "text": "Готово: заявки с сайта теперь будут приходить в этот чат.\n/report — сводка по заявкам."})
            log("Администратор подключён, chat_id", chat)
            set_commands()
            threading.Thread(target=resend_pending, daemon=True).start()
        else:
            tg("sendMessage", {"chat_id": chat, "text": "Неверный пароль. Отправьте: /admin ПАРОЛЬ (REPORT_KEY из config.json)."})
    elif text.startswith("/start"):
        if is_admin:
            tg("sendMessage", {"chat_id": chat, "text": "Вы администратор: сюда приходят заявки. /report — сводка, /update — обновить сервер.\n"
                               "После «Подтвердил запись» бот спросит время — ответьте на его сообщение, и клиент получит подтверждение."})
        else:
            client_hello(chat)
    elif text.startswith("/update") and is_admin:
        threading.Thread(target=check_update, args=(True,), daemon=True).start()
    elif text.startswith("/report") and is_admin:
        tg("sendMessage", {"chat_id": chat, "text": summary(7) + "\n\n" + summary(30) + "\n\nПолный список: страница /admin на сервере."})
    elif not is_admin and not text.startswith("/"):
        client_message(chat, text[:1000], m)


def poll_loop():
    global tg_offset
    tg("deleteWebhook", {})
    set_commands()
    me = tg("getMe", {})
    if me.get("ok"):
        BOT["username"] = me["result"]["username"]
    log("Бот:", "@" + me["result"]["username"] if me.get("ok") else "не подключён — " + str(me.get("description")))
    while True:
        r = tg("getUpdates", {"offset": tg_offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]}, timeout=35)
        if not r.get("ok"):
            d = str(r.get("description"))
            if "Conflict" in d:
                log("Бот уже запущен в другом месте (другое окно сервера или другой компьютер). Оставьте один сервер.")
                time.sleep(30)
            else:
                time.sleep(5)
            continue
        for u in r["result"]:
            tg_offset = u["update_id"] + 1
            try:
                handle_update(u)
            except Exception as e:
                log("Ошибка обработки:", e)


# ---------- обновления ----------
def gh_get(path):
    url = f"{cfg.get('UPDATE_API') or 'https://api.github.com'}/repos/{cfg['UPDATE_REPO']}/contents/{path}?ref={urllib.parse.quote(cfg['UPDATE_BRANCH'])}"
    headers = {"Accept": "application/vnd.github.raw", "User-Agent": "euphoria-updater", "Cache-Control": "no-cache"}
    if cfg.get("UPDATE_TOKEN"):
        headers["Authorization"] = "Bearer " + cfg["UPDATE_TOKEN"]
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return r.read()


def local_version():
    return VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else ""


def admin_say(text):
    if cfg.get("CHAT_ID") and cfg.get("BOT_TOKEN"):
        tg("sendMessage", {"chat_id": cfg["CHAT_ID"], "text": text})


def restart():
    log("Перезапуск после обновления…")
    if cfg.get("BOT_TOKEN"):  # подтвердить Telegram обработанные сообщения, чтобы после перезапуска они не пришли снова
        tg("getUpdates", {"offset": tg_offset, "timeout": 0}, timeout=10)
    os._exit(RESTART_CODE)


def check_update(manual=False):
    if not cfg.get("UPDATE_REPO"):
        return manual and admin_say("Обновления выключены (UPDATE_REPO пуст в config.json).")
    with update_lock:
        try:
            man = json.loads(gh_get("server/manifest.json"))
            if man.get("version") == local_version():
                return manual and admin_say(f"Обновлений нет. Версия от {man.get('built', '')}.")
            new = {}
            for f, sha in (man.get("files") or {}).items():
                if f not in UPDATE_FILES:
                    continue
                data = gh_get("server/" + f)
                if hashlib.sha256(data).hexdigest() != sha:
                    raise ValueError("не совпала контрольная сумма " + f)
                if f.endswith(".py"):
                    compile(data, f, "exec")  # битый файл не ставим
                new[f] = data
            changed = [f for f, d in new.items() if not (HERE / f).exists() or (HERE / f).read_bytes() != d]
            for f in changed:
                dst = HERE / f
                if dst.exists():
                    bak = HERE / "backup" / f
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    bak.write_bytes(dst.read_bytes())
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".new")
                tmp.write_bytes(new[f])
                os.replace(tmp, dst)
            VERSION_PATH.write_text(man["version"], encoding="utf-8")
            log("Обновлено:", man.get("built", ""), "· файлы:", ", ".join(changed) or "без изменений")
            if changed or manual:
                admin_say(f"🔄 Сервер обновлён · версия от {man.get('built', '')}\n{man.get('note') or ''}".strip())
            if "server.py" in changed:
                restart()
        except Exception as e:
            log("Обновление: не получилось —", e)
            if manual:
                admin_say(f"Не получилось обновиться: {e}")


def update_loop():
    time.sleep(20)
    while True:
        check_update()
        time.sleep(max(1, int(cfg.get("UPDATE_EVERY_MIN") or 5)) * 60)


# ---------- заявки ----------
def clean_lead(b):
    cut = lambda s, n: str(s or "")[:n].strip()
    services = [{"id": cut(s.get("id"), 40), "name": cut(s.get("name"), 160), "price": cut(s.get("price"), 40)}
                for s in (b.get("services") or [])[:20] if isinstance(s, dict) and s.get("name")]
    lead = {"name": cut(b.get("name"), 80), "phone": cut(b.get("phone"), 40), "services": services,
            "total": int(b.get("total") or 0), "total_from": 1 if b.get("totalFrom") else 0,
            "day": b.get("day") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(b.get("day"))) else datetime.now(MSK).strftime("%Y-%m-%d"),
            "win": b.get("window") if b.get("window") in WIN else "any",
            "contact": b.get("contact") if b.get("contact") in CONTACT else "call", "comment": cut(b.get("comment"), 500)}
    lead["handle"] = norm_handle(lead["contact"], b.get("handle"))
    ok = lead["name"] and len(re.sub(r"\D", "", lead["phone"])) >= 10 and services
    return lead if ok else None


def save_lead(lead):
    i = datetime.now().strftime("%y%m%d") + secrets.token_hex(3)
    row = dict(lead, id=i, tok=secrets.token_urlsafe(12), created_at=datetime.now(timezone.utc).isoformat(),
               services=json.dumps(lead["services"], ensure_ascii=False))
    with db_lock, db() as con:
        con.execute("INSERT INTO leads(id,tok,created_at,name,phone,services,total,total_from,day,win,contact,handle,comment) "
                    "VALUES(:id,:tok,:created_at,:name,:phone,:services,:total,:total_from,:day,:win,:contact,:handle,:comment)", row)
    ok = send_lead(get_lead(i))
    return i, ok, row["tok"]


def send_lead(l):
    """Отправить заявку администратору. Не получилось — она остаётся в базе и уйдёт позже (resend_loop)."""
    sent = {"ok": False, "description": "CHAT_ID не задан: администратор должен отправить боту /admin ПАРОЛЬ"}
    if cfg["CHAT_ID"]:
        sent = tg("sendMessage", {"chat_id": cfg["CHAT_ID"], "text": lead_text(l), "parse_mode": "HTML", "reply_markup": keyboard(l["id"])})
    if sent.get("ok"):
        with db_lock, db() as con:
            con.execute("UPDATE leads SET message_id=? WHERE id=?", (sent["result"]["message_id"], l["id"]))
    log("Заявка", l["id"], l["name"], "· Telegram:", "отправлено" if sent.get("ok") else "НЕ отправлено — " + str(sent.get("description")) + " (повторю позже)")
    return bool(sent.get("ok"))


resend_lock = threading.Lock()


def resend_pending():
    if not (cfg.get("CHAT_ID") and cfg.get("BOT_TOKEN")) or not resend_lock.acquire(blocking=False):
        return
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        with db_lock, db() as con:
            rows = con.execute("SELECT * FROM leads WHERE message_id IS NULL AND created_at >= ? ORDER BY created_at", (since,)).fetchall()
        for l in rows:
            if not send_lead(l):
                break
    finally:
        resend_lock.release()


def resend_loop():
    while True:
        time.sleep(60)
        resend_pending()


# ---------- веб ----------
def csv_bytes(rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата заявки", "Имя", "Телефон", "Услуги", "Сумма, ₽", "Желаемый день", "Время", "Записан(а)", "Связь", "Ник в мессенджере", "Бот подключён", "Комментарий", "Статус", "Кто поставил статус"])
    for l in rows:
        w.writerow([fmt_time(l["created_at"]), l["name"], l["phone"], "; ".join(s["name"] for s in json.loads(l["services"])),
                    ("от " if l["total_from"] else "") + str(l["total"]), l["day"], WIN.get(l["win"], l["win"]),
                    fmt_appt(l["appt_at"]) if l["appt_at"] else "", CONTACT.get(l["contact"], l["contact"]), l["handle"] or "",
                    "да" if l["client_chat"] else "", l["comment"], STATUS.get(l["status"], l["status"]), l["status_by"] or ""])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def admin_html(days):
    rows = all_leads(days)
    tr = "".join(
        f"<tr><td>{fmt_time(l['created_at'])}</td><td>{esc(l['name'])}</td><td><a href='tel:{esc(re.sub(r'[^0-9+]', '', l['phone']))}'>{esc(l['phone'])}</a></td>"
        f"<td>{'<br>'.join(esc(s['name']) for s in json.loads(l['services']))}</td><td class=r>{'от ' if l['total_from'] else ''}{rub(l['total'])}</td>"
        f"<td>{esc(fmt_appt(l['appt_at'])) if l['appt_at'] else esc(fmt_day(l['day'])) + '<br><small>' + esc(WIN.get(l['win'], '')) + '</small>'}</td><td>{esc(CONTACT.get(l['contact'], ''))}{' 🔔' if l['client_chat'] else ''}<br><small>{esc(l['handle'] or '')}</small></td>"
        f"<td>{esc(l['comment'])}</td><td><span class='st {l['status']}'>{STATUS.get(l['status'], l['status'])}</span></td></tr>" for l in rows)
    key = urllib.parse.quote(cfg["REPORT_KEY"])
    chips = "".join(f"<a class='chip{' on' if days == d else ''}' href='?key={key}&days={d}'>{n}</a>" for d, n in [(7, "7 дней"), (30, "30 дней"), (0, "всё время")])
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Заявки · Эйфория</title><style>
body{{margin:0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0d0d0e;color:#f4f2ee}}
main{{max-width:1200px;margin:0 auto;padding:24px 16px}} h1{{font-weight:400;margin:0 0 4px}} p{{color:#b0aca8;margin:0 0 16px;white-space:pre-line}}
a{{color:#c8b08a}} .chip{{display:inline-block;padding:6px 12px;border:1px solid #444;border-radius:999px;margin:0 6px 16px 0;text-decoration:none;color:#b0aca8}}
.chip.on{{background:#f4f2ee;color:#0d0d0e}} .wrap{{overflow-x:auto;border:1px solid #2a2a2d;border-radius:12px}}
table{{border-collapse:collapse;width:100%;font-size:14px;min-width:900px}} th,td{{padding:10px;border-bottom:1px solid #2a2a2d;text-align:left;vertical-align:top}}
th{{color:#8e8a86;font-weight:500}} td:nth-child(1),td:nth-child(3){{white-space:nowrap}} .r{{text-align:right;white-space:nowrap}} small{{color:#8e8a86}}
.st{{padding:3px 8px;border-radius:999px;font-size:12px;white-space:nowrap;border:1px solid #555}} .st.came{{background:#c8b08a;color:#1a140c;border:0}}
.st.confirmed{{border-color:#7fc79a;color:#7fc79a}} .st.noanswer,.st.cancelled{{color:#e08a80;border-color:#e08a80}}</style></head>
<body><main><h1>Заявки «Эйфории»</h1><p>{esc(summary(days or 3650)).replace('За 3650 дней', 'За всё время')}</p>{chips}
<a href="/report.csv?key={key}&days={days}">Скачать таблицей (CSV)</a><br><br>
<div class="wrap"><table><tr><th>Дата</th><th>Имя</th><th>Телефон</th><th>Услуги</th><th class=r>Сумма, ₽</th><th>Когда</th><th>Связь</th><th>Комментарий</th><th>Статус</th></tr>
{tr or '<tr><td colspan=9>Заявок пока нет</td></tr>'}</table></div><p style="margin-top:12px">Статусы меняются кнопками под заявкой в Telegram.</p></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "EuphoriaLeads/1.0"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def key_ok(self, qs):
        return secrets.compare_digest(qs.get("key", [""])[0], cfg["REPORT_KEY"])

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        days = int(qs.get("days", ["0"])[0] or 0) if qs.get("days", ["0"])[0].isdigit() else 0
        if u.path in ("/", "/index.html"):
            if not PAGE_PATH.exists():
                return self.send(404, "Нет файла public/index.html")
            page = PAGE_PATH.read_text(encoding="utf-8").replace("<script>", "<script>window.EUPHORIA_ENDPOINT='/api/lead';window.EUPHORIA_BOT=" + json.dumps(BOT["username"]) + ";", 1)
            return self.send(200, page, "text/html; charset=utf-8")
        if u.path == "/admin":
            return self.send(200, admin_html(days or 30), "text/html; charset=utf-8") if self.key_ok(qs) else self.send(403, "Нужен ?key=ПАРОЛЬ")
        if u.path == "/report.csv":
            if not self.key_ok(qs):
                return self.send(403, "Нужен ?key=ПАРОЛЬ")
            return self.send(200, csv_bytes(all_leads(days)), "text/csv; charset=utf-8", {"Content-Disposition": 'attachment; filename="euphoria-zayavki.csv"'})
        if u.path == "/health":
            return self.send(200, "ok")
        self.send(404, "Не найдено")

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/lead":
            return self.send(404, "Не найдено")
        ip = self.client_address[0]
        now = time.time()
        rate[ip] = [t for t in rate.get(ip, []) if now - t < 60] + [now]
        if len(rate[ip]) > 5:
            return self.send(429, json.dumps({"ok": False, "error": "Слишком много заявок, попробуйте через минуту"}), "application/json")
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 64_000)).decode("utf-8"))
        except Exception:
            return self.send(400, json.dumps({"ok": False, "error": "Неверный формат"}), "application/json")
        lead = clean_lead(body if isinstance(body, dict) else {})
        if not lead:
            return self.send(400, json.dumps({"ok": False, "error": "Не хватает данных"}), "application/json")
        i, ok, tok = save_lead(lead)
        # Заявка сохранена в любом случае; если Telegram недоступен, она видна на странице /admin.
        self.send(200, json.dumps({"ok": True, "id": i, "telegram": ok, "bot": BOT["username"], "start": tok}), "application/json")


def main():
    load_config()
    init_db()
    if cfg["BOT_TOKEN"]:
        threading.Thread(target=poll_loop, daemon=True).start()
    else:
        log("BOT_TOKEN пуст: заявки сохраняются, но в Telegram не отправляются.")
    if cfg.get("UPDATE_REPO") and cfg.get("UPDATE_AUTO"):
        threading.Thread(target=update_loop, daemon=True).start()
    if cfg.get("BOT_TOKEN"):
        threading.Thread(target=resend_loop, daemon=True).start()
        threading.Thread(target=reminder_loop, daemon=True).start()
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"  # на Windows иначе можно случайно запустить два сервера на одном порту
    for attempt in range(6):  # после перезапуска порт может освободиться не сразу
        try:
            srv = ThreadingHTTPServer((cfg["HOST"], int(cfg["PORT"])), Handler)
            break
        except OSError:
            time.sleep(2)
    else:
        sys.exit(f"Порт {cfg['PORT']} занят: сервер уже запущен в другом окне. Закройте лишнее окно и запустите снова.")
    port = int(cfg["PORT"])
    log("Обновления:", (cfg["UPDATE_REPO"] + (" · автоматически" if cfg.get("UPDATE_AUTO") else " · по команде /update")) if cfg.get("UPDATE_REPO") else "выключены")
    log(f"Сервер запущен: http://localhost:{port}/  ·  заявки: http://localhost:{port}/admin?key={cfg['REPORT_KEY']}")
    if not cfg["CHAT_ID"]:
        log(f"Администратор: напишите боту в Telegram  /admin {cfg['REPORT_KEY']}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Остановлено")


if __name__ == "__main__":
    main()
