#!/usr/bin/env python3
# home-pipe.py — вход «телефон вне дома → домашний шлюз» на этом VPS (обратный туннель Xray, VLESS reverse).
#
# Запускает установщик 3xui_install3.sh; на уже работающем сервере можно запустить руками:
#   python3 home-pipe.py --name DE-214 --password 'ПАРОЛЬ_ADMIN_ПАНЕЛИ'
#
# Что создаёт В ПАНЕЛИ 3x-ui (через её API, всё видно глазами под именами HOME-*):
#   HOME-PIPE-HY   труба для шлюза, VLESS поверх транспорта Hysteria (UDP) — основная, быстрая
#   HOME-PIPE-TCP  труба для шлюза, VLESS по TCP — запасная, если UDP до дома не ходит
#   HOME-DOOR      дверь для телефонов (tcp+udp): ничего не расшифровывает, перекладывает порт в трубу.
#                  Ключей телефонов на сервере нет — шифрование идёт от телефона до шлюза.
# и правило маршрута «дверь → труба». В конце печатает строку для шлюза (sudo add-vps … --pipe …).
#
# ВАЖНО: после ЛЮБОЙ правки входов HOME-* в панели мышкой — нажать «Перезапустить Xray»,
# иначе метка обратного туннеля теряется и вход домой молчит (проверено 2026-09-18).
import argparse, base64, http.cookiejar, json, os, re, sqlite3, ssl, subprocess, sys, time, urllib.parse, urllib.request

XRAY = "/usr/local/x-ui/bin/xray-linux-amd64"
DB = "/etc/x-ui/x-ui.db"
KEYS = "/root/home-pipe-keys.json"
TAG = "r-home"

ap = argparse.ArgumentParser()
ap.add_argument("--name", required=True, help="имя сервера, как в шлюзе (например DE-214)")
ap.add_argument("--password", default=os.environ.get("PANEL_PASS", ""), help="пароль admin панели")
ap.add_argument("--user", default="admin")
ap.add_argument("--ip", default=os.environ.get("MAIN_IP", ""))
ap.add_argument("--port-hy", type=int, default=int(os.environ.get("PORT_PIPE_HY", 58932)))
ap.add_argument("--port-tcp", type=int, default=int(os.environ.get("PORT_PIPE_TCP", 58930)))
ap.add_argument("--port-door", type=int, default=int(os.environ.get("PORT_DOOR", 2053)))
ap.add_argument("--home-port", type=int, default=24443, help="порт входов mihomo на шлюзе")
ap.add_argument("--cert", default=os.environ.get("CERT", ""))
ap.add_argument("--key", default=os.environ.get("KEY", ""))
a = ap.parse_args()


def setting(k):
    try:
        r = sqlite3.connect(DB).execute("select value from settings where key=?", (k,)).fetchone()
        return r[0] if r else ""
    except Exception:
        return ""


port = setting("webPort") or "2053"
path = ("/" + (setting("webBasePath") or "/").strip("/")).rstrip("/")
cert = a.cert or setting("webCertFile")
key = a.key or setting("webKeyFile")
ip = a.ip or subprocess.run("ip -4 route get 1.1.1.1 | sed -n 's/.*src \\([0-9.]*\\).*/\\1/p'", shell=True,
                            capture_output=True, text=True).stdout.strip()

ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                 urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
BASE = None
for scheme in ("https", "http"):
    try:
        op.open(f"{scheme}://127.0.0.1:{port}{path}/", timeout=10).read(); BASE = f"{scheme}://127.0.0.1:{port}{path}"; break
    except Exception:
        continue
if not BASE:
    sys.exit("  !!! панель не отвечает — вход домой не создан")


def csrf():
    m = re.search(r'csrf-token" content="([^"]+)', op.open(BASE + "/", timeout=15).read().decode("utf-8", "replace"))
    return m.group(1) if m else ""


def post(p, data):
    r = urllib.request.Request(BASE + p, data=urllib.parse.urlencode(data).encode(),
                               headers={"X-CSRF-Token": csrf(), "Content-Type": "application/x-www-form-urlencoded"})
    return op.open(r, timeout=30).read().decode("utf-8", "replace")


if '"success":true' not in post("/login", {"username": a.user, "password": a.password}):
    sys.exit("  !!! не вошёл в панель (пароль admin?) — вход домой не создан")

have = {}
try:
    for rid, remark, tag in sqlite3.connect(DB).execute("select id, remark, tag from inbounds where remark like 'HOME-%'"):
        have[remark] = tag
except Exception:
    pass
keys = json.load(open(KEYS)) if os.path.isfile(KEYS) else {}
if have and not keys:
    sys.exit("  !!! входы HOME-* уже есть, а файла " + KEYS + " нет — удали входы HOME-* в панели и запусти снова")

if not keys:
    enc = subprocess.run([XRAY, "vlessenc"], capture_output=True, text=True).stdout
    keys = {"dec": re.search(r'"decryption": "([^"]+)"', enc).group(1),
            "enc": re.search(r'"encryption": "([^"]+)"', enc).group(1),
            "u_hy": subprocess.run([XRAY, "uuid"], capture_output=True, text=True).stdout.strip(),
            "u_tcp": subprocess.run([XRAY, "uuid"], capture_output=True, text=True).stdout.strip()}
ms = int(time.time() * 1000)


def client(email, uid):
    return {"id": uid, "email": email, "flow": "", "enable": True, "subId": "", "comment": "труба домашнего шлюза",
            "expiryTime": 0, "limitIp": 0, "reset": 0, "tgId": 0, "totalGB": 0, "created_at": ms, "updated_at": ms,
            "reverse": {"tag": TAG}}


def add(remark, prt, protocol, settings, stream):
    if remark in have:
        print(f"  {remark}: уже есть"); return have[remark]
    r = json.loads(post("/panel/api/inbounds/add", {
        "remark": remark, "enable": "true", "port": str(prt), "protocol": protocol, "up": "0", "down": "0", "total": "0",
        "expiryTime": "0", "listen": "", "settings": json.dumps(settings), "streamSettings": json.dumps(stream),
        "sniffing": json.dumps({"enabled": False})}))     # sniffing выключен намеренно: иначе REALITY телефона уедет на сайт-прикрытие
    print(f"  {remark}: " + ("создано" if r.get("success") else "ОШИБКА " + str(r.get("msg"))[:120]))
    return (r.get("obj") or {}).get("tag") if r.get("success") else None


hy_ok = bool(cert and key and os.path.isfile(cert) and os.path.isfile(key))
if hy_ok:
    add("HOME-PIPE-HY", a.port_hy, "vless",
        {"clients": [client("home-pipe-hy", keys["u_hy"])], "decryption": keys["dec"], "encryption": keys["enc"]},
        {"network": "hysteria", "security": "tls",
         "tlsSettings": {"serverName": "", "minVersion": "1.2", "maxVersion": "1.3", "alpn": ["h3"],
                         "certificates": [{"certificateFile": cert, "keyFile": key}]},
         "hysteriaSettings": {"version": 2, "auth": "home-pipe", "udpIdleTimeout": 60}})
else:
    print("  HOME-PIPE-HY: ПРОПУЩЕНО — у панели нет сертификата (останется труба по TCP)")
add("HOME-PIPE-TCP", a.port_tcp, "vless",
    {"clients": [client("home-pipe-tcp", keys["u_tcp"])], "decryption": keys["dec"], "encryption": keys["enc"]},
    {"network": "tcp", "security": "none", "tcpSettings": {"acceptProxyProtocol": False, "header": {"type": "none"}}})
door = add("HOME-DOOR", a.port_door, "tunnel",
           {"address": "127.0.0.1", "port": a.home_port, "network": "tcp,udp", "followRedirect": False}, {})
if not door:
    sys.exit("  !!! дверь не создана")
json.dump(keys, open(KEYS, "w")); os.chmod(KEYS, 0o600)

g = json.loads(post("/panel/api/xray/", {}))
xs = json.loads(g["obj"])["xraySetting"]
xs = json.loads(xs) if isinstance(xs, str) else xs
rules = xs.setdefault("routing", {}).setdefault("rules", [])
if not any(r.get("outboundTag") == TAG for r in rules):
    # ВЫШЕ правила «частные адреса → blocked»: дверь ведёт на 127.0.0.1 шлюза (по ту сторону трубы)
    pos = 1 if rules and rules[0].get("outboundTag") == "api" else 0
    rules.insert(pos, {"type": "field", "inboundTag": [door], "outboundTag": TAG})
    ok = '"success":true' in post("/panel/api/xray/update", {"xraySetting": json.dumps(xs, indent=2)})
    print("  правило маршрута «дверь → труба»: " + ("добавлено" if ok else "ОШИБКА"))
post("/panel/api/server/restartXrayService", {})

for p_ in (f"{a.port_hy}/udp", f"{a.port_tcp}/tcp", f"{a.port_door}/tcp", f"{a.port_door}/udp"):
    subprocess.run(["ufw", "allow", p_], capture_output=True)

tok = {"h": ip, "hy": a.port_hy if hy_ok else 0, "tcp": a.port_tcp, "door": a.port_door,
       "uhy": keys["u_hy"], "utcp": keys["u_tcp"], "enc": keys["enc"]}
token = base64.urlsafe_b64encode(json.dumps(tok, separators=(",", ":")).encode()).decode().rstrip("=")
open("/root/home-pipe.token", "w").write(token)
print(f"\n  НА ШЛЮЗЕ (одна строка):\n\n  sudo add-vps {a.name} --pipe {token}\n")
