"""Кликабельный макет карточки салона в стиле приложения карт (для показа владелице).
Запуск после build.py:  python3 maket/build_maket.py  →  dist/euphoria/maket-karta.html"""
import base64, html, io, json, pathlib
from PIL import Image

R = pathlib.Path(__file__).resolve().parent.parent
D = R / "dist/euphoria"
data = json.loads((R / "data.json").read_text(encoding="utf-8"))


def jpg(path, w, q=82):
    im = Image.open(path).convert("RGB")
    im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=q, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


imgs = {"cover": jpg(D / "yandex-business/brand-images/cover-1600x900.png", 900),
        "logo": jpg(D / "yandex-business/brand-images/logo-800x800.png", 240),
        "stories": [jpg(D / "yandex-business/stories" / s["file"], 540) for s in data["stories"]]}
# Фото салона (раздел «Фото») и фото услуг встраиваем в страницу: в артефакте внешние картинки не грузятся
SALON_ORDER = ["salon_5540", "salon_5525", "salon_5543", "salon_5536", "salon_5549", "salon_5521", "salon_5537",
               "salon_5528", "salon_5544", "salon_5524", "salon_5559", "salon_5550", "salon_5538"]
imgs["salon"] = [jpg(R / "photos/salon" / (n + ".jpg"), 640, 80) for n in SALON_ORDER]
imgs["svc"] = {}
services = []
for sv in data["services"]:
    sv = dict(sv)
    name = sv["photo"].rsplit("/", 1)[-1].removesuffix(".jpg") if sv.get("photo", "").startswith("http") else ""
    if name and (R / "photos/services" / (name + ".jpg")).exists():
        if name not in imgs["svc"]:
            imgs["svc"][name] = jpg(R / "photos/services" / (name + ".jpg"), 360, 78)
        sv["photo"] = "svc:" + name
    services.append(sv)
booking = (D / "server/public/index.html").read_text(encoding="utf-8")
tpl = (R / "maket/maket_template.html").read_text(encoding="utf-8")
payload = json.dumps({"salon": data["salon"], "services": services, "stories": data["stories"], "features": data["features"],
                      "reviews": data["reviews"], "offer": data["offer"], "imgs": imgs}, ensure_ascii=False).replace("</", "<\\/")
out = tpl.replace("/*__DATA__*/", payload).replace("__BOOKING__", html.escape(booking, quote=True))
(D / "maket-karta.html").write_text(out, encoding="utf-8")
print("Готово:", D / "maket-karta.html", round(len(out) / 1024), "КБ")
