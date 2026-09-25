#!/usr/bin/env python3
# vps-health.py — «градусник» VPS для панели мониторинга VPS (vpsdash) (Р-30).
# Лежит на каждом VPS в /usr/local/bin/vps-health.py. Шлюз заходит по SSH-ключу, которому
# в authorized_keys разрешена ТОЛЬКО эта команда (command=...), и забирает одну строку JSON.
# Ничего не меняет — только читает.
import json, os, re, shutil, sqlite3, subprocess, time

def sh(cmd, t=8):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t).stdout.strip()
    except Exception:
        return ""

def rd(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default

r = {"ts": int(time.time()), "host": os.uname().nodename}

# нагрузка, память, диск, время работы
r["uptime_s"] = int(float(rd("/proc/uptime", "0 0").split()[0]))
la = rd("/proc/loadavg", "0 0 0").split()
r["load"] = [float(x) for x in la[:3]]
r["cpus"] = os.cpu_count()
mem = {}
for line in (rd("/proc/meminfo", "") or "").splitlines():
    k, v = line.split(":", 1)
    mem[k] = int(v.split()[0]) // 1024
r["mem_total_mb"] = mem.get("MemTotal")
r["mem_avail_mb"] = mem.get("MemAvailable")
r["swap_used_mb"] = (mem.get("SwapTotal", 0) or 0) - (mem.get("SwapFree", 0) or 0)
du = shutil.disk_usage("/")
r["disk_used_pct"] = round(du.used * 100 / du.total, 1)

# таблица соединений ядра (переполнение = 2026-09-10 на NL, Р-28)
c, m = rd("/proc/sys/net/netfilter/nf_conntrack_count"), rd("/proc/sys/net/netfilter/nf_conntrack_max")
r["conntrack"] = [int(c), int(m)] if c and m else None

# трафик основного интерфейса (сборщик сам посчитает скорость между замерами)
dev = sh("ip route show default | awk '{print $5; exit}'")
r["iface"] = dev
for line in (rd("/proc/net/dev", "") or "").splitlines():
    if line.strip().startswith(dev + ":"):
        f = line.split(":", 1)[1].split()
        r["rx_bytes"], r["tx_bytes"] = int(f[0]), int(f[8])

r["tcp_established"] = int(sh("ss -Htn state established | wc -l") or 0)

# службы
r["services"] = {s: sh(f"systemctl is-active {s}") for s in ("x-ui", "warp-svc", "caddy")}
r["services"] = {k: v for k, v in r["services"].items() if v and v != "inactive" or k == "x-ui"}
xv = re.search(r"Xray (\S+)", sh('for f in /usr/local/x-ui/bin/xray-linux-*; do [ -x "$f" ] && "$f" version && break; done'))
r["xray_version"] = xv.group(1) if xv else None

# инбаунды 3x-ui: порт, включён ли, «Мин. версия клиента» (урок Р-28), SNI
db = "/etc/x-ui/x-ui.db"
r["inbounds"] = []
certs = []
if os.path.exists(db):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        for port, remark, enable, proto, ss in con.execute(
                "select port, remark, enable, protocol, stream_settings from inbounds"):
            st = json.loads(ss or "{}")
            rs = st.get("realitySettings") or {}
            r["inbounds"].append({
                "port": port, "remark": remark, "enable": bool(enable), "proto": proto,
                "net": st.get("network"), "security": st.get("security"),
                "minClientVer": rs.get("minClientVer") if st.get("security") == "reality" else None,
                "sni": (rs.get("serverNames") or [None])[0] if rs else None,
            })
            # сертификат самого входа (Hysteria живёт на нём: истёк — узел погас, Р-52/Р-55)
            if enable:
                for c in (st.get("tlsSettings") or {}).get("certificates") or []:
                    if c.get("certificateFile"):
                        certs.append(c["certificateFile"])
        for k, v in con.execute("select key, value from settings where key in ('webCertFile','subCertFile')"):
            if v:
                certs.append(v)
        con.close()
    except Exception as e:
        r["db_error"] = str(e)[:120]

# сертификаты панели, подписки и входов: сколько дней осталось
r["certs"] = []
for p in sorted(set(certs)):
    end = sh(f"openssl x509 -enddate -noout -in '{p}' 2>/dev/null | cut -d= -f2")
    if end:
        ts = sh(f"date -d '{end}' +%s")
        if ts:
            r["certs"].append({"file": p, "days_left": round((int(ts) - time.time()) / 86400, 1)})

# выходы каскада: если на сервере работает наблюдатель Xray (балансировщик), его замеры
# лежат в метриках Xray (127.0.0.1:11111). Р-33: FORNEX-RU выбирает между NL и DE.
try:
    import urllib.request
    ob = json.load(urllib.request.urlopen("http://127.0.0.1:11111/debug/vars", timeout=3)).get("observatory") or {}
    # наблюдатель помнит замеры и удалённых выходов (до перезапуска) — берём только живые теги
    live = set(re.findall(r'"tag":\s*"([^"]+)"', sh(
        'for f in /usr/local/x-ui/bin/xray-linux-*; do [ -x "$f" ] && "$f" api lso --server=127.0.0.1:62789 && break; done')))
    # обратный туннель домой (reverse у клиента входа, Р-66) — не выход каскада: наблюдатель ненадолго
    # цепляет его, пока мост переподключается, и выходила ложная тревога «r-home не отвечает» (2026-09-25)
    rev = {"r-home"}
    try:
        for ib in json.load(open("/usr/local/x-ui/bin/config.json")).get("inbounds", []):
            for cl in (ib.get("settings") or {}).get("clients") or []:
                if (cl.get("reverse") or {}).get("tag"):
                    rev.add(cl["reverse"]["tag"])
    except Exception:
        pass
    if ob:
        r["exits"] = {k: {"alive": bool(v.get("alive")), "delay": v.get("delay")}
                      for k, v in ob.items() if (not live or k in live) and k not in rev}
except Exception:
    pass

# ошибки Xray за 15 минут (отказы рукопожатий и т.п.)
r["xray_errors_15m"] = int(sh("journalctl -u x-ui --since -15min --no-pager 2>/dev/null | grep -ciE 'failed|error'") or 0)

print(json.dumps(r, ensure_ascii=False))
