"""Стресс-тест: долгие закачки в 1, 4 и 8 потоков напрямую и через vpn, плюс cpu и память роутера

python stresstest.py [секунд на сценарий]   по умолчанию 60

Запускать с компа в локалке роутера, нужен curl (в Windows 10+ и Linux он есть)
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time

from ros import connect, run

HOST = "proof.ovh.net"
URL = f"https://{HOST}/files/100Mb.dat"
# через этот хост только смотрим, с какого ip выходим
TRACE = "speed.cloudflare.com"
TAG = "vpn-ai"
WIN = sys.platform == "win32"


def sample(c):
    out = run(c, ':local r [/system resource get]; :local k [/container get [find comment="vpn-ai"]]; '
                 ':put (($r->"cpu-load") . " " . ($r->"free-memory") . " " . ($k->"memory-current"))').split()
    num = [int(x) if x.isdigit() else 0 for x in out + ["0"] * 3]
    return num[0], num[1] / 2**20, num[2] / 2**20


def exit_ip():
    out = subprocess.run(["curl", "-s", "-m", "10", f"https://{TRACE}/cdn-cgi/trace"],
                         capture_output=True, text=True, env=no_proxy()).stdout
    return next((ln[3:] for ln in out.splitlines() if ln.startswith("ip=")), "?")


def no_proxy():
    # системный прокси увёл бы закачку мимо роутера
    return {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}


def router_ping(host):
    cmd = ["ping", "-n", "1", "-w", "1000", host] if WIN else ["ping", "-c", "1", "-W", "1", host]
    out = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stdout
    m = re.search(r"[=<]\s*([\d.]+)\s*(ms|мс)", out)
    return float(m.group(1)) if m else 1000


def worker(deadline, total, lock):
    while time.time() < deadline:
        left = max(1, int(deadline - time.time()))
        res = subprocess.run(["curl", "-s", "-o", os.devnull, "-m", str(left), "-w", "%{size_download}", URL],
                             capture_output=True, text=True, env=no_proxy())
        with lock:
            total[0] += int(float(res.stdout or 0))


def scenario(c, host, label, threads, secs):
    total, lock, rows, pings = [0], threading.Lock(), [], []
    deadline = time.time() + secs
    ws = [threading.Thread(target=worker, args=(deadline, total, lock)) for _ in range(threads)]
    for w in ws:
        w.start()
    while time.time() < deadline:
        rows.append(sample(c))
        pings.append(router_ping(host))
        time.sleep(2)
    for w in ws:
        w.join()
    cpu = [r[0] for r in rows]
    print(f"{label:<16} {total[0] * 8 / secs / 1e6:6.1f} Мбит/с | cpu ср {sum(cpu) / len(cpu):3.0f}% "
          f"макс {max(cpu):3}% | ram мин {min(r[1] for r in rows):5.1f} MB | "
          f"контейнер макс {max(r[2] for r in rows):5.1f} MB | "
          f"пинг роутера ср {sum(pings) / len(pings):4.0f} мс макс {max(pings):.0f} мс", flush=True)


def main():
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    sys.stdout.reconfigure(encoding="utf-8")
    c = connect()
    host = c.get_transport().getpeername()[0]
    cpu, free, cont = sample(c)
    print(f"в покое: cpu {cpu}% | ram {free:.1f} MB | контейнер {cont:.1f} MB\n")
    try:
        print("выход напрямую:", exit_ip())
        scenario(c, host, "напрямую x4", 4, secs)
        # адреса кладём в список статически: записи из dns живут по ttl, а комп
        # может переспросить dns мимо роутера, и закачка тихо уйдёт напрямую
        ips = {a[4][0] for h in (HOST, TRACE) for a in socket.getaddrinfo(h, 443, socket.AF_INET)}
        for ip in ips:
            run(c, f'/ip firewall address-list add list={TAG} address={ip} comment="{TAG} stresstest"')
        time.sleep(3)
        print("выход через vpn:", exit_ip())
        for n in (1, 4, 8):
            scenario(c, host, f"vpn x{n}", n, secs)
        print("выход в конце:", exit_ip())
    finally:
        run(c, f'/ip firewall address-list remove [find comment="{TAG} stresstest"]')
        time.sleep(20)
        cpu, free, cont = sample(c)
        print(f"\nпосле: cpu {cpu}% | ram {free:.1f} MB | контейнер {cont:.1f} MB")
        print(run(c, '/log print without-paging where topics~"container" and message~"memory|Killed"').strip()[-600:])
        c.close()


if __name__ == "__main__":
    main()
