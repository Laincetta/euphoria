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
booking = (D / "server/public/index.html").read_text(encoding="utf-8")
tpl = (R / "maket/maket_template.html").read_text(encoding="utf-8")
payload = json.dumps({"salon": data["salon"], "services": data["services"], "stories": data["stories"], "features": data["features"],
                      "reviews": data["reviews"], "offer": data["offer"], "imgs": imgs}, ensure_ascii=False).replace("</", "<\\/")
out = tpl.replace("/*__DATA__*/", payload).replace("__BOOKING__", html.escape(booking, quote=True))
(D / "maket-karta.html").write_text(out, encoding="utf-8")
print("Готово:", D / "maket-karta.html", round(len(out) / 1024), "КБ")
