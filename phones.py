#!/usr/bin/env python3
"""phones — ключи и ссылки для телефонов/компьютеров, которые ходят в интернет через домашний шлюз (Р-66).

  sudo phones list            кто заведён
  sudo phones link ИМЯ        ссылки и QR-коды для Happ (Hysteria и XHTTP, по каждому VPS-входу)
  sudo phones add ИМЯ         завести человека/устройство и показать его ссылки
  sudo phones del ИМЯ         удалить устройство: его ключ перестаёт работать (потерян телефон и т.п.)
  sudo phones rename ИМЯ НОВОЕ   переименовать; ключи те же, уже добавленные в Happ узлы работают дальше
  sudo phones json            всё то же для страницы панели мониторинга (ссылки + QR в SVG)

Ключи живут только на шлюзе: /etc/mihomo/phones/secrets.json (вне git). На VPS их нет —
VPS лишь перекладывает порт в обратный туннель («слепая труба»).
"""
import json, os, re, secrets, subprocess, sys, urllib.parse as up, urllib.request, uuid

CFG, SEC = "/etc/mihomo/config.yaml", "/etc/mihomo/phones/secrets.json"
BEGIN, END = "# >>> phones (блок пишет команда phones, руками не править)", "# <<< phones"

def load():
    s = json.load(open(SEC))
    s.setdefault("doors", [{"name": "RU", "host": "130.17.11.191", "port": 2053}])
    s.setdefault("xhttp_path", "/api/v2/sync")
    s.setdefault("listen_port", 24443)
    if "hy2_pin" not in s:
        fp = subprocess.run(["openssl", "x509", "-in", "/etc/mihomo/phones/hy2.crt", "-noout", "-fingerprint", "-sha256"],
                            capture_output=True, text=True).stdout
        s["hy2_pin"] = fp.split("=")[1].strip().replace(":", "")
    return s

def save(s):
    json.dump(s, open(SEC, "w"), indent=1, ensure_ascii=False); os.chmod(SEC, 0o600)

def block(s):
    r, p = s["reality"], s["listen_port"]
    L = [BEGIN, "listeners:",
         "  - name: phones-hy2", "    type: hysteria2", "    listen: 127.0.0.1", f"    port: {p}",
         "    certificate: ./phones/hy2.crt", "    private-key: ./phones/hy2.key", "    users:"]
    L += [f"      {n}: {v['hy2']}" for n, v in s["users"].items()]
    L += ["  - name: phones-vless", "    type: vless", "    listen: 127.0.0.1", f"    port: {p}", "    users:"]
    L += [f"      - {{ username: {n}, uuid: {v['uuid']} }}" for n, v in s["users"].items()]
    L += ["    # XHTTP пакует трафик в пару соединений: простой VLESS-TCP мобильный оператор банит",
          "    # за пачку одновременных одинаковых соединений (проверено 2026-09-18, Р-66).",
          "    xhttp-config:", f"      path: {s['xhttp_path']}", "      mode: auto",
          "    reality-config:", f"      dest: {r['sni']}:443", f"      private-key: {r['private']}",
          f"      short-id: [ \"{r['short_id']}\" ]", f"      server-names: [ {r['sni']} ]", END]
    return "\n".join(L)

def apply(s, msg):
    text = open(CFG, encoding="utf-8").read()
    if BEGIN in text:
        new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda m: block(s), text, flags=re.S)
    else:                                   # первый запуск: заменить блок, написанный вручную
        a = text.index("listeners:\n  - name: phones-hy2"); b = text.index("\nrules:\n", a)
        new = text[:a] + block(s) + "\n" + text[b:]
    tmp = "/tmp/phones-config.yaml"; open(tmp, "w", encoding="utf-8").write(new)
    t = subprocess.run(["mihomo", "-t", "-f", tmp, "-d", "/etc/mihomo"], capture_output=True, text=True)
    if "test is successful" not in t.stdout + t.stderr:
        sys.exit("!!! проверка конфига не прошла, ничего не изменено:\n" + (t.stdout + t.stderr)[-600:])
    open(CFG, "w", encoding="utf-8").write(new); save(s)
    subprocess.run(["git", "-C", "/etc/mihomo", "commit", "-q", "-am", msg])
    reload_now()

PENDING = "/etc/mihomo/phones/.reload-pending"

def game_running():
    # Перечитывание конфига сбивает игре подставные адреса, и она вылетает (Р-62) — при игре откладываем.
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:9090/connections", timeout=10))
        return any("nexon" in (c["metadata"].get("host") or "") for c in d.get("connections") or [])
    except Exception:
        return False

def reload_now():
    if game_running():
        open(PENDING, "w").write("1")
        print("ОТЛОЖЕНО: на шлюзе идёт игра. Изменение сохранено и включится само после закрытия игры.")
        return False
    rq = urllib.request.Request("http://127.0.0.1:9090/configs?force=false", method="PUT",
                                data=json.dumps({"path": CFG}).encode(), headers={"Content-Type": "application/json"})
    ok = urllib.request.urlopen(rq, timeout=20).status == 204
    if ok and os.path.exists(PENDING): os.remove(PENDING)
    print("конфиг шлюза перечитан:", ok)
    return ok

def links(s, name):
    u, r = s["users"][name], s["reality"]
    out = []
    for d in s["doors"]:
        tag = up.quote(f"Дом {d['name']}")
        out.append((f"Hysteria через {d['name']} (основной)",
            f"hysteria2://{up.quote(u['hy2'])}@{d['host']}:{d['port']}/?sni=home.phones&insecure=1&pinSHA256={s['hy2_pin']}#{tag}%20Hysteria"))
        out.append((f"XHTTP через {d['name']} (запасной, TCP)",
            f"vless://{u['uuid']}@{d['host']}:{d['port']}?type=xhttp&path={up.quote(s['xhttp_path'], safe='')}&mode=auto&security=reality"
            f"&pbk={r['public']}&sid={r['short_id']}&sni={r['sni']}&fp=chrome&encryption=none#{tag}%20XHTTP"))
    return out

def show(s, name):
    for title, link in links(s, name):
        print(f"\n===== {name}: {title} =====\n{link}\n")
        q = subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "1", link], capture_output=True, text=True)
        print(q.stdout if q.returncode == 0 else "(QR не нарисован: sudo apt install qrencode)")

def as_json(s):
    out = []
    for n in s["users"]:
        items = []
        for title, link in links(s, n):
            q = subprocess.run(["qrencode", "-t", "SVG", "-m", "2", "-o", "-", link], capture_output=True, text=True)
            items.append({"title": title, "link": link, "svg": q.stdout[q.stdout.find("<svg"):] if q.returncode == 0 else ""})
        out.append({"name": n, "links": items})
    print(json.dumps({"users": out, "doors": s["doors"], "pending": os.path.exists(PENDING)}, ensure_ascii=False))

def main():
    a = sys.argv[1:]
    if not a or a[0] not in ("list", "link", "add", "del", "rename", "json") or (a[0] not in ("list", "json") and len(a) < 2):
        sys.exit(__doc__)
    if os.geteuid() != 0: sys.exit("запускать через sudo")
    s = load()
    if a[0] == "list":
        print("\n".join(s["users"])); return
    if a[0] == "json":
        if os.path.exists(PENDING) and not game_running():
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()): reload_now()
        as_json(s); return
    n = a[1]
    if a[0] == "link":
        if n not in s["users"]: sys.exit(f"нет такого: {n}. Есть: {', '.join(s['users'])}")
        show(s, n)
    elif a[0] == "add":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", n): sys.exit("имя: латиница, цифры, - и _")
        if n in s["users"]: sys.exit("уже есть; ссылки: sudo phones link " + n)
        s["users"][n] = {"uuid": str(uuid.uuid4()), "hy2": secrets.token_urlsafe(18)}
        apply(s, f"phones: добавлен {n} (Р-66)")
        if "--quiet" not in a: show(s, n)
    elif a[0] == "rename":
        new = a[2] if len(a) > 2 else ""
        if n not in s["users"]: sys.exit("нет такого: " + n)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", new): sys.exit("имя: латиница, цифры, - и _")
        if new in s["users"]: sys.exit("такое имя уже есть: " + new)
        s["users"] = {(new if k == n else k): v for k, v in s["users"].items()}
        apply(s, f"phones: {n} переименован в {new} (Р-66)"); print("переименовано:", n, "→", new)
    elif a[0] == "del":
        if n not in s["users"]: sys.exit("нет такого: " + n)
        del s["users"][n]; apply(s, f"phones: отозван {n} (Р-66)"); print("устройство удалено:", n)

main()
