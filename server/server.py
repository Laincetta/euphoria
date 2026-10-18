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
    "SALON_PHONE": "+7 (965) 340-82-87",
    "SITE_URL": "",
    "MAP_URL": "",  # ссылка на салон в Яндекс Картах (Поделиться → Скопировать); пусто — поиск по адресу  # адрес страницы записи, когда сервер будет в интернете
    "REVIEW_URL": "",  # ссылка на отзывы в Яндекс Картах — бот попросит отзыв после визита
    # оператор персональных данных — для страниц /consent и /privacy (данные владелицы или её ИП/ООО)
    "OPERATOR_NAME": "",  # например: ИП Иванова Анна Андреевна
    "OPERATOR_INN": "",
    "OPERATOR_OGRN": "",  # ОГРНИП или ОГРН
    "OPERATOR_ADDRESS": "",
    "OPERATOR_EMAIL": "",
    "DATA_KEEP_DAYS": 365,
    "DIGEST_HOUR": 9,  # во сколько (МСК) бот присылает админу записи на день; 0 — не присылать  # заявки старше этого срока удаляются автоматически
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
    saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg = dict(DEFAULT_CONFIG, **saved)
    if set(DEFAULT_CONFIG) - set(saved):  # после обновления дописываем новые настройки в файл, чтобы их было видно
        save_config()
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
                         ("rem24", "INTEGER DEFAULT 0"), ("rem2", "INTEGER DEFAULT 0"),
                         ("client_ok", "INTEGER DEFAULT 0"), ("tg_username", "TEXT")]:  # обновление старой базы
            if col not in have:
                con.execute(f"ALTER TABLE leads ADD COLUMN {col} {typ}")
        # какое сообщение в чате админа к какой заявке относится: вопрос о времени, пересланное сообщение клиента
        con.execute("CREATE TABLE IF NOT EXISTS msgmap(admin_msg INTEGER PRIMARY KEY, lead_id TEXT, kind TEXT)")
        # вопросы клиентов админу: кто, что спросил, ответили ли
        con.execute("""CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, chat TEXT, lead_id TEXT,
            name TEXT, phone TEXT, dir TEXT, text TEXT, handled INTEGER DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, chat TEXT, lead_id TEXT,
            name TEXT, phone TEXT, text TEXT, answered_at TEXT, answer TEXT)""")
        if not con.execute("SELECT 1 FROM messages LIMIT 1").fetchone():  # перенос вопросов из прошлой версии в диалоги
            for q in con.execute("SELECT * FROM questions ORDER BY at").fetchall():
                con.execute("INSERT INTO messages(at,chat,lead_id,name,phone,dir,text,handled) VALUES(?,?,?,?,?,'in',?,?)",
                            (q[1], q[2], q[3], q[4], q[5], q[6], 1 if q[7] else 0))
                if q[8] and q[8] != "(отмечено вручную)":
                    con.execute("INSERT INTO messages(at,chat,name,dir,text,handled) VALUES(?,?,'Администратор','out',?,1)", (q[7], q[2], q[8]))


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
    if not iso:
        return ""
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
        out.append(f"<b>Записан(а):</b> {fmt_appt(l['appt_at'])}" + (" — клиент подтвердил, что придёт" if l["client_ok"] else ""))
    if l["status"] and l["status"] != "new":
        out += ["", f"<b>Статус: {STATUS[l['status']]}</b> · {esc(l['status_by'] or '')} {fmt_time(l['status_at'])}"]
    return "\n".join(out)


def contact_row(l):
    """Кнопки «связаться»: от имени бота (если клиент подключил бота) и из своего Telegram/MAX."""
    if not l:
        return []
    row = []
    if l["client_chat"]:
        row.append({"text": "Написать от бота", "callback_data": f"rp:d{l['client_chat']}"})
    nick = (l["tg_username"] or "") or (l["handle"][1:] if l["contact"] == "telegram" and (l["handle"] or "").startswith("@") else "")
    digits = re.sub(r"\D", "", l["phone"] or "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if l["contact"] == "max" and re.match(r"(https?://)?(web\.)?max\.ru/\S+$", l["handle"] or ""):
        h = l["handle"]
        row.append({"text": "Открыть в MAX", "url": h if h.startswith("http") else "https://" + h})
    elif nick:
        row.append({"text": "Написать в Telegram", "url": f"https://t.me/{nick}"})
    elif len(digits) >= 10:  # по номеру: откроется, если клиент разрешил находить себя по телефону
        row.append({"text": "Найти в Telegram по номеру", "url": f"https://t.me/+{digits}"})
    return [row] if row else []


def keyboard(i, closed=False):
    b = lambda t, s: {"text": t, "callback_data": f"st:{i}:{s}"}
    contact = contact_row(get_lead(i))
    if closed:  # статус выбран — кнопки свёрнуты, остаётся одна, чтобы исправить ошибку
        return {"inline_keyboard": [[b("Изменить статус", "edit")]] + contact}
    return {"inline_keyboard": [[b("Подтвердил запись", "confirmed"), b("Не дозвонился", "noanswer")],
                                [b("Клиент пришёл", "came"), b("Отмена", "cancelled")]] + contact}


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


def to_client(l, text, kb=None):
    if l and l["client_chat"]:
        r = tg("sendMessage", dict({"chat_id": l["client_chat"], "text": text}, **({"reply_markup": kb} if kb else {})))
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
    sent = to_client(l, f"Вы записаны в салон «{cfg['SALON_NAME']}»\n\n{fmt_appt(l['appt_at'])}\n{services_line(l)}\n\n"
                        f"Адрес: {cfg['SALON_ADDRESS']}\nТелефон: {salon_phone()}\n\nНапомним за сутки и за 2 часа до визита.",
                     appt_kb(l))
    tg("sendMessage", {"chat_id": admin_chat, "reply_markup": {"inline_keyboard": [[{"text": "Открыть записи", "callback_data": "up:all"}]]},
                       "text": f"Готово: {l['name']} — {fmt_appt(l['appt_at'])}. " + (
        "Клиенту ушло подтверждение, напомним за сутки и за 2 часа." if sent else
        {"call": "Клиент выбрал звонок", "max": "Клиент выбрал MAX"}.get(l["contact"], "Клиент не подключил бота") +
        " — подтвердите ему сами. За сутки до визита бот напомнит вам связаться с ним.")})
    log("Время записи", l["id"], "→", fmt_appt(l["appt_at"]))


def route_url():
    """Карточка салона на Яндекс Картах (а не маршрут от текущего места): там уже есть кнопка «Маршрут»."""
    return cfg.get("MAP_URL") or ("https://yandex.ru/maps/?text=" +
                                  urllib.parse.quote(f"салон красоты {cfg['SALON_NAME']}, {cfg['SALON_ADDRESS']}"))


def cb(text, data):
    return {"text": text, "callback_data": data}


def reviews_url():
    """Отзывы о салоне в Яндекс Картах: REVIEW_URL, иначе карточка из MAP_URL + reviews/, иначе поиск салона."""
    if cfg.get("REVIEW_URL"):
        return cfg["REVIEW_URL"]
    m = (cfg.get("MAP_URL") or "").split("?")[0]
    if "/maps/org/" in m:
        m = m.rstrip("/")
        return (m[:-len("/reviews")] if m.endswith("/reviews") else m) + "/reviews/"
    return route_url()


def menu_kb():
    rows = [[cb("Моя запись", "c:my")], [cb("Задать вопрос администратору", "c:ask")],
            [{"text": "Как добраться", "url": route_url()}, {"text": "Отзывы", "url": reviews_url()}]]
    if cfg.get("SITE_URL"):
        rows.insert(1, [{"text": "Записаться на сайте", "url": cfg["SITE_URL"]}])
    return {"inline_keyboard": rows}


def appt_kb(l):
    """Кнопки под записью клиента — зависят от её состояния."""
    rows = []
    if l["status"] == "confirmed" and l["appt_at"]:
        if not l["client_ok"]:
            rows.append([cb("Приду", f"c:yes:{l['id']}")])
        rows.append([cb("Перенести", f"c:move:{l['id']}"), cb("Отменить запись", f"c:cancel:{l['id']}")])
    elif l["status"] in ("new", "noanswer", "confirmed"):
        rows.append([cb("Изменить пожелания", f"c:move:{l['id']}"), cb("Отменить заявку", f"c:cancel:{l['id']}")])
    rows.append([{"text": "Как добраться", "url": route_url()}, cb("Меню", "c:menu")])
    return {"inline_keyboard": rows}


def client_leads(chat):
    with db_lock, db() as con:
        return con.execute("SELECT * FROM leads WHERE client_chat=? ORDER BY created_at DESC LIMIT 10", (str(chat),)).fetchall()


def active_lead(chat):
    now = datetime.now(MSK)
    for l in client_leads(chat):
        if l["status"] in ("new", "noanswer") or (l["status"] == "confirmed" and (not l["appt_at"] or
                                                   datetime.fromisoformat(l["appt_at"]) >= now - timedelta(hours=1))):
            return l
    return None


def my_text(l):
    if not l:
        return ("Сейчас у вас нет активных записей." + (f"\nЗаписаться можно на сайте или по телефону {salon_phone()}."
                                                         if cfg.get("SITE_URL") else f"\nЗаписаться можно по телефону {salon_phone()}."))
    if l["status"] == "confirmed" and l["appt_at"]:
        head = "Ваша запись подтверждена" + (" — вы отметили, что придёте" if l["client_ok"] else "")
        when = fmt_appt(l["appt_at"])
    else:
        head = "Заявка принята, администратор скоро свяжется"
        when = f"хотели бы: {fmt_day(l['day'])}, {WIN.get(l['win'], '')}"
    total = ("от " if l["total_from"] else "") + rub(l["total"]) + " ₽"
    return (f"{head}\n\n{when}\n{services_line(l)}\nСтоимость: {total}\n\n"
            f"Салон «{cfg['SALON_NAME']}», {cfg['SALON_ADDRESS']}\nТелефон: {salon_phone()}")


def client_start(chat, tok, first_name, username="", auto=False):
    with db_lock, db() as con:
        l = con.execute("SELECT * FROM leads WHERE tok=?", (tok,)).fetchone()
        if l and not l["client_chat"]:
            con.execute("UPDATE leads SET client_chat=?, tg_username=? WHERE id=?", (str(chat), username or None, l["id"]))
    if not l or (l["client_chat"] and l["client_chat"] != str(chat)):
        return client_hello(chat)
    l = get_lead(l["id"])
    w = WIN.get(l["win"], "")
    tg("sendMessage", {"chat_id": chat, "reply_markup": appt_kb(l),
                       "text": f"Здравствуйте, {l['name']}! Заявка в салон «{cfg['SALON_NAME']}» получена.\n\n"
                               f"{services_line(l)}\nКогда удобно: {fmt_day(l['day'])}, {w}\n\n"
                               "Администратор позвонит или напишет и подтвердит точное время. "
                               "Подтверждение и напоминания придут сюда." +
                               ("\n\nЕсли вы не оставляли эту заявку — нажмите «Отменить заявку»." if auto else "")})
    refresh_admin_msg(l)
    log("Клиент подключил уведомления:", l["id"], l["name"])


def salon_phone():
    return cfg.get("SALON_PHONE") or DEFAULT_CONFIG["SALON_PHONE"]


def client_hello(chat):
    l = active_lead(chat)
    text = (f"Салон красоты «{cfg['SALON_NAME']}»\n{cfg['SALON_ADDRESS']}, ежедневно 10:00–21:00\nТелефон: {salon_phone()}\n\n"
            "Здесь приходят подтверждения записи и напоминания. Можно посмотреть свою запись, перенести или отменить её "
            "и написать администратору.")
    tg("sendMessage", {"chat_id": chat, "text": text, "reply_markup": menu_kb() if not l else
                       {"inline_keyboard": [[cb("Моя запись", "c:my")], [cb("Задать вопрос администратору", "c:ask")],
                                            [{"text": "Как добраться", "url": route_url()}, {"text": "Отзывы", "url": reviews_url()}]]}})


def notify_admin(l, text, fwd=True):
    """Сообщение админу о действии клиента. fwd=True — это сообщение клиента: попадает в «Диалоги», под ним «Ответить»."""
    if not cfg.get("CHAT_ID"):
        return
    if fwd:
        return add_question(l["client_chat"], l, l["name"], l["phone"], text)
    tg("sendMessage", {"chat_id": cfg["CHAT_ID"], "text": text})


def dlg_kb(chat):
    return {"inline_keyboard": [[{"text": "Ответить", "callback_data": f"rp:d{chat}"},
                                 {"text": "Открыть диалог", "callback_data": f"dl:open:{chat}"}]]}


def add_question(chat, l, name, phone, text, header=None):
    """Входящее сообщение клиента: в историю диалога + уведомление админу."""
    with db_lock, db() as con:
        con.execute("INSERT INTO messages(at,chat,lead_id,name,phone,dir,text,handled) VALUES(?,?,?,?,?,'in',?,0)",
                    (datetime.now(timezone.utc).isoformat(), str(chat or ""), l["id"] if l else None, name, phone, text))
    head = header or (f"Новое сообщение от {name}" + (f", {nice_phone(phone)}" if phone else ""))
    r = tg("sendMessage", {"chat_id": cfg["CHAT_ID"], "text": f"{head}\n\n{text}", "reply_markup": dlg_kb(chat)})
    if r.get("ok"):
        map_msg(r["result"]["message_id"], f"d{chat}", "fwd")
    log(f"Сообщение от {name}:", "передано админу" if r.get("ok") else "НЕ передано — " + str(r.get("description")))
    return r.get("ok")


def resolve_target(target):
    """Кому отвечаем → chat_id клиента. Цели: d<chat>, q<id> (старые вопросы), chat:<id>, id заявки."""
    t = str(target)
    if t.startswith("d"):
        return t[1:]
    if t.startswith("chat:"):
        return t[5:]
    if t.startswith("q") and t[1:].isdigit():
        with db_lock, db() as con:
            row = con.execute("SELECT chat FROM questions WHERE id=?", (int(t[1:]),)).fetchone()
        return row["chat"] if row else None
    l = get_lead(t)
    return l["client_chat"] if l else None


def dialog_person(chat):
    """Имя и телефон собеседника — из последнего его сообщения или из заявки."""
    with db_lock, db() as con:
        m = con.execute("SELECT name, phone, lead_id FROM messages WHERE chat=? AND dir='in' ORDER BY at DESC LIMIT 1", (str(chat),)).fetchone()
        l = con.execute("SELECT * FROM leads WHERE client_chat=? ORDER BY created_at DESC LIMIT 1", (str(chat),)).fetchone()
    name = (m["name"] if m else None) or (l["name"] if l else "Клиент")
    phone = (m["phone"] if m and m["phone"] else None) or (l["phone"] if l else "")
    return name, phone, l


def reply_btn(target, label="Ответить"):
    return {"inline_keyboard": [[{"text": label, "callback_data": f"rp:{target}"}]]}


def ask_reply(target):
    """Админ нажал «Ответить» — бот открывает поле ответа; следующий текст уйдёт клиенту."""
    chat = resolve_target(target)
    if not chat:
        return tg("sendMessage", {"chat_id": cfg["CHAT_ID"], "text": "Этот клиент не подключал бота — написать ему можно только по телефону."})
    name, phone, _ = dialog_person(chat)
    with db_lock, db() as con:
        last = con.execute("SELECT text FROM messages WHERE chat=? AND dir='in' ORDER BY at DESC LIMIT 1", (str(chat),)).fetchone()
    quote = f"\nПоследнее сообщение: «{last['text'][:300]}»" if last else ""
    r = tg("sendMessage", {"chat_id": cfg["CHAT_ID"], "text": f"Ответ для {name}" + (f", {nice_phone(phone)}" if phone else "") +
                           f".{quote}\n\nНапишите ответ и отправьте.",
                           "reply_markup": {"force_reply": True, "input_field_placeholder": f"Ответ для {name}"[:64]}})
    if r.get("ok"):
        map_msg(r["result"]["message_id"], f"d{chat}", "fwd")


def send_answer(target, text):
    """Ответ админа клиенту. Возвращает (ok, кому)."""
    chat = resolve_target(target)
    name = dialog_person(chat)[0] if chat else "клиенту"
    if not chat:
        log("Ответ админа: у клиента нет чата с ботом")
        return False, name
    r = tg("sendMessage", {"chat_id": chat, "text": f"Администратор салона «{cfg['SALON_NAME']}»:\n{text}",
                           "reply_markup": {"inline_keyboard": [[cb("Ответить", "c:reply"), cb("Меню", "c:menu")]]}})
    if r.get("ok"):
        with db_lock, db() as con:
            con.execute("INSERT INTO messages(at,chat,lead_id,name,phone,dir,text,handled) VALUES(?,?,NULL,?,NULL,'out',?,1)",
                        (datetime.now(timezone.utc).isoformat(), str(chat), "Администратор", text))
            con.execute("UPDATE messages SET handled=1 WHERE chat=? AND dir='in'", (str(chat),))
    log("Ответ админа →", name, "доставлен" if r.get("ok") else "НЕ доставлен — " + str(r.get("description")))
    return r.get("ok"), name


def when_short(iso):
    x = datetime.fromisoformat(iso).astimezone(MSK)
    d = (datetime.now(MSK).date() - x.date()).days
    return x.strftime("%H:%M") if d == 0 else ("вчера " + x.strftime("%H:%M")) if d == 1 else x.strftime("%d.%m %H:%M")


PAGE = 8


def dialogs_view(page=0):
    """Список диалогов: сначала ждущие ответа, затем по свежести."""
    with db_lock, db() as con:
        chats = con.execute("""SELECT chat, MAX(at) AS last_at, SUM(CASE WHEN dir='in' AND handled=0 THEN 1 ELSE 0 END) AS waiting
                               FROM messages WHERE chat<>'' GROUP BY chat
                               ORDER BY (SUM(CASE WHEN dir='in' AND handled=0 THEN 1 ELSE 0 END) > 0) DESC, MAX(at) DESC""").fetchall()
        lasts = {r["chat"]: r for r in con.execute(
            "SELECT m.* FROM messages m JOIN (SELECT chat, MAX(at) AS a FROM messages GROUP BY chat) x ON m.chat=x.chat AND m.at=x.a")}
    waiting = sum(1 for c in chats if c["waiting"])
    out = ["<b>Диалоги с клиентами</b>",
           (f"Ждут ответа: {waiting}" if waiting else "Все ответы отправлены.") if chats else "Пока никто не писал."]
    part = chats[page * PAGE:(page + 1) * PAGE]
    kb = []
    for i, c in enumerate(part, page * PAGE + 1):
        name, phone, _ = dialog_person(c["chat"])
        last = lasts.get(c["chat"])
        preview = ("Вы: " if last and last["dir"] == "out" else "") + (last["text"] if last else "")
        preview = preview if len(preview) <= 70 else preview[:69] + "…"
        mark = f" — ждёт ответа ({c['waiting']})" if c["waiting"] else ""
        out += ["", f"<b>{i}. {esc(name)}</b>" + (f", {nice_phone(phone)}" if phone else "") + f" · {when_short(c['last_at'])}{mark}",
                f"   {esc(preview)}"]
        kb.append([{"text": f"{i}. {name[:24]}" + (" — ждёт ответа" if c["waiting"] else ""), "callback_data": f"dl:open:{c['chat']}"}])
    nav = []
    if page > 0:
        nav.append({"text": "← Назад", "callback_data": f"dl:list:{page - 1}"})
    if (page + 1) * PAGE < len(chats):
        nav.append({"text": "Ещё →", "callback_data": f"dl:list:{page + 1}"})
    kb += [nav] if nav else []
    kb.append([{"text": "Обновить", "callback_data": f"dl:list:{page}"}, {"text": "Закрыть", "callback_data": "x:close"}])
    return "\n".join(out)[:4000], {"inline_keyboard": kb}


def dialog_view(chat):
    name, phone, l = dialog_person(chat)
    with db_lock, db() as con:
        msgs = con.execute("SELECT * FROM messages WHERE chat=? ORDER BY at DESC LIMIT 20", (str(chat),)).fetchall()[::-1]
        waiting = con.execute("SELECT COUNT(*) FROM messages WHERE chat=? AND dir='in' AND handled=0", (str(chat),)).fetchone()[0]
    out = [f"<b>Диалог: {esc(name)}</b>" + (f", {nice_phone(phone)}" if phone else "")]
    if l:
        st = STATUS.get(l["status"], l["status"])
        when = fmt_appt(l["appt_at"]) if l["appt_at"] else f"хочет {fmt_day(l['day'])}"
        out.append(f"Заявка: {esc(services_line(l))} · {esc(when)} · {st}")
    out.append("—" * 12)
    for m in msgs:
        who_ = "Вы" if m["dir"] == "out" else esc(name)
        out += ["", f"<b>{who_}</b> · {when_short(m['at'])}", esc(m["text"])]
    if not msgs:
        out += ["", "Сообщений пока нет."]
    text = "\n".join(out)
    while len(text) > 4000 and len(out) > 6:  # длинная переписка — убираем самые старые сообщения
        del out[3:6]
        text = "\n".join(out)
    kb = [[{"text": "Ответить", "callback_data": f"rp:d{chat}"}]]
    if waiting:
        kb.append([{"text": "Отметить как отвеченный", "callback_data": f"dl:done:{chat}"}])
    kb.append([{"text": "← Все диалоги", "callback_data": "dl:list:0"}, {"text": "Обновить", "callback_data": f"dl:open:{chat}"}])
    kb.append([{"text": "Закрыть", "callback_data": "x:close"}])
    return text, {"inline_keyboard": kb}


def dialog_callback(q):
    parts = q["data"].split(":")
    act, arg = parts[1], (parts[2] if len(parts) > 2 else "0")
    mid = q["message"]["message_id"]
    if act == "done":
        with db_lock, db() as con:
            con.execute("UPDATE messages SET handled=1 WHERE chat=? AND dir='in'", (arg,))
        act = "open"
    text, kb = dialog_view(arg) if act == "open" else dialogs_view(int(arg) if arg.isdigit() else 0)
    tg("editMessageText", {"chat_id": cfg["CHAT_ID"], "message_id": mid, "text": text, "parse_mode": "HTML", "reply_markup": kb})


ADMIN_KB = {"keyboard": [[{"text": "Записи"}, {"text": "Диалоги"}], [{"text": "Сводка"}]],
            "resize_keyboard": True, "is_persistent": True}


def who(l):
    return f"{l['name']} ({nice_phone(l['phone'])})"


def client_callback(q):
    """Кнопки клиента: c:my, c:menu, c:ask, c:yes:ID, c:move:ID, c:cancel:ID, c:cancelok:ID."""
    chat, mid = q["message"]["chat"]["id"], q["message"]["message_id"]
    parts = q["data"].split(":")
    act, lid = parts[1], (parts[2] if len(parts) > 2 else "")
    ans = lambda text="": tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": text})
    edit = lambda text, kb: tg("editMessageText", {"chat_id": chat, "message_id": mid, "text": text, "reply_markup": kb})
    if act == "my":
        l = active_lead(chat)
        ans()
        return tg("sendMessage", {"chat_id": chat, "text": my_text(l), "reply_markup": appt_kb(l) if l else menu_kb()})
    if act == "menu":
        ans()
        return client_hello(chat)
    if act in ("ask", "reply"):  # поле ввода открывается сразу, следующий текст уйдёт администратору
        ans()
        return tg("sendMessage", {"chat_id": chat, "text": "Напишите ваш вопрос одним сообщением — администратор ответит здесь."
                                  if act == "ask" else "Напишите ответ администратору:",
                                  "reply_markup": {"force_reply": True, "input_field_placeholder": "Сообщение администратору"}})
    l = get_lead(lid) if lid else None
    if not l or l["client_chat"] != str(chat):
        return ans("Запись не найдена")
    if act == "yes":
        with db_lock, db() as con:
            con.execute("UPDATE leads SET client_ok=1 WHERE id=?", (l["id"],))
        l = get_lead(l["id"])
        refresh_admin_msg(l)
        notify_admin(l, f"{who(l)} подтвердил(а), что придёт: {fmt_appt(l['appt_at'])}.", fwd=False)
        ans("Спасибо! Ждём вас")
        return tg("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid, "reply_markup": appt_kb(l)})
    if act == "move":
        when = fmt_appt(l["appt_at"]) if l["appt_at"] else f"{fmt_day(l['day'])}, {WIN.get(l['win'], '')}"
        add_question(chat, l, l["name"], l["phone"], f"Просит перенести запись ({when}). Клиент сейчас напишет удобное время — "
                     "или позвоните ему.", header=f"{who(l)} — перенос записи")
        ans()
        return tg("sendMessage", {"chat_id": chat, "text": "Напишите, когда вам удобно — например «в четверг после 18:00». "
                                                           "Администратор подберёт время и ответит здесь."})
    if act == "cancel":
        ans()
        return edit(my_text(l) + "\n\nТочно отменить?",
                    {"inline_keyboard": [[cb("Да, отменить", f"c:cancelok:{l['id']}"), cb("Нет", f"c:keep:{l['id']}")]]})
    if act == "keep":
        ans()
        return edit(my_text(l), appt_kb(l))
    if act == "cancelok":
        with db_lock, db() as con:
            con.execute("UPDATE leads SET status='cancelled', status_at=?, status_by=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), "клиент", l["id"]))
        l = get_lead(l["id"])
        refresh_admin_msg(l)
        when = fmt_appt(l["appt_at"]) if l["appt_at"] else f"заявка на {fmt_day(l['day'])}"
        notify_admin(l, f"{who(l)} отменил(а) запись: {when}, {services_line(l)}.", fwd=False)
        log("Клиент отменил", l["id"])
        ans("Запись отменена")
        return edit(f"Запись отменена.\n\nЕсли захотите записаться снова — напишите сюда или позвоните: {salon_phone()}.", menu_kb())
    ans()


def client_message(chat, text, m):
    l = client_leads(chat)
    l = l[0] if l else None
    if not cfg.get("CHAT_ID"):
        return client_hello(chat)
    if l:
        ok = add_question(chat, l, l["name"], l["phone"], text)
    else:  # человек нашёл бота сам, без заявки — всё равно передаём админу
        u = m.get("from") or {}
        name = " ".join(x for x in [u.get("first_name"), u.get("last_name")] if x) or "Без имени"
        nick = f" @{u['username']}" if u.get("username") else ""
        ok = add_question(chat, None, name + nick, "", text, header=f"Новое сообщение от {name}{nick} (без заявки)")
    tg("sendMessage", {"chat_id": chat, "text": "Передали администратору. Ответ придёт сюда." if ok else
                       f"Не получилось передать сообщение. Позвоните, пожалуйста: {salon_phone()}"})


def admin_reply(chat, text, reply_to):
    with db_lock, db() as con:
        mm = con.execute("SELECT * FROM msgmap WHERE admin_msg=?", (reply_to,)).fetchone()
        if not mm:  # ответ прямо на сообщение с заявкой — тоже считаем временем записи
            row = con.execute("SELECT id FROM leads WHERE message_id=? ORDER BY created_at DESC LIMIT 1", (reply_to,)).fetchone()
            mm = {"lead_id": row["id"], "kind": "ask"} if row else None
    if not mm:
        return False
    if mm["kind"] == "fwd":
        ok, name = send_answer(mm["lead_id"], text)
        tg("sendMessage", {"chat_id": chat, "text": f"Отправлено: {name}." if ok else
                           f"Не получилось отправить ({name}): клиент не подключал бота. Позвоните ему.",
                           **({"reply_markup": {"inline_keyboard": [[{"text": "Открыть диалог", "callback_data": f"dl:open:{resolve_target(mm['lead_id'])}"}]]}}
                              if ok else {})})
        return True
    l = get_lead(mm["lead_id"])
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
                rows = con.execute("SELECT * FROM leads WHERE appt_at IS NOT NULL AND status='confirmed' "
                                   "AND (rem24=0 OR rem2=0)").fetchall()
            for l in rows:
                when = datetime.fromisoformat(l["appt_at"])
                if when < now:
                    continue
                if not l["client_chat"]:  # бот не может написать клиенту — напоминаем админу, чтобы позвонил или написал
                    if not l["rem24"] and now >= when - timedelta(hours=24) and cfg.get("CHAT_ID"):
                        how = {"call": "позвоните", "max": "напишите в MAX"}.get(l["contact"], "позвоните или напишите")
                        tg("sendMessage", {"chat_id": cfg["CHAT_ID"], "text": f"📞 Напомните клиенту о записи — {how}:\n{l['name']}, {l['phone']}"
                                           f"{(' · ' + l['handle']) if l['handle'] else ''}\n{fmt_appt(l['appt_at'])} · {services_line(l)}"})
                        log("Напоминание админу", l["id"])
                    with db_lock, db() as con:
                        con.execute("UPDATE leads SET rem24=CASE WHEN ? THEN 1 ELSE rem24 END, rem2=1 WHERE id=?",
                                    (now >= when - timedelta(hours=24), l["id"]))
                    continue
                for col, before, text in [("rem24", timedelta(hours=24), "Напоминаем: {d} ждём вас в салоне «{n}»\n{t}"),
                                          ("rem2", timedelta(hours=2), "Через 2 часа ждём вас в салоне «{n}»\n{t}")]:
                    if not l[col] and now >= when - before:
                        t = fmt_appt(l["appt_at"]) + "\n" + services_line(l) + "\nАдрес: " + cfg["SALON_ADDRESS"]
                        d = {0: "сегодня", 1: "завтра"}.get((when.astimezone(MSK).date() - now.date()).days, "скоро")
                        ok = to_client(l, text.format(n=cfg["SALON_NAME"], t=t, d=d) + "\n\nЕсли планы изменились — нажмите «Перенести» или «Отменить».",
                                       appt_kb(l))
                        with db_lock, db() as con:
                            con.execute(f"UPDATE leads SET {col}=1 WHERE id=?", (l["id"],))
                        log("Напоминание", col, l["id"], "отправлено" if ok else "не доставлено")
                        break
        except Exception as e:
            log("Ошибка напоминаний:", e)


def set_commands():
    tg("setMyCommands", {"commands": [{"command": "my", "description": "Моя запись"}, {"command": "start", "description": "Меню"}]})
    if cfg.get("CHAT_ID"):
        tg("setMyCommands", {"scope": {"type": "chat", "chat_id": int(cfg["CHAT_ID"])},
                             "commands": [{"command": "upcoming", "description": "Записи"},
                                          {"command": "dialogs", "description": "Диалоги с клиентами"},
                                          {"command": "report", "description": "Сводка по заявкам"},
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


def nice_phone(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d[0] in "78":
        return f"+7 {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:]}"
    return p


def day_title(d, today):
    x = d.date()
    rel = {0: "Сегодня", 1: "Завтра", 2: "Послезавтра"}.get((x - today).days)
    base = f"{DAYS[x.weekday()]}, {x.day} {MONTHS[x.month - 1]}"
    return f"{rel}, {base}" if rel else base[0].upper() + base[1:]


def appt_block(l, with_time=True):
    sv = "\n".join("   " + esc(s["name"]) for s in json.loads(l["services"]))
    t = datetime.fromisoformat(l["appt_at"]).astimezone(MSK).strftime("%H:%M") if with_time and l["appt_at"] else ""
    how = {"telegram": "Telegram" + (", бот подключён" if l["client_chat"] else ", бот не подключён"),
           "max": "MAX" + (f" ({l['handle']})" if l["handle"] else ""), "call": "звонок"}.get(l["contact"], l["contact"])
    total = ("от " if l["total_from"] else "") + rub(l["total"]) + " ₽"
    head = f"<b>{t}</b>  {esc(l['name'])}" if t else f"<b>{esc(l['name'])}</b>"
    lines = [head, f"   {nice_phone(l['phone'])}", sv, f"   {total} · связь: {esc(how)}"]
    if l["comment"]:
        lines.append(f"   «{esc(l['comment'])}»")
    return "\n".join(lines)


def plural(n, one, few, many):
    n = abs(n) % 100
    return one if n % 10 == 1 and n != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n <= 14 else many


CLOSE_BTN = {"text": "Закрыть", "callback_data": "x:close"}
PERIODS = {"today": "Сегодня", "tomorrow": "Завтра", "week": "Неделя", "all": "Все"}


def pending_leads():
    """Заявки без времени записи за последние 14 дней — их нужно подтвердить и назначить время."""
    with db_lock, db() as con:
        pend = con.execute("SELECT * FROM leads WHERE appt_at IS NULL AND status IN ('new','noanswer','confirmed') ORDER BY created_at").fetchall()
    return [l for l in pend if datetime.fromisoformat(l["created_at"]) >= datetime.now(timezone.utc) - timedelta(days=14)]


def period_keyboard(cur):
    kb = [[{"text": f"Подтвердить и назначить время: {i}. {l['name'][:18]}", "callback_data": f"tm:{l['id']}"}]
          for i, l in enumerate(pending_leads()[:8], 1)]
    return {"inline_keyboard": kb + [[{"text": ("• " if k == cur else "") + n, "callback_data": f"up:{k}"} for k, n in PERIODS.items()],
                                     [CLOSE_BTN]]}


def upcoming_text(period="all"):
    now = datetime.now(MSK)
    today = now.date()
    with db_lock, db() as con:
        rows = con.execute("SELECT * FROM leads WHERE appt_at IS NOT NULL AND status IN ('confirmed','came')").fetchall()
        pend = con.execute("SELECT * FROM leads WHERE appt_at IS NULL AND status IN ('new','noanswer','confirmed') ORDER BY created_at").fetchall()
    rows = sorted([l for l in rows if datetime.fromisoformat(l["appt_at"]) >= now - timedelta(hours=1)], key=lambda l: l["appt_at"])
    date_of = lambda l: datetime.fromisoformat(l["appt_at"]).astimezone(MSK).date()
    span = {"today": (0, 0), "tomorrow": (1, 1), "week": (0, 6)}.get(period)
    if span:
        rows = [l for l in rows if span[0] <= (date_of(l) - today).days <= span[1]]
    one_day = period in ("today", "tomorrow")
    if one_day:
        d = now + timedelta(days=span[0])
        title = f"Записи на {PERIODS[period].lower()}, {day_title(d, today).split(', ', 1)[1]}"
    else:
        title = {"week": "Записи на неделю", "all": "Все предстоящие записи"}[period]
    total = sum(int(l["total"] or 0) for l in rows)
    out = [f"<b>{title}</b>", f"{len(rows)} {plural(len(rows), 'запись', 'записи', 'записей')} на {rub(total)} ₽" if rows else
           "Записей с назначенным временем нет."]
    day = None
    for l in rows:
        if not one_day and date_of(l) != day:
            day = date_of(l)
            out += ["", f"<b>{day_title(datetime.fromisoformat(l['appt_at']).astimezone(MSK), today)}</b>", "—" * 12]
        out += ["", appt_block(l)]
    pend = pending_leads()
    if pend:
        out += ["", "", f"<b>Без времени записи ({len(pend)})</b>"]
        for n, l in enumerate(pend, 1):
            st = {"noanswer": "не дозвонились", "confirmed": "подтверждена, время не указано"}.get(l["status"], "новая, ждёт звонка")
            sv = json.loads(l["services"])
            first = (sv[0]["name"] if sv else "—") + (f" +{len(sv) - 1}" if len(sv) > 1 else "")
            out.append(f"{n}. <b>{esc(l['name'])}</b>, {nice_phone(l['phone'])} — {esc(first)} — хочет {esc(fmt_day(l['day']))} — {st}")
        out += ["", "<i>Нажмите «Подтвердить и назначить время» ниже — бот спросит время, и запись появится в расписании.</i>"]
    text = "\n".join(out)
    return text if len(text) < 4000 else text[:3980] + "\n…"


def send_upcoming(chat, period):
    tg("sendMessage", {"chat_id": chat, "text": upcoming_text(period), "parse_mode": "HTML", "reply_markup": period_keyboard(period)})


def digest_loop():
    sent_for = None
    while True:
        time.sleep(60)
        try:
            h, now = int(cfg.get("DIGEST_HOUR") or 0), datetime.now(MSK)
            if h and cfg.get("CHAT_ID") and now.hour == h and sent_for != now.date():
                sent_for = now.date()
                send_upcoming(cfg["CHAT_ID"], "today")
                log("Утренняя сводка записей отправлена")
        except Exception as e:
            log("Ошибка утренней сводки:", e)


def lead_block(l):
    """Заявка блоком в столбик: дата и статус, клиент, услуги, сумма и время, связь."""
    sv = "\n".join(esc(s["name"]) for s in json.loads(l["services"])) or "—"
    when = ("запись " + fmt_appt(l["appt_at"])) if l["appt_at"] else ("хочет " + fmt_day(l["day"]) + ", " + WIN.get(l["win"], ""))
    total = ("от " if l["total_from"] else "") + rub(l["total"]) + " ₽"
    how = {"telegram": "Telegram" + (", бот подключён" if l["client_chat"] else ""), "max": "MAX", "call": "звонок"}.get(l["contact"], l["contact"])
    return (f"<b>{fmt_time(l['created_at'])} · {STATUS.get(l['status'], l['status'])}</b>\n"
            f"{esc(l['name'])}, {nice_phone(l['phone'])}\n{sv}\n{total} · {esc(when)}\nСвязь: {esc(how)}")


def report_text():
    w, m = all_leads(7), all_leads(30)
    c = lambda ls, *s: sum(1 for l in ls if l["status"] in s)
    money = lambda ls: sum(int(l["total"] or 0) for l in ls if l["status"] == "came")
    conv = lambda ls: f"{round(100 * c(ls, 'came') / len(ls))}%" if ls else "—"
    rows = [("Заявок", len(w), len(m)), ("Подтверждено", c(w, "confirmed", "came"), c(m, "confirmed", "came")),
            ("Пришли", c(w, "came"), c(m, "came")), ("Дошли до визита", conv(w), conv(m)),
            ("Выручка, ₽", rub(money(w)), rub(money(m))), ("Не дозвонились", c(w, "noanswer"), c(m, "noanswer")),
            ("Отмены", c(w, "cancelled"), c(m, "cancelled")), ("Ждут звонка", c(w, "new"), c(m, "new"))]
    table = [f"{'':<16}{'7 дн':>7}{'30 дн':>8}"] + [f"{a:<16}{str(b):>7}{str(d):>8}" for a, b, d in rows]
    top = {}
    for l in m:
        for s in json.loads(l["services"]):
            top[s["name"]] = top.get(s["name"], 0) + 1
    best = sorted(top.items(), key=lambda x: -x[1])[:5]
    out = ["<b>Сводка по заявкам</b>", "<pre>" + esc("\n".join(table)) + "</pre>"]
    if best:
        out += ["<b>Популярные услуги за 30 дней</b>"] + [f"{i}. {esc(n)} — {k}" for i, (n, k) in enumerate(best, 1)]
    last = all_leads()[:15]
    blocks = [lead_block(l) for l in last]
    out += ["", f"<b>Последние заявки ({len(last)})</b>" if last else "Заявок пока нет.", "—" * 12]
    site = (cfg.get("SITE_URL") or "").rstrip("/")
    tail = ["", f'<a href="{html.escape(site)}/admin?key={urllib.parse.quote(cfg["REPORT_KEY"])}">Все заявки таблицей</a>' if site
            else "Все заявки таблицей — страница admin на сервере."]
    while blocks and len("\n".join(out + ["\n\n".join(blocks)] + tail)) > 4000:  # лимит сообщения — убираем самые старые
        blocks.pop()
    return "\n".join(out + ["\n\n".join(blocks)] + tail)


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
    if q and str(q.get("data", "")).startswith("rp:"):  # админ: «Ответить» под сообщением клиента
        if str(q["message"]["chat"]["id"]) == str(cfg["CHAT_ID"]):
            ask_reply(q["data"][3:])
        tg("answerCallbackQuery", {"callback_query_id": q["id"]})
        return
    if q and str(q.get("data", "")).startswith("tm:"):  # админ: подтвердить заявку из расписания и назначить время
        if str(q["message"]["chat"]["id"]) == str(cfg["CHAT_ID"]):
            l = get_lead(q["data"][3:])
            if l:
                if l["status"] in ("new", "noanswer"):
                    with db_lock, db() as con:
                        con.execute("UPDATE leads SET status='confirmed', status_at=?, status_by=? WHERE id=?",
                                    (datetime.now(timezone.utc).isoformat(), q["from"].get("first_name", ""), l["id"]))
                    l = get_lead(l["id"])
                    refresh_admin_msg(l)
                    log("Статус заявки", l["id"], "→ Подтверждена (из расписания)")
                ask_time(l)
        tg("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Напишите время записи"})
        return
    if q and q.get("data") == "x:close":  # «Закрыть» — убрать сообщение-меню из чата
        r = tg("deleteMessage", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"]})
        if not r.get("ok"):  # Telegram не даёт удалять сообщения старше 48 часов — тогда просто сворачиваем
            tg("editMessageText", {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"], "text": "Закрыто."})
        tg("answerCallbackQuery", {"callback_query_id": q["id"]})
        return
    if q and str(q.get("data", "")).startswith(("dl:", "qa:")):  # админ: диалоги с клиентами
        if str(q["message"]["chat"]["id"]) == str(cfg["CHAT_ID"]):
            if q["data"].startswith("qa:"):
                q["data"] = "dl:list:0"
            dialog_callback(q)
        tg("answerCallbackQuery", {"callback_query_id": q["id"]})
        return
    if q and str(q.get("data", "")).startswith("c:"):
        return client_callback(q)
    if q and str(q.get("data", "")).startswith("up:"):  # переключение периода в списке записей
        k = q["data"][3:]
        if k in PERIODS and str(q["message"]["chat"]["id"]) == str(cfg["CHAT_ID"]):
            tg("editMessageText", {"chat_id": cfg["CHAT_ID"], "message_id": q["message"]["message_id"], "text": upcoming_text(k),
                                   "parse_mode": "HTML", "reply_markup": period_keyboard(k)})
        tg("answerCallbackQuery", {"callback_query_id": q["id"]})
        return
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
                         "Если захотите выбрать другое время — напишите сюда.", menu_kb())
        if l and st == "came" and prev and prev["status"] != "came":
            ok = to_client(l, f"Спасибо, что были в салоне «{cfg['SALON_NAME']}»! Нам очень важно ваше мнение — "
                              "оставьте, пожалуйста, отзыв в Яндекс Картах, это пара минут.",
                           {"inline_keyboard": [[{"text": "Оставить отзыв", "url": reviews_url()}], [cb("Меню", "c:menu")]]})
            log("Просьба об отзыве", l["name"], "→", "отправлена" if ok else "не отправлена (клиент не подключал бота)")
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
        return client_start(chat, text.split(maxsplit=1)[1].strip(), (m.get("from") or {}).get("first_name", ""),
                            (m.get("from") or {}).get("username", ""))
    if text.split("@")[0] == "/my":
        l = active_lead(chat)
        return tg("sendMessage", {"chat_id": chat, "text": my_text(l), "reply_markup": appt_kb(l) if l else menu_kb()})
    if text.startswith("/admin"):
        if text.split(maxsplit=1)[-1].strip() == cfg["REPORT_KEY"]:
            cfg["CHAT_ID"] = str(chat)
            save_config()
            tg("sendMessage", {"chat_id": chat, "reply_markup": ADMIN_KB,
                               "text": "Готово: заявки с сайта теперь будут приходить в этот чат. Кнопки внизу — записи, вопросы клиентов и сводка."})
            log("Администратор подключён, chat_id", chat)
            set_commands()
            threading.Thread(target=resend_pending, daemon=True).start()
        else:
            tg("sendMessage", {"chat_id": chat, "text": "Неверный пароль. Отправьте: /admin ПАРОЛЬ (REPORT_KEY из config.json)."})
    elif text.startswith("/start"):
        if is_admin:
            tg("sendMessage", {"chat_id": chat, "reply_markup": ADMIN_KB,
                               "text": "Вы администратор: сюда приходят заявки и вопросы клиентов.\n\n"
                                       "Кнопки внизу: «Записи» — расписание, «Диалоги» — переписка с клиентами, «Сводка» — статистика.\n"
                                       "После «Подтвердил запись» бот спросит время — ответьте на его сообщение, и клиент получит подтверждение."})
        else:
            client_hello(chat)
    elif text.startswith("/update") and is_admin:
        threading.Thread(target=check_update, args=(True,), daemon=True).start()
    elif is_admin and (text.split("@")[0] in ("/dialogs", "/questions") or text in ("Диалоги", "Вопросы от клиентов")):
        qt, kb = dialogs_view()
        tg("sendMessage", {"chat_id": chat, "text": qt, "parse_mode": "HTML", "reply_markup": kb})
    elif is_admin and text == "Записи":
        send_upcoming(chat, "all")
    elif is_admin and text == "Сводка":
        tg("sendMessage", {"chat_id": chat, "text": report_text(), "parse_mode": "HTML", "disable_web_page_preview": True})
    elif is_admin and text.split("@")[0] in ("/today", "/tomorrow", "/week", "/upcoming"):
        send_upcoming(chat, {"/today": "today", "/tomorrow": "tomorrow", "/week": "week"}.get(text.split("@")[0], "all"))
    elif text.startswith("/report") and is_admin:
        tg("sendMessage", {"chat_id": chat, "text": report_text(), "parse_mode": "HTML", "disable_web_page_preview": True})
    elif not text.startswith("/"):
        if not is_admin or client_leads(chat):  # админ, проверяющий запись на себе, тоже пишет «как клиент»
            client_message(chat, text[:1000], m)
        else:
            tg("sendMessage", {"chat_id": chat, "reply_markup": ADMIN_KB,
                               "text": "Чтобы ответить клиенту, нажмите кнопку «Ответить» под его сообщением "
                                       "или откройте «Диалоги»."})


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
    ok = lead["name"] and len(re.sub(r"\D", "", lead["phone"])) >= 10 and services and b.get("consent") is True
    return lead if ok else None


def save_lead(lead):
    i = datetime.now().strftime("%y%m%d") + secrets.token_hex(3)
    row = dict(lead, id=i, tok=secrets.token_urlsafe(12), created_at=datetime.now(timezone.utc).isoformat(),
               services=json.dumps(lead["services"], ensure_ascii=False))
    with db_lock, db() as con:
        con.execute("INSERT INTO leads(id,tok,created_at,name,phone,services,total,total_from,day,win,contact,handle,comment) "
                    "VALUES(:id,:tok,:created_at,:name,:phone,:services,:total,:total_from,:day,:win,:contact,:handle,:comment)", row)
    linked = autolink(i, lead["phone"])
    ok = send_lead(get_lead(i))
    if linked:
        client_start(linked, row["tok"], "", auto=True)
    return i, ok, row["tok"], bool(linked)


def phone_key(p):
    d = re.sub(r"\D", "", p or "")
    return "7" + d[1:] if len(d) == 11 and d[0] == "8" else d


def autolink(i, phone):
    """Клиент уже подключал бота раньше (тот же телефон) — привязываем новую заявку к его чату сразу."""
    key = phone_key(phone)
    if len(key) < 10:
        return None
    with db_lock, db() as con:
        for r in con.execute("SELECT phone, client_chat, tg_username FROM leads WHERE client_chat IS NOT NULL AND id<>? "
                             "ORDER BY created_at DESC", (i,)).fetchall():
            if phone_key(r["phone"]) == key:
                con.execute("UPDATE leads SET client_chat=?, tg_username=? WHERE id=?", (r["client_chat"], r["tg_username"], i))
                log("Заявка", i, "привязана к боту клиента по номеру телефона")
                return r["client_chat"]
    return None


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
        try:
            resend_pending()
        except Exception as e:
            log("Ошибка досылки заявок:", e)


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


def operator_line():
    miss = '<mark>не заполнено — впишите в config.json</mark>'
    f = lambda k: esc(cfg.get(k)) or miss
    return (f"{f('OPERATOR_NAME')}, ИНН {f('OPERATOR_INN')}, ОГРН/ОГРНИП {f('OPERATOR_OGRN')}, адрес: {f('OPERATOR_ADDRESS')}, "
            f"e-mail: {f('OPERATOR_EMAIL')}")


def legal_html(kind):
    salon, keep = esc(cfg["SALON_NAME"]), int(cfg.get("DATA_KEEP_DAYS") or 365)
    data = ("имя; номер телефона; выбранный мессенджер (Telegram или MAX) и, если вы его указали, ник или ссылка на профиль; "
            "идентификатор чата Telegram — если вы подключили бота салона; выбранные услуги, желаемые день и время; комментарий к заявке")
    purposes = ("запись на услуги салона; связь с вами для подтверждения записи; отправка подтверждения, напоминаний о визите "
                "и ответов на ваши сообщения; учёт заявок салоном")
    if kind == "consent":
        title = "Согласие на обработку персональных данных"
        body = f"""<p>Я, отправляя заявку на сайте салона «{salon}», свободно, своей волей и в своём интересе даю согласие оператору:
{operator_line()} — на обработку моих персональных данных на следующих условиях.</p>
<p><b>Цели:</b> {purposes}.</p>
<p><b>Данные:</b> {data}.</p>
<p><b>Действия:</b> сбор, запись, систематизация, хранение, уточнение, использование, передача (предоставление доступа) в пределах целей, удаление, уничтожение. Обработка автоматизированная.</p>
<p><b>Передача:</b> заявка передаётся администратору салона через мессенджер Telegram (Telegram FZ-LLC и связанные лица; серверы могут находиться за пределами РФ — трансграничная передача), а если вы выбрали MAX — через мессенджер MAX. Другим лицам данные не передаются и не продаются.</p>
<p><b>Срок:</b> согласие действует до достижения целей, но не дольше {keep} дней с даты заявки; после этого данные удаляются.</p>
<p><b>Отзыв:</b> согласие можно отозвать в любой момент — написать на {esc(cfg.get('OPERATOR_EMAIL')) or 'e-mail оператора'}
или сообщением боту салона в Telegram. Данные удаляются в течение 10 рабочих дней.</p>"""
    else:
        title = "Политика обработки персональных данных"
        body = f"""<p>Оператор: {operator_line()}.</p>
<p><b>Какие данные собираем и зачем.</b> Только данные из формы записи: {data}. Цели: {purposes}. Данные не используются для рекламных рассылок.</p>
<p><b>Основание.</b> Согласие на обработку персональных данных, которое вы даёте отдельно при отправке заявки (<a href="/consent">текст согласия</a>).</p>
<p><b>Как храним.</b> Заявки хранятся в базе данных на сервере салона. Доступ есть только у администратора салона.
Заявка отправляется администратору через Telegram (или MAX). Срок хранения — не дольше {keep} дней, затем заявка удаляется автоматически.</p>
<p><b>Ваши права.</b> Вы можете узнать, какие ваши данные хранятся, попросить их исправить или удалить и отозвать согласие —
написав на {esc(cfg.get('OPERATOR_EMAIL')) or 'e-mail оператора'} или сообщением боту салона. Ответим в течение 10 рабочих дней.</p>
<p><b>Cookie и аналитика.</b> Сайт записи не использует cookie и счётчики аналитики.</p>"""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — {salon}</title><style>body{{margin:0;background:#0f0f10;color:#ece9e4;font:16px/1.6 -apple-system,Segoe UI,Roboto,Arial,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:28px 16px 60px}} h1{{font-size:24px;line-height:1.3}} a{{color:#c8b08a}} mark{{background:#5a1d1d;color:#fff;padding:0 4px}}
p{{margin:0 0 14px}}</style></head><body><main><h1>{title}</h1>{body}
<p style="color:#8e8a86;font-size:14px;margin-top:28px">Салон «{salon}», {esc(cfg['SALON_ADDRESS'])}. <a href="/">Вернуться к записи</a></p></main></body></html>"""


def purge_loop():
    while True:
        try:
            keep = int(cfg.get("DATA_KEEP_DAYS") or 0)
            if keep > 0:
                since = (datetime.now(timezone.utc) - timedelta(days=keep)).isoformat()
                with db_lock, db() as con:
                    ids = [r[0] for r in con.execute("SELECT id FROM leads WHERE created_at < ?", (since,))]
                    con.execute("DELETE FROM leads WHERE created_at < ?", (since,))
                    con.executemany("DELETE FROM msgmap WHERE lead_id=?", [(i,) for i in ids])
                if ids:
                    log("Удалено старых заявок (срок хранения):", len(ids))
        except Exception as e:
            log("Ошибка очистки:", e)
        time.sleep(6 * 3600)


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
        if u.path in ("/consent", "/privacy"):
            return self.send(200, legal_html(u.path[1:]), "text/html; charset=utf-8")
        if u.path == "/health":
            return self.send(200, "ok")
        self.send(404, "Не найдено")

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/lead":
            return self.send(404, "Не найдено")
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1") and self.headers.get("X-Forwarded-For"):  # за Caddy на VPS — настоящий адрес клиента
            ip = self.headers["X-Forwarded-For"].split(",")[-1].strip()
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
        i, ok, tok, linked = save_lead(lead)
        # Заявка сохранена в любом случае; если Telegram недоступен, она видна на странице /admin.
        self.send(200, json.dumps({"ok": True, "id": i, "telegram": ok, "bot": BOT["username"], "start": tok, "linked": linked}), "application/json")


def main():
    load_config()
    init_db()
    if cfg["BOT_TOKEN"]:
        threading.Thread(target=poll_loop, daemon=True).start()
    else:
        log("BOT_TOKEN пуст: заявки сохраняются, но в Telegram не отправляются.")
    threading.Thread(target=purge_loop, daemon=True).start()
    if not cfg.get("OPERATOR_NAME"):
        log("Внимание: в config.json не заполнены данные оператора (OPERATOR_…) — они нужны для страниц /consent и /privacy.")
    if cfg.get("UPDATE_REPO") and cfg.get("UPDATE_AUTO"):
        threading.Thread(target=update_loop, daemon=True).start()
    if cfg.get("BOT_TOKEN"):
        threading.Thread(target=resend_loop, daemon=True).start()
        threading.Thread(target=reminder_loop, daemon=True).start()
        threading.Thread(target=digest_loop, daemon=True).start()
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
