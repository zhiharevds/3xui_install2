#!/usr/bin/env python3
# add-vps — добавить (или убрать) сервер в домашнем шлюзе mihomo ОДНОЙ командой.
# Живёт на шлюзе: /usr/local/bin/add-vps. Готовую строку запуска печатает установщик сервера
# 3xui_install3.sh в конце своей работы.
#
#   sudo add-vps GB-113 https://IP:2096/clash/<id> [https://IP:2096/clash/<id-hysteria>]
#   sudo add-vps --remove GB-113
#   sudo add-vps --reload            (перечитать конфиг, если в прошлый раз помешала игра)
#
# Что делает: дописывает подписку(и) в proxy-providers, добавляет сервер в список тумблера VPN
# (только в СПИСОК — трафик на него не пойдёт, пока сам не выберешь его в панели), проверяет
# конфиг (mihomo -t), сохраняет в git и перечитывает. Если проверка не прошла — возвращает как было.
# Конфиг правится как текст, комментарии не трогаются.
import argparse, json, os, re, subprocess, sys, urllib.request

HOME = "/etc/mihomo"
CONFIG = HOME + "/config.yaml"
API = "http://127.0.0.1:9090"
LIST_GROUP = "VPN"  # тумблер со списком «любой сервер поштучно»


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


def game_running():
    try:
        cs = json.load(urllib.request.urlopen(API + "/connections", timeout=10)).get("connections") or []
    except Exception:
        return False
    for c in cs:
        host = (c["metadata"].get("host") or c["metadata"].get("sniffHost") or "")
        if c["chains"][-1] == "Игра" or "nexon" in host:
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
    pids = [pid, pid + "-hy2"]
    old = open(a.config, encoding="utf-8").read()

    if a.remove:
        if not any(has_provider(old, p) for p in pids):
            die(f"сервера «{pid}» в конфиге нет")
        new = remove_everywhere(old, pids)
        what = f"Сервер {a.name} убран из шлюза"
    else:
        if not a.url:
            die("не указана ссылка подписки")
        for u in (a.url, a.hy_url):
            if u and "/clash/" not in u:
                die(f"ссылка {u} не формата mihomo (в ней должно быть /clash/) — возьми ту, что напечатал установщик")
        if has_provider(old, pid):
            die(f"сервер «{pid}» уже есть в конфиге (убрать: sudo add-vps --remove {a.name})")
        blocks = provider_block(pid, a.url, f"{a.name}: добавлен командой add-vps; подписка формата mihomo — поправки не нужны")
        added = [pid]
        if a.hy_url:
            blocks += provider_block(pid + "-hy2", a.hy_url, f"{a.name}: Hysteria 2 (отдельная подписка — панель мониторинга меряет один узел на подписку)")
            added.append(pid + "-hy2")
        _, b = providers_section(old)
        new = old[:b] + blocks + old[b:]
        new = edit_use(new, LIST_GROUP, add=added)
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

    if a.remove:  # скачанные файлы подписок убранного сервера больше не нужны
        for p_ in pids:
            try:
                os.remove(f"{HOME}/providers/{p_}.yaml")
            except OSError:
                pass
    subprocess.run(["git", "-C", HOME, "commit", "-qam", what + " (add-vps)"], capture_output=True)
    print("✓ сохранено в git: " + what)
    if reload_config(a) and not a.remove:
        print(f"Готово. В панели узлов сервер появится в списке тумблера «{LIST_GROUP}»; трафик на него пойдёт,")
        print("только когда выберешь его сам. В панели мониторинга VPS он покажется при следующем замере.")


if __name__ == "__main__":
    main()
