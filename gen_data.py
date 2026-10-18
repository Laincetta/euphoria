"""Генерирует data.json из реального прайса салона (фото прайс-листов от 18.10.2026)."""
import json
import pathlib

ROOT = pathlib.Path(__file__).parent

P = {  # фото Unsplash (бесплатные, для примера; заменить на фото салона)
    "interior": "1600948836101-f9ffda59d250", "interior2": "1633681926022-84c23e8cb2d6",
    "laser": "1700760933574-9f0f4ea9aa3b", "laser2": "1700760933941-3a06a28fbf47",
    "lash": "1589710751893-f9a6770ad71b", "lash2": "1639629509821-c54cdd984227",
    "brow": "1674049406179-d7bf2c263e71", "brow2": "1790244342917-b08f24662dde",
    "nails": "1604654894610-df63bc536371", "nails2": "1632345031435-8727f6897d53",
    "pedi": "1537094082840-dcbbfa99385f", "haircut": "1634449571010-02389ed0f9b0",
    "blow": "1580618672591-eb180b1a973f", "color": "1595475884562-073c30d45670",
    "men": "1605497788044-5a32c7078486", "peel": "1713085085470-fba013d67e65",
}

services = []


def rub(n):
    return f"{n:,}".replace(",", " ")


def add(cat, key, sid, name, price, text=None, frm=False, photo=None, short="", desc="", popular=False, badge="", kind=""):
    services.append({"id": sid, "cat": cat, "catKey": key, "name": name, "price": price,
                     "priceText": text, "priceFrom": frm, "photo": photo or "", "short": short,
                     "description": desc or f"{name}. Стоимость {text or f'{price:,} ₽'.replace(',', ' ')}.",
                     "popular": popular, "badge": badge, "badgeKind": kind})


LZ = [("lip", "Верхняя губа", 500), ("chin", "Подбородок", 1000), ("forehead", "Лоб", 1000), ("face", "Лицо", 1500),
      ("neck", "Шея передняя/задняя", 1500), ("areola", "Ореолы", 500), ("back", "Спина", 1000), ("belly", "Живот", 1000),
      ("arms", "Руки полностью", 2000), ("forearms", "Руки до локтя", 1000), ("legs", "Ноги полностью", 5000),
      ("shins", "Ноги до колен", 2500), ("fingers", "Фаланги пальцев", 500), ("armpits", "Подмышки", 1000),
      ("bikini-t", "Тотальное бикини", 2500), ("bikini-c", "Классическое бикини", 1500)]
zone = {k: (n, p) for k, n, p in LZ}
for k, n, p in LZ:
    add("Лазерная эпиляция", "laser", "lz-" + k, f"Лазерная эпиляция: {n.lower()}", p, photo=P["laser"] if k in ("legs", "shins") else P["laser2"],
        short="Одна зона", desc=f"Лазерная эпиляция, зона «{n.lower()}». Курс процедур для долгого результата.",
        popular=k in ("armpits", "bikini-t"))

COMBO = [("ab", ["armpits", "bikini-t"], 3000), ("fab", ["face", "armpits", "bikini-t"], 3500),
         ("sab", ["shins", "armpits", "bikini-t"], 4000), ("full", ["legs", "arms", "bikini-t", "armpits"], 7000),
         ("cfs", ["bikini-c", "shins", "forearms", "armpits"], 5000)]
for k, zs, p in COMBO:
    names = [zone[z][0].lower() for z in zs]
    sep = sum(zone[z][1] for z in zs)
    title = " + ".join(n.capitalize() if i == 0 else n for i, n in enumerate(names))
    save = sep - p
    add("Комбо эпиляции", "combo", "cb-" + k, f"Комбо: {title}", p, photo=P["laser"],
        short=f"Отдельно {rub(sep)} ₽, выгода {rub(save)} ₽",
        desc=f"Комбо лазерной эпиляции: {', '.join(names)}. По отдельности {rub(sep)} ₽, в комбо {rub(p)} ₽, выгода {rub(save)} ₽.",
        popular=k in ("full", "ab"), badge=f"−{rub(save)} ₽", kind="deal")
    services[-1]["oldPrice"] = sep

for sid, n, p, ph, pop in [("brow-art", "Художественное оформление бровей", 1500, P["brow"], True),
                           ("brow-fix", "Коррекция бровей (пинцет или нитка)", 750, P["brow2"], False),
                           ("brow-color", "Окрашивание бровей", 750, P["brow"], False),
                           ("tattoo-off", "Лазерное удаление старого татуажа (брови, глаза, губы)", 2500, P["laser2"], False),
                           ("carbon", "Карбоновый пилинг", 3000, P["peel"], False),
                           ("ears", "Прокалывание ушей", 2500, None, False)]:
    add("Брови и лицо", "brows", sid, n, p, photo=ph, popular=pop)

for i, (v, p) in enumerate([("классика", 3300), ("1,5D", 3400), ("2D", 3600), ("2,5D", 3800), ("3D", 4100), ("4D", 4500)]):
    add("Наращивание ресниц", "lashes", f"lash-{i}", f"Наращивание ресниц: {v}", p, photo=P["lash"] if i % 2 == 0 else P["lash2"],
        popular=i == 0, short="Объём " + v)

for sid, n, p, t, pop in [("mani", "Маникюр аппаратный или обрезной", 1000, None, False),
                          ("mani-shellac", "Маникюр + шеллак", 2500, None, True),
                          ("mani-gel", "Маникюр + укрепление гелем", 3000, None, False),
                          ("mani-french", "Гель + френч", 3500, None, False),
                          ("mani-ext", "Наращивание ногтей", 4500, "4 500 / 5 500 ₽", False),
                          ("mani-off", "Снятие покрытия (руки)", 300, None, False)]:
    add("Маникюр", "nails", sid, n, p, t, frm=bool(t), photo=P["nails"] if pop else P["nails2"], popular=pop,
        short="Цена зависит от длины" if t else "")

for sid, n, p, pop in [("pedi", "Педикюр аппаратный или классический", 2500, False), ("pedi-gel", "Педикюр + гель-лак", 3000, True),
                       ("pedi-hard", "Педикюр + укрепление гелем", 3500, False), ("pedi-lite", "Педикюр лайт + гель-лак", 2500, False),
                       ("pedi-french", "Френч или омбре (ноги)", 3500, False), ("pedi-off", "Снятие покрытия (ноги)", 300, False)]:
    add("Педикюр", "pedi", sid, n, p, photo=P["pedi"], popular=pop)

for sid, n, p, t, pop in [("hw-short", "Стрижка: короткие волосы", 1000, "1 000 / 1 500 ₽", False),
                          ("hw-mid", "Стрижка: средняя длина", 1500, "1 500 / 2 000 ₽", True),
                          ("hw-long", "Стрижка: длинные волосы", 2500, "2 500 / 3 500 ₽", False),
                          ("hw-cut", "Стрижка: один срез", 1000, None, False), ("hw-bangs", "Стрижка чёлки", 500, None, False),
                          ("hw-hot", "Стрижка горячими ножницами", 2000, None, False), ("hw-cascade", "Авторский каскад", 3500, None, False),
                          ("hw-bob", "Стрижка: каре", 2500, None, False)]:
    add("Женский зал", "hair", sid, n, p, t, frm=bool(t), photo=P["haircut"], popular=pop, short="Точную цену назовёт администратор" if t else "")

for sid, n, p, t in [("hm-one", "Мужская стрижка: одна насадка", 300, None), ("hm-two", "Мужская стрижка: две насадки", 500, None),
                     ("hm-model", "Модельная стрижка", 800, "800 / 1 200 ₽"), ("hm-kid", "Детская модельная стрижка", 500, "500 / 1 000 ₽"),
                     ("hm-wash", "Мытьё головы (мужской зал)", 200, None)]:
    add("Мужской зал", "men", sid, n, p, t, frm=bool(t), photo=P["men"])

for sid, n, s, l, sp in [("c-complex", "Сложные техники: шатуш, AirTouch, балаяж, омбре, baby blonde", "13 000", "16 000", 13000),
                         ("c-micro", "Микромелирование", "8 000", "12 000", 8000), ("c-black", "Выход из чёрного", "5 000 / 7 000", "10 000", 5000),
                         ("c-total", "Total blonde", "9 000", "15 000", 9000), ("c-dark", "Затемнение блонда", "8 000", "10 000", 8000),
                         ("c-high", "Мелирование", "3 000 / 3 500", "5 500", 3000),
                         ("c-mix", "Мелирование + окрашивание + тонирование", "7 000", "8 000", 7000),
                         ("c-roots", "Коррекция отросших корней до 2 см", "2 500", "2 500", 2500),
                         ("c-one", "Окрашивание в один тон", "3 000 / 3 500", "4 000", 3000)]:
    add("Окрашивание", "color", sid, n, sp, (f"{s} ₽" if s == l else f"{s} / {l} ₽"), frm=s != l, photo=P["color"],
        short="Короткие / длинные" if s != l else "", popular=sid == "c-high",
        desc=f"{n}. Короткие волосы (до плеч): {s} ₽, длинные (ниже лопаток): {l} ₽.")

for sid, n, t, sp, frm in [("care-keratin", "Нанопластика, ботокс, кератин", "от 7 000 ₽", 7000, True),
                           ("care-shield", "Экранирование волос", "2 500 / 3 500 / 4 500 ₽", 2500, True),
                           ("care-peel", "Пилинг кожи головы", "от 3 000 ₽", 3000, True),
                           ("care-wash", "Мытьё головы (женский зал)", "500 ₽", 500, False)]:
    add("Уход за волосами", "care", sid, n, sp, t, frm=frm, photo=P["blow"],
        desc={"care-keratin": "Нанопластика, ботокс или кератин. Короткие волосы от 7 000 / 10 000 ₽, длинные 10 000 / 15 000 ₽.",
              "care-shield": "Экранирование волос: короткие 2 500 / 3 500 ₽, длинные 4 500 ₽."}.get(sid, ""))

for sid, n, t, sp, frm in [("st-brush", "Локоны на брашинг", "2 500 ₽", 2500, False), ("st-style", "Укладка", "1 000 / 1 500 / 2 000 ₽", 1000, True),
                           ("st-afro", "Афро-кудри", "2 500 / 3 500 ₽", 2500, True), ("st-wedding", "Свадебная причёска", "от 5 000 ₽", 5000, True)]:
    add("Укладки", "styling", sid, n, sp, t, frm=frm, photo=P["blow"])

for s in services:
    if s["priceText"] is None:
        s["priceText"] = rub(s["price"]) + " ₽"

data = {
    "_readme": "Цены — реальный прайс салона (фото от 18.10.2026). ПРИМЕРНЫЕ: телефон, фото (Unsplash), отзывы. Меняйте здесь и запускайте build.py.",
    "salon": {
        "name": "Эйфория", "brandTop": "Beauty Salon", "brand": "EUPHORIA",
        "tagline": "Лазерная эпиляция · ресницы и брови · ногти · волосы",
        "categories": "Салон красоты · Лазерная эпиляция · Парикмахерская",
        "rating": "4,5", "ratingsCount": "162 оценки", "reviewsCount": 95, "photosCount": 90,
        "address": "Московская область, Реутов, Парковая улица, 8", "addressShort": "Реутов, Парковая улица, 8", "floor": "этаж 1",
        "open": 10, "close": 21, "phone": "+7 (965) 340-82-87", "telegram": "", "site": "", "siteLabel": "",
        "metro": {"name": "Новокосино", "dist": "2,08 км"}, "stop": {"name": "Проспект Мира, 23", "dist": "320 м"},
        "description": "Салон красоты «Эйфория» в Реутове: лазерная эпиляция и выгодные комбо, наращивание ресниц, оформление бровей, маникюр и педикюр, в том числе в 4 руки, стрижки, окрашивание и уход за волосами. Ежедневно с 10:00 до 21:00. Запись онлайн, 320 м от остановки «Проспект Мира, 23»."
    },
    "photos": {
        "hero": [{"id": P["interior"], "caption": "Наш зал"}, {"id": P["laser"], "caption": "Лазерная эпиляция"},
                 {"id": P["lash"], "caption": "Наращивание ресниц"}, {"id": P["nails"], "caption": "Маникюр и педикюр"},
                 {"id": P["haircut"], "caption": "Стрижки и окрашивание"}],
        "gallery": [{"id": P["interior"], "label": "Зал"}, {"id": P["laser"], "label": "Лазер"}, {"id": P["lash2"], "label": "Ресницы"},
                    {"id": P["brow"], "label": "Брови"}, {"id": P["nails"], "label": "Маникюр"}, {"id": P["pedi"], "label": "Педикюр"},
                    {"id": P["haircut"], "label": "Стрижки"}, {"id": P["color"], "label": "Окрашивание"}, {"id": P["interior2"], "label": "Интерьер"}]
    },
    "offer": {"badge": "Комбо эпиляции", "title": "Ноги, руки, бикини и подмышки за 7 000 ₽", "text": "Тело целиком за один визит. По отдельности вышло бы 10 500 ₽.",
              "price": 7000, "oldPrice": 10500, "serviceId": "cb-full", "photo": P["laser2"],
              "promoLine": "Комбо лазерной эпиляции: выгода до 3 500 ₽", "heroBadge": "Выгода до 3 500 ₽ на комбо"},
    "services": services,
    "crossSell": {"laser": "cb-ab", "combo": "lz-forearms", "lashes": "brow-color", "brows": "lash-0", "nails": "pedi-gel",
                  "pedi": "mani-shellac", "hair": "st-style", "men": "hm-wash", "color": "care-shield", "care": "st-style", "styling": "care-shield"},
    "reviews": [{"name": "Екатерина", "stars": 5, "date": "12 октября", "text": "Хожу на лазер третий месяц, результат уже заметен. Девочки аккуратные, всегда без задержек."},
                {"name": "Мария", "stars": 5, "date": "3 октября", "text": "Сделала маникюр и педикюр одновременно в 4 руки, уложились в полтора часа."},
                {"name": "Дмитрий", "stars": 4, "date": "27 сентября", "text": "Стригусь здесь давно: быстро, недорого, рядом с домом."}],
    "stories": [
        {"file": "01-combo.png", "cover": "Тело целиком 7 000 ₽", "caption": "Комбо", "kicker": "Комбо эпиляции", "title": "Тело целиком за 7 000 ₽",
         "text": "Ноги и руки полностью, тотальное бикини и подмышки. По отдельности вышло бы 10 500 ₽.", "cta": "Записаться на комбо", "serviceId": "cb-full", "photo": P["laser"],
         "lines": [["подмышки + тотальное бикини", "3 000 ₽"], ["лицо + подмышки + бикини", "3 500 ₽"], ["ноги до колен + подмышки + бикини", "4 000 ₽"], ["тело целиком", "7 000 ₽"]]},
        {"file": "02-lashes.png", "cover": "Ресницы от 3 300 ₽", "caption": "Ресницы", "kicker": "Наращивание ресниц", "title": "Ресницы от 3 300 ₽",
         "text": "Классика, 1,5D, 2D, 2,5D, 3D и 4D. Объём подбираем под форму глаз.", "cta": "Выбрать объём", "serviceId": "lash-0", "photo": P["lash"],
         "lines": [["классика", "3 300 ₽"], ["1,5D", "3 400 ₽"], ["2D", "3 600 ₽"], ["2,5D", "3 800 ₽"], ["3D", "4 100 ₽"], ["4D", "4 500 ₽"]]},
        {"file": "03-four-hands.png", "cover": "Маникюр и педикюр в 4 руки", "caption": "В 4 руки", "kicker": "Экономим ваше время", "title": "Маникюр и педикюр одновременно",
         "text": "Два мастера работают в 4 руки. Маникюр от 1 000 ₽, педикюр от 2 500 ₽.", "cta": "Записаться", "serviceId": "mani-shellac", "photo": P["nails"],
         "lines": [["маникюр", "1 000 ₽"], ["маникюр + шеллак", "2 500 ₽"], ["педикюр", "2 500 ₽"], ["педикюр + гель-лак", "3 000 ₽"]]},
        {"file": "04-brows.png", "cover": "Брови 1 500 ₽", "caption": "Брови", "kicker": "Оформление бровей", "title": "Брови за 1 500 ₽",
         "text": "Художественное оформление. Коррекция пинцетом или ниткой 750 ₽, окрашивание 750 ₽.", "cta": "Записаться", "serviceId": "brow-art", "photo": P["brow"],
         "lines": [["художественное оформление", "1 500 ₽"], ["коррекция пинцет / нитка", "750 ₽"], ["окрашивание бровей", "750 ₽"]]}],
    "features": [["Лазерная эпиляция и комбо", "есть"], ["Наращивание ресниц", "есть"], ["Оформление бровей", "есть"], ["Маникюр и педикюр", "есть"],
                 ["Работа в 4 руки", "есть"], ["Женский и мужской зал", "есть"], ["Окрашивание и уход за волосами", "есть"],
                 ["Прокалывание ушей", "есть"], ["Онлайн-запись", "есть"]]
}
(ROOT / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
print(len(services), "услуг;", sum(s["popular"] for s in services), "популярных")
