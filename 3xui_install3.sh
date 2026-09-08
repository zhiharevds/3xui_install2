#!/bin/bash
###############################################################################
# 3xui_install3 — установка VPN-сервера на ЧИСТОЙ Ubuntu 22/24
#
# Схема Р-22 (2026-09-08): БЕЗ Caddy. Панель сама держит защищённое соединение —
# сертификат на голый IP и его продление делают штатные средства 3x-ui.
#
# Что делает официальный установщик (мы только передаём ему переменные):
#   панель, сертификат Let's Encrypt на IP, прописывание его панели,
#   автопродление с перезапуском панели, fail2ban.
# Что дописываем мы (в официальных средствах этого нет):
#   отключение IPv6 и пинга, файрвол, шаблон маршрутизации, подписка по TLS,
#   WARP, подключения REALITY/XHTTP с «мин. версией клиента» 0.0.0.
#
# Запуск (одна команда; unattended-upgrades скрипт останавливает сам, шаг 1):
#   bash <(curl -Ls https://raw.githubusercontent.com/zhiharevds/3xui_install2/main/3xui_install3.sh)
###############################################################################

set -uo pipefail

red='\033[0;31m'; green='\033[0;32m'; yellow='\033[0;33m'; plain='\033[0m'
step() { echo -e "\n${yellow}=== $* ===${plain}"; }
ok()   { echo -e "  ${green}✓${plain} $*"; }
bad()  { echo -e "  ${red}✗${plain} $*"; }

###############################################################################
# НАСТРОЙКИ — можно переопределить переменными окружения перед запуском
###############################################################################
SUB_PORT=${SUB_PORT:-2096}            # порт подписок
PORT_REALITY=${PORT_REALITY:-443}     # подключение REALITY (tcp+vision)
PORT_XHTTP=${PORT_XHTTP:-8080}        # подключение XHTTP  (его ТСПУ не душит)
DO_UPGRADE=${DO_UPGRADE:-1}           # 1 = обновить систему перед установкой
DO_WARP=${DO_WARP:-1}                 # 1 = поставить Cloudflare WARP
CREATE_INBOUNDS=${CREATE_INBOUNDS:-1} # 1 = создать подключения автоматически
CLIENTS=${CLIENTS:-"Keenetic belka mama dzh"}

# Выход по умолчанию для трафика, не попавшего ни под одно правило.
#   direct — напрямую с IP сервера (быстро; так на боевых серверах после Р-15)
#   warp   — через Cloudflare WARP (прячет IP сервера, но режет скорость вдвое)
# ВАЖНО: первый исходящий в списке = выход по умолчанию. Не переставлять вслепую —
# именно на этом 2026-09-07 лёг голландский: первой оказалась «чёрная дыра».
DEFAULT_EXIT=${DEFAULT_EXIT:-direct}

###############################################################################
# 0. Защита от запуска на живом сервере
###############################################################################
step "Проверка: сервер чистый?"
if [[ -d /usr/local/x-ui || -d /etc/x-ui ]] && [[ "${FORCE:-0}" != "1" ]]; then
	bad "На сервере уже установлен x-ui."
	echo
	echo "  Этот скрипт — установщик С НУЛЯ. На работающем сервере он сбросит"
	echo "  правила файрвола, пароль панели и настройки Xray."
	echo "  Если это действительно чистая переустановка — запусти с FORCE=1."
	exit 1
fi
[[ $EUID -eq 0 ]] || { bad "Нужны права root"; exit 1; }
ok "чисто, продолжаем"

MAIN_IP=$(curl -s --max-time 10 https://api4.ipify.org || curl -s --max-time 10 https://ipv4.icanhazip.com | tr -d '[:space:]')
[[ "$MAIN_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { bad "не удалось определить внешний IP"; exit 1; }
PANEL_PORT=$(shuf -i 50000-65535 -n1)
PANEL_PATH="/$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 18)/"
PANEL_PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 18)
# Почта для учётки Let's Encrypt. ВАЖНО: домен обязан оканчиваться настоящей зоной.
# Адрес вида admin@1-2-3-4.invalid LE отвергает («invalid public suffix»), и тогда
# не выпускается сертификат, панель остаётся на обычном HTTP, а подписки протухают.
ACME_MAIL="$(head -c 16 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 10)@$(head -c 16 /dev/urandom | base64 | tr -dc 'a-z0-9' | head -c 10).com"
CERT=/root/cert/ip/fullchain.pem
KEY=/root/cert/ip/privkey.pem
ok "IP=${MAIN_IP}  порт панели=${PANEL_PORT}  выход по умолчанию=${DEFAULT_EXIT}"

###############################################################################
# 1. Система
###############################################################################
step "Обновление системы и зависимости"
systemctl stop unattended-upgrades 2>/dev/null
systemctl disable unattended-upgrades 2>/dev/null
while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do echo "  ждём apt..."; sleep 3; done
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
[[ "$DO_UPGRADE" == "1" ]] && apt-get -y -qq upgrade >/dev/null 2>&1 && ok "система обновлена"
apt-get -y -qq install curl tar socat sqlite3 ufw >/dev/null 2>&1
ok "зависимости на месте"

step "Отключаем IPv6 и ответы на пинг"
sed -i '/disable_ipv6/d;/icmp_echo_ignore_all/d' /etc/sysctl.conf
cat >> /etc/sysctl.conf <<'SYSCTL'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
net.ipv4.icmp_echo_ignore_all = 1
SYSCTL
sysctl -p >/dev/null 2>&1
# Настройки применяются ещё раз ПОСЛЕ файрвола — см. раздел ниже, ufw их перебивает.
ok "записано в /etc/sysctl.conf (применим окончательно после файрвола)"

###############################################################################
# 2. Файрвол — ДО установки, чтобы порт 80 был открыт для выпуска сертификата
###############################################################################
step "Файрвол"
ufw --force reset >/dev/null 2>&1
for p in 22/tcp 80/tcp ${PANEL_PORT}/tcp ${SUB_PORT}/tcp ${PORT_REALITY}/tcp ${PORT_XHTTP}/tcp; do
	ufw allow $p >/dev/null 2>&1
done
ufw --force enable >/dev/null 2>&1
ok "открыты: 22 (SSH), 80 (выпуск сертификата), ${PANEL_PORT} (панель), ${SUB_PORT} (подписки), ${PORT_REALITY}, ${PORT_XHTTP}"

# 🔴 ufw держит СВОЙ файл настроек ядра (/etc/ufw/sysctl.conf, прописан в
# /etc/default/ufw как IPT_SYSCTL) и применяет его при каждом включении,
# перебивая /etc/sysctl.conf. Там строкой net/ipv4/icmp_echo_ignore_all=0
# ответы на пинг включаются обратно. Поэтому правим именно его.
if grep -q "^net/ipv4/icmp_echo_ignore_all=" /etc/ufw/sysctl.conf 2>/dev/null; then
	sed -i 's|^net/ipv4/icmp_echo_ignore_all=.*|net/ipv4/icmp_echo_ignore_all=1|' /etc/ufw/sysctl.conf
else
	echo "net/ipv4/icmp_echo_ignore_all=1" >> /etc/ufw/sysctl.conf
fi
ufw reload >/dev/null 2>&1
sysctl -p >/dev/null 2>&1
# Отчитываемся тем, что РЕАЛЬНО в ядре, а не тем, что записали в файл.
PING_OFF=$(sysctl -n net.ipv4.icmp_echo_ignore_all 2>/dev/null)
IPV6_OFF=$(sysctl -n net.ipv6.conf.all.disable_ipv6 2>/dev/null)
[[ "$PING_OFF" == "1" ]] && ok "сервер не отвечает на пинг" || bad "пинг всё ещё включён (в ядре $PING_OFF)"
[[ "$IPV6_OFF" == "1" ]] && ok "IPv6 выключен" || bad "IPv6 всё ещё включён (в ядре $IPV6_OFF)"

###############################################################################
# 3. Официальный установщик 3x-ui — он же выпускает сертификат на IP
###############################################################################
step "Установка 3x-ui (официальный установщик, безлюдный режим)"
export XUI_NONINTERACTIVE=1
export XUI_USERNAME=admin
export XUI_PASSWORD="${PANEL_PASS}"
export XUI_PANEL_PORT="${PANEL_PORT}"
export XUI_WEB_BASE_PATH="${PANEL_PATH}"
export XUI_DB_TYPE=sqlite
export XUI_SSL_MODE=ip                 # сертификат Let's Encrypt на голый IP
export XUI_ACME_HTTP_PORT=80
export XUI_ACME_EMAIL="${ACME_MAIL}"
export XUI_SERVER_IP="${MAIN_IP}"
export XUI_ENABLE_FAIL2BAN=true
# Полный вывод установщика — в отдельный файл: если что-то пойдёт не так,
# без него причину не найти (проверено на себе).
bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) </dev/null \
	> /root/xui-install-raw.log 2>&1
tail -5 /root/xui-install-raw.log
echo "  (полный вывод установщика: /root/xui-install-raw.log)"

command -v x-ui >/dev/null || { bad "установка панели не удалась"; exit 1; }
sleep 3

# Установщик мог выбрать свои значения — забираем фактические
PANEL_PORT=$(/usr/local/x-ui/x-ui setting -show true 2>/dev/null | grep -Eo 'port: .+' | awk '{print $2}')
PANEL_PATH=$(/usr/local/x-ui/x-ui setting -show true 2>/dev/null | grep -Eo 'webBasePath: .+' | awk '{print $2}')
ok "панель установлена: порт ${PANEL_PORT}, путь ${PANEL_PATH}"

step "Сертификат Let's Encrypt на IP"
if [[ ! -f "$CERT" || ! -f "$KEY" ]]; then
	# Установщик мог не справиться — выпускаем сами, тем же официальным рецептом.
	echo "  установщик сертификат не выпустил, делаем сами"
	[[ -x /root/.acme.sh/acme.sh ]] || curl -s https://get.acme.sh | sh -s email="${ACME_MAIL}" >/dev/null 2>&1
	/root/.acme.sh/acme.sh --register-account -m "${ACME_MAIL}" --server letsencrypt >/dev/null 2>&1
	/root/.acme.sh/acme.sh --set-default-ca --server letsencrypt --force >/dev/null 2>&1
	/root/.acme.sh/acme.sh --issue -d "${MAIN_IP}" --standalone --server letsencrypt \
		--certificate-profile shortlived --days 6 --httpport 80 --force 2>&1 | tail -3
	mkdir -p /root/cert/ip
	# --reloadcmd обязателен: без перезапуска панель держит старый сертификат
	# в памяти и подписки перестают качаться.
	/root/.acme.sh/acme.sh --installcert --force -d "${MAIN_IP}" \
		--key-file "$KEY" --fullchain-file "$CERT" \
		--reloadcmd "systemctl restart x-ui" 2>&1 | tail -2 || true
	/root/.acme.sh/acme.sh --upgrade --auto-upgrade >/dev/null 2>&1
	chmod 600 "$KEY" 2>/dev/null; chmod 644 "$CERT" 2>/dev/null
fi

if [[ -f "$CERT" && -f "$KEY" ]]; then
	ok "сертификат действует до: $(openssl x509 -in "$CERT" -noout -enddate | cut -d= -f2)"
	/usr/local/x-ui/x-ui cert -webCert "$CERT" -webCertKey "$KEY" >/dev/null 2>&1
	ok "прописан панели"
	PANEL_SCHEME=https
else
	bad "сертификат так и не выпустился — панель останется на обычном HTTP"
	bad "смотри /root/xui-install-raw.log, потом: x-ui → пункт 20 → 6"
	PANEL_SCHEME=http
fi

# Файрвол мог быть открыт установщиком на другой порт панели
ufw allow ${PANEL_PORT}/tcp >/dev/null 2>&1

###############################################################################
# 4. Подписки: включаем и отдаём по TLS тем же сертификатом
###############################################################################
step "Подписки"
DB=/etc/x-ui/x-ui.db
set_opt() {
	sqlite3 "$DB" "INSERT INTO settings(key,value) SELECT '$1','' WHERE NOT EXISTS(SELECT 1 FROM settings WHERE key='$1');
	               UPDATE settings SET value='$2' WHERE key='$1';"
}
set_opt subEnable true
set_opt subPort "${SUB_PORT}"
set_opt subPath /sub/
set_opt subCertFile "$CERT"
set_opt subKeyFile "$KEY"
ok "подписки на порту ${SUB_PORT}, по защищённому соединению"

###############################################################################
# 5. Шаблон маршрутизации Xray
###############################################################################
step "Шаблон маршрутизации"
# Первый исходящий = выход по умолчанию. Порядок задаётся DEFAULT_EXIT.
if [[ "$DEFAULT_EXIT" == "warp" ]]; then
	OUT_ORDER='"warp-cli","direct","blocked"'
else
	OUT_ORDER='"direct","warp-cli","blocked"'
fi
python3 - "$OUT_ORDER" <<'PY' > /tmp/xtpl.json
import json, sys
outs = {
 "direct":   {"tag":"direct","protocol":"freedom","settings":{"domainStrategy":"UseIPv4"}},
 "warp-cli": {"tag":"warp-cli","protocol":"socks","settings":{"servers":[{"address":"127.0.0.1","port":40000,"users":[]}]}},
 "blocked":  {"tag":"blocked","protocol":"blackhole","settings":{}},
}
order = [t.strip().strip('"') for t in sys.argv[1].split(",")]
tpl = {
 "log": {"access":"none","dnsLog":False,"error":"","loglevel":"warning","maskAddress":""},
 "api": {"tag":"api","services":["HandlerService","LoggerService","StatsService"]},
 "inbounds": [{"tag":"api","listen":"127.0.0.1","port":62789,"protocol":"tunnel","settings":{"address":"127.0.0.1"}}],
 "outbounds": [outs[t] for t in order],
 "routing": {"domainStrategy":"AsIs","rules":[
   {"type":"field","inboundTag":["api"],"outboundTag":"api"},
   {"type":"field","ip":["geoip:private"],"outboundTag":"blocked"},
   {"type":"field","protocol":["bittorrent"],"outboundTag":"blocked"},
   {"type":"field","ip":["geoip:ru"],"outboundTag":"direct"},
   {"type":"field","domain":["regexp:.*\\.ru$","regexp:.*\\.su$","regexp:.*\\.xn--p1ai$"],"outboundTag":"direct"}
 ]},
 "policy": {"levels":{"0":{"statsUserDownlink":True,"statsUserUplink":True}},
            "system":{"statsInboundDownlink":True,"statsInboundUplink":True}},
 "metrics": {"tag":"metrics_out","listen":"127.0.0.1:11111"},
 "stats": {},
 "dns": {"queryStrategy":"UseIPv4","servers":["https+local://1.1.1.1/dns-query","https+local://8.8.8.8/dns-query"]},
}
print(json.dumps(tpl, ensure_ascii=False, indent=2))
PY
sqlite3 "$DB" "DELETE FROM settings WHERE key='xrayTemplateConfig';
               INSERT INTO settings(key,value) VALUES('xrayTemplateConfig', readfile('/tmp/xtpl.json'));"
ok "выход по умолчанию: ${DEFAULT_EXIT}; российское — напрямую; торренты — в отказ"

###############################################################################
# 6. Cloudflare WARP (локальный SOCKS на 127.0.0.1:40000)
###############################################################################
if [[ "$DO_WARP" == "1" ]]; then
	step "Cloudflare WARP"
	curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor \
		--output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg 2>/dev/null
	echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" \
		> /etc/apt/sources.list.d/cloudflare-client.list
	apt-get update -qq
	apt-get -y -qq install cloudflare-warp >/dev/null 2>&1
	warp-cli --accept-tos registration new >/dev/null 2>&1
	warp-cli --accept-tos mode proxy      >/dev/null 2>&1
	warp-cli --accept-tos connect         >/dev/null 2>&1
	# Подключение занимает больше пары секунд: ждём до 40, иначе получаем
	# ложное «не поднялся» на самом деле рабочем WARP.
	WARP_OK=0
	for _ in $(seq 1 20); do
		if warp-cli --accept-tos status 2>/dev/null | grep -qi connected; then WARP_OK=1; break; fi
		sleep 2
	done
	[[ "$WARP_OK" == "1" ]] && ok "WARP подключён (SOCKS на 127.0.0.1:40000)" \
	                        || bad "WARP не поднялся — проверить: warp-cli status"
fi

x-ui restart >/dev/null 2>&1
sleep 5

###############################################################################
# 7. Подключения REALITY (443) и XHTTP (8080) через API панели
###############################################################################
if [[ "$CREATE_INBOUNDS" == "1" ]]; then
	step "Создание подключений"
	export PANEL_PORT PANEL_PATH PANEL_PASS CLIENTS PORT_REALITY PORT_XHTTP
	python3 <<'PYEOF'
import json, os, re, secrets, ssl, subprocess, time, urllib.request, urllib.parse, http.cookiejar
PORT, PATH = os.environ["PANEL_PORT"], os.environ["PANEL_PATH"].rstrip("/")
XRAY = "/usr/local/x-ui/bin/xray-linux-amd64"
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                 urllib.request.HTTPCookieProcessor(cj))
BASE = None
for scheme in ("https", "http"):          # панель может быть и на HTTP, если сертификата нет
    try:
        u = f"{scheme}://127.0.0.1:{PORT}{PATH}"
        op.open(urllib.request.Request(u + "/"), timeout=10).read()
        BASE = u; print(f"  панель отвечает по {scheme}"); break
    except Exception:
        continue
if not BASE:
    raise SystemExit("  !!! панель не отвечает, подключения не созданы")

def csrf():
    h = op.open(urllib.request.Request(BASE + "/"), timeout=15).read().decode("utf-8", "replace")
    m = re.search(r'csrf-token" content="([^"]+)', h)
    return m.group(1) if m else ""
def post(path, data, c):
    r = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(data).encode(),
        headers={"X-CSRF-Token": c, "Content-Type": "application/x-www-form-urlencoded"})
    return op.open(r, timeout=25).read().decode("utf-8", "replace")

c = csrf(); post("/login", {"username": "admin", "password": os.environ["PANEL_PASS"]}, c)

def keypair():
    o = subprocess.run([XRAY, "x25519"], capture_output=True, text=True).stdout
    return (re.search(r'PrivateKey:\s*(\S+)', o).group(1),
            re.search(r'(?:Password|PublicKey).*?:\s*(\S+)', o).group(1))
def uuid():
    return subprocess.run([XRAY, "uuid"], capture_output=True, text=True).stdout.strip()

names = os.environ["CLIENTS"].split()
people = {n: {"id": uuid(), "sub": secrets.token_hex(8)} for n in names}
def client(n, flow):
    return {"id": people[n]["id"], "email": n, "flow": flow,
            "enable": True, "subId": people[n]["sub"]}
sniff = json.dumps({"enabled": True, "destOverride": ["http", "tls"]})

def add(remark, port, settings, stream):
    r = post("/panel/api/inbounds/add",
             {"remark": remark, "enable": "true", "port": str(port), "protocol": "vless",
              "up": "0", "down": "0", "total": "0", "expiryTime": "0", "listen": "",
              "settings": settings, "streamSettings": stream, "sniffing": sniff}, csrf())
    good = '"success":true' in r
    print("  %s: %s" % (remark, "создано" if good else "ОШИБКА " + r[:120]))

# --- REALITY на 443 (tcp + vision) ---
pv, pb = keypair(); sid = secrets.token_hex(8)
add("reality-443", os.environ["PORT_REALITY"],
    json.dumps({"clients": [client(n, "xtls-rprx-vision") for n in names], "decryption": "none"}),
    json.dumps({"network": "tcp", "security": "reality", "realitySettings": {
        "show": False, "dest": "max.ru:443", "xver": 0, "serverNames": ["max.ru"],
        "privateKey": pv, "shortIds": [sid],
        "minClientVer": "0.0.0",          # иначе Xray 26.7+ не пустит mihomo
        "settings": {"publicKey": pb, "fingerprint": "chrome", "spiderX": "/"}}}))

# --- XHTTP на 8080 (его ТСПУ не душит, в отличие от голого TCP) ---
pv2, pb2 = keypair(); sid2 = secrets.token_hex(8)
add("reality-8080-xhttp", os.environ["PORT_XHTTP"],
    json.dumps({"clients": [client(n, "") for n in names], "decryption": "none"}),
    json.dumps({"network": "xhttp",
        "xhttpSettings": {"path": "/helix/polls", "host": "", "mode": "auto",
            "scMaxEachPostBytes": "1000000", "scMaxBufferedPosts": 30,
            "scStreamUpServerSecs": "20-80"},
        "security": "reality", "realitySettings": {
            "show": False, "dest": "twitch.tv:443", "xver": 0, "serverNames": ["api.twitch.tv"],
            "privateKey": pv2, "shortIds": [sid2],
            "minClientVer": "0.0.0",
            "settings": {"publicKey": pb2, "fingerprint": "firefox", "spiderX": "/"}}}))

with open("/root/3xui-credentials.txt", "a") as f:
    f.write(f"\nreality-443       sni=max.ru        pbk={pb}  sid={sid}\n")
    f.write(f"reality-8080-xhttp sni=api.twitch.tv path=/helix/polls pbk={pb2} sid={sid2}\n")
    for n in names:
        f.write(f"  {n:<10} uuid={people[n]['id']}  subId={people[n]['sub']}\n")
PYEOF
	x-ui restart >/dev/null 2>&1
	sleep 4
fi

###############################################################################
# 8. Самопроверка
###############################################################################
step "Самопроверка"
/usr/local/x-ui/bin/xray-linux-amd64 -test -config /usr/local/x-ui/bin/config.json 2>&1 | tail -1
journalctl -u x-ui --no-pager 2>/dev/null | grep "Web server running" | tail -1 | sed 's/^/  /'
for p in "${PANEL_PORT}" "${SUB_PORT}" "${PORT_REALITY}" "${PORT_XHTTP}"; do
	ss -tln | grep -q ":${p} " && ok "порт ${p} слушает" || bad "порт ${p} НЕ слушает"
done
printf "  панель отвечает: %s (%s)
" "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "${PANEL_SCHEME}://${MAIN_IP}:${PANEL_PORT}${PANEL_PATH}")" "${PANEL_SCHEME}"
# acme.sh хранит команду перезапуска в base64 — простым grep её не найти.
HOOK=""
for f in /root/.acme.sh/*/*.conf; do
	[[ -f "$f" ]] || continue
	raw=$(grep -h "Le_ReloadCmd" "$f" 2>/dev/null)
	enc=$(sed "s/.*__ACME_BASE64__START_//; s/__ACME_BASE64__END_.*//" <<< "$raw")
	HOOK="${HOOK}${raw}$(base64 -d <<< "$enc" 2>/dev/null)"
done
grep -q "restart x-ui" <<< "$HOOK" \
	&& ok "автопродление перезапускает панель" \
	|| bad "в хуке продления нет перезапуска панели — подписки протухнут (x-ui → 20 → 5)"

###############################################################################
# 9. Итог
###############################################################################
SUB1=$(sqlite3 "$DB" "SELECT sub_id FROM clients WHERE email='Keenetic' LIMIT 1" 2>/dev/null)
cat <<SUMMARY | tee -a /root/3xui-credentials.txt

###############################################################################
 УСТАНОВКА ЗАВЕРШЕНА  ($(date '+%Y-%m-%d %H:%M'))
###############################################################################
 Панель:    ${PANEL_SCHEME}://${MAIN_IP}:${PANEL_PORT}${PANEL_PATH}
 Логин:     admin
 Пароль:    ${PANEL_PASS}

 Подписка:  https://${MAIN_IP}:${SUB_PORT}/sub/${SUB1}

 Выход по умолчанию: ${DEFAULT_EXIT}
 Реквизиты подключений — выше в /root/3xui-credentials.txt

 Сертификат продлевается сам (~раз в 3 дня) и перезапускает панель.
 Управление всем остальным — команда:  x-ui
###############################################################################
SUMMARY
