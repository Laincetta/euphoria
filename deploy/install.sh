#!/usr/bin/env bash
# Установка сервера заявок «Эйфория» на VPS (Ubuntu 22.04 / 24.04 / Debian 12).
#
#   curl -fsSL https://raw.githubusercontent.com/Laincetta/euphoria/main/deploy/install.sh | sudo bash -s -- ДОМЕН
#
# Пример: ... | sudo bash -s -- euphoria-reutov.ru
# Перед запуском домен должен указывать на IP этого сервера (A-запись у регистратора).
# Повторный запуск безопасен: config.json и заявки (leads.db) не трогаются.
set -euo pipefail

DOMAIN="${1:-}"
REPO="Laincetta/euphoria"
BRANCH="main"
DIR="/opt/euphoria"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH/server"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo"; exit 1; }
[ -n "$DOMAIN" ] || { echo "Укажите домен: ... | sudo bash -s -- euphoria-reutov.ru"; exit 1; }

say "1/6 Пакеты: Python, curl, Caddy (HTTPS)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 curl ca-certificates debian-keyring debian-archive-keyring apt-transport-https gnupg
if ! command -v caddy >/dev/null; then
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi

say "2/6 Пользователь и папка $DIR"
id euphoria >/dev/null 2>&1 || useradd --system --home "$DIR" --shell /usr/sbin/nologin euphoria
mkdir -p "$DIR/public"

say "3/6 Файлы сервера из GitHub"
curl -fsSL "$RAW/server.py" -o "$DIR/server.py.new" && python3 -m py_compile "$DIR/server.py.new" && mv "$DIR/server.py.new" "$DIR/server.py"
curl -fsSL "$RAW/public/index.html" -o "$DIR/public/index.html"
curl -fsSL "$RAW/manifest.json" | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])" > "$DIR/VERSION"

say "4/6 Настройки (config.json)"
if [ ! -f "$DIR/config.json" ]; then
  KEY="euph_$(python3 -c 'import secrets; print(secrets.token_urlsafe(9).replace("-", "x"))')"
  read -r -s -p "Токен бота от @BotFather (символы не отображаются): " TOKEN </dev/tty || TOKEN=""; echo
  python3 - "$DIR/config.json" "$TOKEN" "$KEY" "$DOMAIN" <<'PY'
import json, sys
path, token, key, domain = sys.argv[1:]
json.dump({"BOT_TOKEN": token.strip(), "REPORT_KEY": key, "CHAT_ID": "", "HOST": "127.0.0.1", "PORT": 8080,
           "SITE_URL": f"https://{domain}/"}, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
  echo "Пароль администратора: $KEY  (сохраните; админ пишет боту: /admin $KEY)"
else
  echo "config.json уже есть — оставляю как есть"
fi
chown -R euphoria:euphoria "$DIR"
chmod 600 "$DIR/config.json"

say "5/6 Служба systemd (автозапуск, перезапуск после обновлений)"
cat > /etc/systemd/system/euphoria.service <<UNIT
[Unit]
Description=Euphoria leads server (страница записи + Telegram-бот)
After=network-online.target
Wants=network-online.target

[Service]
User=euphoria
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/server.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$DIR

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now euphoria
systemctl restart euphoria

say "6/6 HTTPS для $DOMAIN (Caddy сам получит сертификат)"
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN, www.$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:8080
}
CADDY
systemctl reload caddy || systemctl restart caddy
if command -v ufw >/dev/null && ufw status | grep -q active; then ufw allow 80/tcp; ufw allow 443/tcp; fi

sleep 3
say "Готово"
systemctl --no-pager --lines=5 status euphoria | sed -n '1,12p' || true
echo
echo "Сайт:   https://$DOMAIN/   (сертификат появляется в течение минуты)"
echo "Заявки: https://$DOMAIN/admin?key=ПАРОЛЬ"
echo "Логи:   journalctl -u euphoria -f"
echo "Если бот не подключён — проверьте BOT_TOKEN:  nano $DIR/config.json  и  systemctl restart euphoria"
