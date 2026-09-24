#!/usr/bin/env python3
# add-vps — добавить (или убрать) сервер в домашнем шлюзе mihomo ОДНОЙ командой.
# Живёт на шлюзе: /usr/local/bin/add-vps. Готовую строку запуска печатает установщик сервера
# 3xui_install3.sh в конце своей работы.
#
#   sudo add-vps GB-113 https://IP:2096/clash/<id> [https://IP:2096/clash/<id-hysteria> [https://IP:2096/clash/<id-warp>]]
#   sudo add-vps GB-113 … --pipe <строка>   то же + вход «телефон вне дома → дом» через этот сервер
#   sudo add-vps GB-113 --pipe <строка>     только вход домой (сервер в шлюзе уже есть)
#   sudo add-vps --remove GB-113
#   sudo add-vps --reload            (перечитать конфиг, если в прошлый раз помешала игра)
#
# Что делает: дописывает подписку(и) в proxy-providers, добавляет сервер в список тумблера VPN
# (только в СПИСОК — трафик на него не пойдёт, пока сам не выберешь его в панели), проверяет
# конфиг (mihomo -t), сохраняет в git и перечитывает. Если проверка не прошла — возвращает как было.
# Конфиг правится как текст, комментарии не трогаются.
import argparse, base64, json, os, re, subprocess, sys, urllib.request

HOME = "/etc/mihomo"
CONFIG = HOME + "/config.yaml"
API = "http://127.0.0.1:9090"
LIST_GROUP = "VPN"  # тумблер со списком «любой сервер поштучно»
PIPE_DIR = "/opt/home-pipe"                      # служба home-pipe: обратный туннель шлюз → VPS (Р-66)
PIPE_CFG = PIPE_DIR + "/config.json"
PHONES = HOME + "/phones/secrets.json"           # ключи людей и список входов (ведёт команда phones)


def die(msg):
    print("✗ " + msg)
    sys.exit(1)


def provider_block(pid, url, note):
    return (f"  # {note}\n"
            f"  {pid}:\n"
            f"    type: http\n"
            f"    url: \"{url}\"\n"
            f"    path: ./providers/{pid}.yaml\n"
            f"    interval: 86400\n"
            f"    override: {{ udp: true }}\n"
            f"    health-check: {{ enable: true, url: https://www.gstatic.com/generate_204, interval: 300 }}\n\n")


def providers_section(s):
    a = re.search(r"^proxy-providers:\s*$", s, re.M)
    b = re.search(r"^proxy-groups:\s*$", s, re.M)
    if not a or not b or b.start() < a.end():
        die("не нашёл в конфиге разделы proxy-providers / proxy-groups")
    return a.end(), b.start()


def has_provider(s, pid):
    a, b = providers_section(s)
    return re.search(rf"^  {re.escape(pid)}:\s*$", s[a:b], re.M) is not None


def edit_use(s, group, add=(), remove=()):
    """Правит строку `use: [...]` внутри группы с именем group. Возвращает новый текст."""
    m = re.search(rf"^  - name: \"?{re.escape(group)}\"?\s*$", s, re.M)
    if not m:
        die(f"не нашёл тумблер «{group}»")
    nxt = re.search(r"^  - name: ", s[m.end():], re.M)
    end = m.end() + (nxt.start() if nxt else len(s) - m.end())
    body = s[m.end():end]
    u = re.search(r"^(    use: \[)([^\]]*)(\].*)$", body, re.M)
    if not u:
        die(f"у тумблера «{group}» нет строки use: [...]")
    items = [x.strip() for x in u.group(2).split(",") if x.strip()]
    items = [x for x in items if x not in remove] + [x for x in add if x not in items]
    body = body[:u.start()] + u.group(1) + ", ".join(items) + u.group(3) + body[u.end():]
    return s[:m.end()] + body + s[end:]


def remove_everywhere(s, pids):
    """Убирает провайдеров и все их упоминания в use: [...] любых групп."""
    for pid in pids:
        a, b = providers_section(s)
        sec = s[a:b]
        m = re.search(rf"^(?:  #.*\n)*  {re.escape(pid)}:\s*\n(?:    .*\n|      .*\n|        .*\n)*\n?", sec, re.M)
        if m:
            s = s[:a] + sec[:m.start()] + sec[m.end():] + s[b:]

    def fix(mm):
        items = [x.strip() for x in mm.group(2).split(",") if x.strip() and x.strip() not in pids]
        return mm.group(1) + ", ".join(items) + mm.group(3)
    return re.sub(r"^(    use: \[)([^\]]*)(\])", fix, s, flags=re.M)


def pipe_token(tok):
    try:
        t = json.loads(base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)))
        assert t["h"] and t["tcp"] and t["door"] and t["enc"]
        return t
    except Exception:
        die("строка --pipe повреждена — скопируй её целиком из вывода установщика")


def pipe_edit(name, pid, tok=None):
    """Добавляет (tok задан) или убирает (tok=None) трубы к серверу в службе home-pipe и вход в список phones.
    mihomo не трогает — перечитывать конфиг для этого не нужно."""
    if not os.path.isfile(PIPE_CFG) or not os.path.isfile(PHONES):
        print("! на этом шлюзе нет службы home-pipe / входов для телефонов — часть --pipe пропущена"); return False
    cfg = json.load(open(PIPE_CFG)); sec = json.load(open(PHONES))
    tags = (pid + "-hy", pid + "-tcp")
    cfg["outbounds"] = [o for o in cfg["outbounds"] if o.get("tag") not in tags]
    doors = [d for d in sec.get("doors") or [{"name": "RU", "host": "130.17.11.191", "port": 2053}] if d["name"] != name]
    if tok:
        def out(tag, port, uid, stream):
            return {"protocol": "vless", "tag": tag, "streamSettings": stream,
                    "settings": {"address": tok["h"], "port": port, "encryption": tok["enc"], "id": uid, "reverse": {"tag": "r-in"}}}
        if tok.get("hy"):
            cfg["outbounds"].append(out(tags[0], tok["hy"], tok["uhy"], {
                "network": "hysteria", "hysteriaSettings": {"version": 2, "auth": "home-pipe"}, "security": "tls",
                "tlsSettings": {"serverName": tok["h"], "alpn": ["h3"]}}))
        cfg["outbounds"].append(out(tags[1], tok["tcp"], tok["utcp"], {"network": "raw"}))
        doors.append({"name": name, "host": tok["h"], "port": tok["door"]})
    tmp = PIPE_DIR + "/config.new.json" if PIPE_CFG.startswith(PIPE_DIR) else PIPE_CFG + ".new.json"; json.dump(cfg, open(tmp, "w"), indent=1)
    t = subprocess.run([PIPE_DIR + "/xray", "run", "-test", "-c", tmp], capture_output=True, text=True)
    if "Configuration OK" not in t.stdout + t.stderr:
        os.remove(tmp); print((t.stdout + t.stderr).strip()[-300:]); die("проверка конфига home-pipe не прошла — не тронут")
    os.replace(tmp, PIPE_CFG); os.chmod(PIPE_CFG, 0o644)
    sec["doors"] = doors; json.dump(sec, open(PHONES, "w"), indent=1, ensure_ascii=False); os.chmod(PHONES, 0o600)
    subprocess.run(["systemctl", "restart", "home-pipe"])
    print(f"✓ вход домой через {name}: " + ("обратный туннель подключён, ссылки появились на странице «Подключение устройств»" if tok else "убран"))
    return True


def direct_rule(s, ip, name, add=True):
    """Адрес VPS — всегда напрямую: иначе трубу службы home-pipe правила завернут в туннель."""
    line = f"  - IP-CIDR,{ip}/32,DIRECT,no-resolve   # add-vps: {name}\n"
    s = re.sub(rf"^  - IP-CIDR,[0-9.]+/32,DIRECT,no-resolve   # add-vps: {re.escape(name)}\n", "", s, flags=re.M)
    if add and f"IP-CIDR,{ip}/32,DIRECT" not in s:
        m = re.search(r"^rules:[ ]*\n", s, re.M)
        s = s[:m.end()] + line + s[m.end():]
    return s


def forget_host_key(ip):
    """Сервер переустановили — у него новый отпечаток SSH, а панель мониторинга помнит старый и
    перестаёт на него заходить («Host key has changed»). Добавляем сервер заново → старый отпечаток забыть."""
    kh = "/opt/vpsdash/data/known_hosts"
    if ip and os.path.isfile(kh):
        st = os.stat(kh)
        subprocess.run(["ssh-keygen", "-f", kh, "-R", ip], capture_output=True)
        os.chown(kh, st.st_uid, st.st_gid)
        try:
            os.remove(kh + ".old")
        except OSError:
            pass


def game_running():
    try:
        cs = json.load(urllib.request.urlopen(API + "/connections", timeout=10)).get("connections") or []
    except Exception:
        return False
    for c in cs:
        host = (c["metadata"].get("host") or c["metadata"].get("sniffHost") or "")
        if c["chains"][-1] in ("The First Descendant", "Игра") or "nexon" in host:  # «Игра» — старое имя тумблера (до Р-112)
            return True
    return False


def reload_config(a):
    if game_running() and not a.force:
        print("! Запущена игра — конфиг НЕ перечитан, чтобы не сбить ей адреса.")
        print("  Когда закроешь игру:  sudo add-vps --reload")
        return False
    req = urllib.request.Request(API + "/configs?force=true", data=json.dumps({"path": CONFIG}).encode(),
                                 method="PUT", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30)
    print("✓ конфиг перечитан")
    return True


def main():
    ap = argparse.ArgumentParser(description="Добавить или убрать сервер в шлюзе mihomo")
    ap.add_argument("name", nargs="?", help="имя сервера, как напечатал установщик (например GB-113)")
    ap.add_argument("url", nargs="?", help="ссылка подписки формата mihomo (/clash/...)")
    ap.add_argument("hy_url", nargs="?", help="ссылка подписки Hysteria (/clash/...), если есть")
    ap.add_argument("warp_url", nargs="?", help="ссылка подписки запасного входа «через WARP» (/clash/...), если есть")
    ap.add_argument("--pipe", help="строка от установщика: вход «телефон вне дома → дом» через этот сервер")
    ap.add_argument("--remove", action="store_true", help="убрать сервер из шлюза")
    ap.add_argument("--reload", action="store_true", help="только перечитать конфиг")
    ap.add_argument("--config", default=CONFIG, help=argparse.SUPPRESS)
    ap.add_argument("--no-apply", action="store_true", help="только поправить файл и проверить; без git и перечитывания")
    ap.add_argument("--force", action="store_true", help="перечитать конфиг, даже если запущена игра")
    a = ap.parse_args()

    if os.geteuid() != 0 and a.config == CONFIG:
        die("нужны права: запусти через sudo")
    if a.reload:
        return reload_config(a)
    if not a.name:
        die("не указано имя сервера")
    pid = re.sub(r"[^a-z0-9-]+", "-", a.name.lower()).strip("-")
    if not pid:
        die("пустое имя")
    pids = [pid, pid + "-hy2", pid + "-hy2-warp"]
    old = open(a.config, encoding="utf-8").read()

    tok = pipe_token(a.pipe) if a.pipe else None
    if a.url and not a.hy_url and "/clash/" not in a.url and not a.pipe and len(a.url) > 120:
        die("похоже, это строка --pipe: перед ней нужно слово --pipe")
    if a.remove:
        had_pipe = pipe_edit(a.name, pid, None) if a.config == CONFIG else False
        if not any(has_provider(old, p) for p in pids) and not had_pipe:
            die(f"сервера «{pid}» в конфиге нет")
        new = direct_rule(remove_everywhere(old, pids), "0.0.0.0", a.name, add=False)
        what = f"Сервер {a.name} убран из шлюза"
    elif tok and not a.url:
        new = direct_rule(old, tok["h"], a.name)
        what = f"Вход домой через {a.name}"
    else:
        if not a.url:
            die("не указана ссылка подписки")
        for u in (a.url, a.hy_url, a.warp_url):
            if u and "/clash/" not in u:
                die(f"ссылка {u} не формата mihomo (в ней должно быть /clash/) — возьми ту, что напечатал установщик")
        if has_provider(old, pid):
            die(f"сервер «{pid}» уже есть в конфиге (убрать: sudo add-vps --remove {a.name})")
        blocks = provider_block(pid, a.url, f"{a.name}: добавлен командой add-vps; подписка формата mihomo — поправки не нужны")
        added = [pid]
        if a.hy_url:
            blocks += provider_block(pid + "-hy2", a.hy_url, f"{a.name}: Hysteria 2 (отдельная подписка — панель мониторинга меряет один узел на подписку)")
            added.append(pid + "-hy2")
        if a.warp_url:
            blocks += provider_block(pid + "-hy2-warp", a.warp_url, f"{a.name}: Hysteria 2, выход через Cloudflare WARP — запасной на случай, если адрес сервера Google считает российским")
            added.append(pid + "-hy2-warp")
        _, b = providers_section(old)
        new = old[:b] + blocks + old[b:]
        new = edit_use(new, LIST_GROUP, add=added)
        if tok:
            new = direct_rule(new, tok["h"], a.name)
        what = f"Сервер {a.name} добавлен в шлюз"

    open(a.config, "w", encoding="utf-8").write(new)
    t = subprocess.run(["/usr/local/bin/mihomo", "-t", "-d", HOME, "-f", a.config], capture_output=True, text=True)
    if "test is successful" not in (t.stdout + t.stderr):
        open(a.config, "w", encoding="utf-8").write(old)
        print((t.stdout + t.stderr).strip()[-400:])
        die("проверка конфига не прошла — всё возвращено как было")
    print("✓ конфиг поправлен и прошёл проверку")
    if a.no_apply:
        return
    if tok and not a.remove:
        pipe_edit(a.name, pid, tok)

    if not a.remove:
        m = re.search(r"//([0-9.]+):", a.url or "")
        forget_host_key(tok["h"] if tok else (m.group(1) if m else ""))
    if a.remove:  # скачанные файлы подписок убранного сервера больше не нужны
        for p_ in pids:
            try:
                os.remove(f"{HOME}/providers/{p_}.yaml")
            except OSError:
                pass
    subprocess.run(["git", "-C", HOME, "commit", "-qam", what + " (add-vps)"], capture_output=True)
    print("✓ сохранено в git: " + what)
    if new == old:
        return
    if reload_config(a) and not a.remove and a.url:
        print(f"Готово. В панели узлов сервер появится в списке тумблера «{LIST_GROUP}»; трафик на него пойдёт,")
        print("только когда выберешь его сам. В панели мониторинга VPS он покажется при следующем замере.")


if __name__ == "__main__":
    main()
