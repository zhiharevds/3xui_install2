#!/bin/bash
# warp-add.sh — поставить Cloudflare WARP на УЖЕ РАБОТАЮЩИЙ сервер (поставлен установщиком с DO_WARP=0)
# и завести вход Hysteria, весь TCP которого выходит через WARP. Остальные входы WARP не трогает.
#
#   bash warp-add.sh [КЛИЕНТ]
#     КЛИЕНТ — имя нового входа. По умолчанию Keenetic-hy-warp — вход для домашнего шлюза, как делает
#     установщик. Сервер стоит выходом каскада за другим сервером — имя по входящему: HIP-NL-hy-warp.
#
# Часть «поставить WARP, сторож warp-region-fix, warp-on / warp-off» — копия шага 6 установщика
# 3xui_install3.sh: меняешь там — поменяй и здесь. WARP после скрипта ВКЛЮЧЁН (ради этого его и ставят).
set -u
CLIENT=${1:-Keenetic-hy-warp}
DB=/etc/x-ui/x-ui.db
CRED=/root/3xui-credentials.txt
[[ $EUID -eq 0 ]] || { echo "✗ нужен root"; exit 1; }
[[ -f $DB && -f $CRED ]] || { echo "✗ нет $DB или $CRED — сервер ставили не установщиком 3xui_install3"; exit 1; }
command -v sqlite3 >/dev/null || apt-get -y -qq install sqlite3 >/dev/null

echo "── 1. Cloudflare WARP"
if ! command -v warp-cli >/dev/null; then
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor \
	--output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg 2>/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" \
	> /etc/apt/sources.list.d/cloudflare-client.list
apt-get update -qq
apt-get -y -qq install cloudflare-warp >/dev/null 2>&1
for _ in $(seq 1 15); do warp-cli --accept-tos status >/dev/null 2>&1 && break; sleep 1; done
warp-cli --accept-tos registration new >/dev/null 2>&1
warp-cli --accept-tos mode proxy      >/dev/null 2>&1
else
	echo "  уже стоит"
fi
# Сторож страны WARP. Cloudflare выдаёт выходной адрес случайно, и часть таких адресов Google
# считает российскими (на HIP-NL 2026-09-18 — с первой же регистрации). Раз в 10 минут меряет
# страну и, пока она RU, перерегистрирует WARP — сколько бы раз ни пришлось. Пока WARP выключен —
# молчит. Он же умеет померить сам адрес сервера (--direct): по этому замеру решаем, нужен ли WARP.
cat > /usr/local/bin/warp-region-fix <<'WRF'
#!/bin/bash
# Сторож страны WARP: пока Google ИЛИ YouTube считает выход WARP российским — перерегистрировать WARP.
# Базы у них разные: на HIP-NL 2026-09-24 аккаунт видел NL, а YouTube — RU; поэтому смотрим обе.
# Запуск: cron раз в 10 минут (/etc/cron.d/warp-region-fix). Журнал: /var/log/warp-region.log
# Руками: warp-region-fix (одна проверка), --show (страна выхода WARP «аккаунт/YouTube», напр. NL/RU),
#         --direct (то же для самого адреса сервера, без WARP).
exec 9>/run/warp-region-fix.lock; flock -n 9 || exit 0
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
PX=(-x socks5h://127.0.0.1:40000)
[[ "$1" == "--direct" ]] && PX=()
acc() {
curl -s --max-time 12 "${PX[@]}" -A "$UA" \
	'https://accounts.google.com/v3/signin/identifier?flowName=GlifSetupAndroid' \
	| grep -o 'name="region" value="[A-Z]*"' | head -1 | grep -o '[A-Z][A-Z]"' | tr -d '"'
}
yt() {  # регион YouTube из sw.js_data (в начале защитная приставка )]}' — JSON с первой скобки)
curl -s --max-time 12 "${PX[@]}" -A "$UA" 'https://www.youtube.com/sw.js_data' \
	| python3 -c 'import sys,json,re;r=sys.stdin.read();v=json.loads(r[r.index("["):])[0][2][0][0][1];print(v if re.fullmatch("[A-Z]{2}",v) else "")' 2>/dev/null
}
region() { local a y; a=$(acc); y=$(yt); echo "${a:-?}/${y:-?}"; }
[[ "$1" == "--direct" ]] && { region; exit 0; }
if ! systemctl is-active -q warp-svc; then
[[ "$1" == "--show" ]] && echo "WARP выключен"
exit 0
fi
R=$(region)
[[ "$1" == "--show" ]] && { echo "$R"; exit 0; }
[[ "$R" != *RU* ]] && exit 0          # нигде не Россия или не удалось узнать — не трогаем
warp-cli --accept-tos disconnect          >/dev/null 2>&1
warp-cli --accept-tos registration delete >/dev/null 2>&1
warp-cli --accept-tos registration new    >/dev/null 2>&1
warp-cli --accept-tos mode proxy          >/dev/null 2>&1
warp-cli --accept-tos connect             >/dev/null 2>&1
for _ in $(seq 1 20); do warp-cli --accept-tos status 2>/dev/null | grep -qi connected && break; sleep 2; done
sleep 2; N=$(region)
echo "$(date '+%F %T') аккаунт/YouTube было ${R} — перерегистрация, теперь: ${N}" >> /var/log/warp-region.log
[[ "$N" != *RU* ]]
WRF
cat > /usr/local/bin/warp-on <<'WON'
#!/bin/bash
# warp-on — включить Cloudflare WARP на этом сервере: через него выходит вход Keenetic-hy-warp.
# Нужен, когда Google считает адрес сервера российским (панель мониторинга VPS об этом сообщит).
# Выключить обратно: warp-off.
systemctl enable --now warp-svc >/dev/null 2>&1
for _ in $(seq 1 15); do warp-cli --accept-tos status >/dev/null 2>&1 && break; sleep 1; done
warp-cli --accept-tos mode proxy >/dev/null 2>&1
warp-cli --accept-tos connect    >/dev/null 2>&1
OK=0
for _ in $(seq 1 20); do warp-cli --accept-tos status 2>/dev/null | grep -qi connected && { OK=1; break; }; sleep 2; done
[[ "$OK" == "1" ]] || { echo "✗ WARP не подключился — проверить: warp-cli status"; exit 1; }
# сразу после подключения страна выхода ещё не меряется («?/?») — дать WARP до полуминуты
# (HIP-USA 2026-09-25: дважды «не узнать» при установке, через минуту — US/US)
for _ in 1 2 3 4 5 6; do [[ "$(warp-region-fix --show)" != "?/?" ]] && break; sleep 5; done
for _ in 1 2 3 4 5; do          # сразу несколько попыток, чтобы не ждать сторожа
[[ "$(warp-region-fix --show)" != *RU* ]] && break
warp-region-fix
done
R=$(warp-region-fix --show)
if   [[ "$R" == *RU* ]];  then echo "✓ WARP включён, но Google пока считает его выход российским (${R}) — сторож warp-region-fix будет перерегистрировать раз в 10 минут (журнал: /var/log/warp-region.log)"
elif [[ "$R" == "?/?" ]]; then echo "✓ WARP включён; страну выхода узнать не удалось — сторож warp-region-fix проверит сам"
else                          echo "✓ WARP включён; для Google его выход — ${R}"; fi
if [[ -s /root/warp-gateway-line.txt && "$1" != "--quiet" ]]; then
echo
echo "Если WARP-узла этого сервера на домашнем шлюзе ещё нет — выполнить НА ШЛЮЗЕ:"
echo "   $(cat /root/warp-gateway-line.txt)"
fi
WON
cat > /usr/local/bin/warp-off <<'WOF'
#!/bin/bash
# warp-off — выключить Cloudflare WARP. Остаётся установленным и зарегистрированным: включить — warp-on.
warp-cli --accept-tos disconnect >/dev/null 2>&1
systemctl disable --now warp-svc >/dev/null 2>&1
echo "✓ WARP выключен (включить обратно: warp-on). Вход Keenetic-hy-warp без него не работает."
WOF
chmod 755 /usr/local/bin/warp-region-fix /usr/local/bin/warp-on /usr/local/bin/warp-off
echo '*/10 * * * * root /usr/local/bin/warp-region-fix >/dev/null 2>&1' > /etc/cron.d/warp-region-fix
chmod 755 /usr/local/bin/warp-region-fix /usr/local/bin/warp-on /usr/local/bin/warp-off
echo '*/10 * * * * root /usr/local/bin/warp-region-fix >/dev/null 2>&1' > /etc/cron.d/warp-region-fix

echo "── 2. Вход ${CLIENT} (Hysteria) и правило «${CLIENT} → WARP»"
MAIN_IP=$(curl -4 -s --max-time 8 https://api.ipify.org)
CLIENT="$CLIENT" MAIN_IP="$MAIN_IP" python3 - <<'PY' || exit 1
import json, os, re, secrets, sqlite3, ssl, time, http.cookiejar, urllib.request, urllib.parse
client, ip = os.environ["CLIENT"], os.environ["MAIN_IP"]
cred = open("/root/3xui-credentials.txt", encoding="utf-8").read()
base = re.sub(r"//[^:/]+", "//127.0.0.1", re.search(r"Панель:\s*(\S+)", cred).group(1), 1)
user = re.search(r"Логин:\s*(\S+)", cred).group(1)
pwd = re.search(r"Пароль:\s*(\S+)", cred).group(1)
con = sqlite3.connect("/etc/x-ui/x-ui.db")
row = con.execute("select id, remark, settings from inbounds where protocol='hysteria' order by id limit 1").fetchone()
if not row:
    raise SystemExit("✗ на сервере нет входа Hysteria")
iid, remark, settings = row
clients = json.loads(settings)["clients"]
sub = next((c["subId"] for c in clients if c["email"] == client), None)
if sub:
    print(f"  вход {client} уже есть")
else:
    # API панели 3.8: POST panel/api/clients/add {client, inboundIds} (Р-153); вход с 127.0.0.1, токен CSRF со страницы
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                     urllib.request.HTTPSHandler(context=ctx))
    def csrf(page):
        m = re.search(r'name="csrf-token" content="([^"]+)"', page)
        return {"X-CSRF-Token": m.group(1)} if m else {}
    hdr = csrf(op.open(base).read().decode("utf-8", "replace"))
    op.open(urllib.request.Request(base + "login", data=urllib.parse.urlencode({"username": user, "password": pwd}).encode(),
                                   headers=dict(hdr, **{"Content-Type": "application/x-www-form-urlencoded"}))).read()
    hdr = csrf(op.open(base + "panel/").read().decode("utf-8", "replace")) or hdr
    ms, sub = int(time.time() * 1000), secrets.token_hex(8)
    c = {"auth": secrets.token_urlsafe(18), "email": client, "enable": True, "subId": sub,
         "comment": "весь TCP — через Cloudflare WARP (warp-add.sh)", "expiryTime": 0, "limitIp": 0,
         "reset": 0, "security": "", "tgId": 0, "totalGB": 0, "created_at": ms, "updated_at": ms}
    r = op.open(urllib.request.Request(base + "panel/api/clients/add", data=json.dumps({"client": c, "inboundIds": [iid]}).encode(),
                                       headers=dict(hdr, **{"Content-Type": "application/json"}), method="POST")).read().decode()
    if '"success":true' not in r.replace(" ", ""):
        raise SystemExit("✗ панель не добавила вход: " + r[:300])
    print(f"  вход {client} добавлен во «{remark}»")
# шаблон Xray: исходящий warp-cli (не первым — первый = выход по умолчанию) и правило входа сразу за geoip:private
tpl = json.loads(con.execute("select value from settings where key='xrayTemplateConfig'").fetchone()[0])
outs, rules = tpl["outbounds"], tpl["routing"]["rules"]
if not any(o.get("tag") == "warp-cli" for o in outs):
    outs.insert(1, {"tag": "warp-cli", "protocol": "socks", "settings": {"servers": [{"address": "127.0.0.1", "port": 40000, "users": []}]}})
rule = {"type": "field", "user": [client], "network": "tcp", "outboundTag": "warp-cli"}
if rule not in rules:  # WARP-прокси UDP не умеет — UDP этого входа идёт напрямую
    at = next((i + 1 for i, r in enumerate(rules) if r.get("ip") == ["geoip:private"]), 1)
    rules.insert(at, rule)
    open("/tmp/xtpl.json", "w").write(json.dumps(tpl, ensure_ascii=False, indent=2))
    print("  правило добавлено в шаблон")
line = f"https://{ip}:2096/clash/{sub}"
if client == "Keenetic-hy-warp":
    name = re.sub(r"-HY2$", "", remark)
    open("/root/warp-gateway-line.txt", "w").write(f'sudo add-vps {name} --warp "{line}"\n')
open("/tmp/warp-add-sub", "w").write(line)
PY
if [[ -f /tmp/xtpl.json ]]; then  # шаблон пишем при остановленной панели — как установщик
	systemctl stop x-ui
	sqlite3 $DB "UPDATE settings SET value=readfile('/tmp/xtpl.json') WHERE key='xrayTemplateConfig';"
	rm -f /tmp/xtpl.json
	systemctl start x-ui
	sleep 5
	systemctl is-active -q x-ui && echo "  панель перезапущена" || { echo "✗ x-ui не поднялась: journalctl -u x-ui"; exit 1; }
fi

echo "── 3. Включаю WARP"
warp-on
echo
echo "Подписка входа ${CLIENT}: $(cat /tmp/warp-add-sub)"
