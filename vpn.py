"""Домены из domains.txt через AmneziaWG на микротике

python vpn.py prepare     подготовить роутер: пакет container, device-mode, образ диска на флешке
python vpn.py install     поставить всё: контейнер с awg, маршруты, домены
python vpn.py conf        перезалить конфиги из servers/ и перезапустить контейнер
python vpn.py sync        залить domains.txt на роутер
python vpn.py off         выключить, всё ходит напрямую, контейнер стоит
python vpn.py on          включить обратно
python vpn.py status      что сейчас происходит
python vpn.py uninstall   снести всё, что ставил install

Сервера берутся из servers/awg-*.conf по алфавиту, первый основной, остальные запасные
"""
import sys
import tempfile
import time
from pathlib import Path

from ros import connect, load_env, run

HERE = Path(__file__).parent
ENV = load_env()
TAG = "vpn-ai"
TABLE = "vpn-ai"
IFLIST = "VPN-AI"
DNS = "1.1.1.1"
IMAGE = ENV["ROS_IMAGE"]
DISK = "vpnimg"
CONF_DIR = f"{DISK}/awg/conf"
VETH, ROUTER_IP, CONT_IP = "veth-awg", "172.18.0.1", "172.18.0.2"

# всё, что включается и выключается через on/off
MENUS = ["/ip firewall mangle", "/ip firewall filter", "/ip firewall nat", "/ip route", "/ip dns static"]


def awg_confs():
    """servers/awg-*.conf превращает в awg1.conf, awg2.conf... для контейнера"""
    out = {}
    for i, f in enumerate(sorted((HERE / "servers").glob("awg-*.conf")), 1):
        lines = []
        for ln in f.read_text(encoding="utf-8").splitlines():
            # dns и маршруты рулит start.sh, awg-quick туда лезть не должен
            if ln.strip().lower().startswith("dns"):
                continue
            lines.append(ln)
            if ln.strip() == "[Interface]":
                lines += ["Table = off", "MTU = 1280"]
        out[f"awg{i}.conf"] = "\n".join(lines) + "\n"
    return out


def domains():
    lines = (HERE / "domains.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def is_off(c):
    out = run(c, f':put [/ip firewall mangle get [find comment="{TAG} mark-conn"] disabled]')
    return out.strip() == "true"


def domains_rsc(disabled):
    rsc = [
        f'/ip dns static remove [find comment="{TAG} domain"]',
        f'/ip firewall address-list remove [find list={TAG} comment="{TAG} ip"]',
    ]
    for d in domains():
        # подсеть из domains.txt кладём в список сразу, без dns
        if d[0].isdigit():
            rsc.append(f'/ip firewall address-list add list={TAG} address={d} comment="{TAG} ip"')
            continue
        rsc.append(
            f"/ip dns static add name={d} type=FWD forward-to={DNS} match-subdomain=yes "
            f'address-list={TAG} comment="{TAG} domain" disabled={"yes" if disabled else "no"}'
        )
    # ответы из кэша в address-list не попадают, поэтому кэш сбрасываем
    return rsc + ["/ip dns cache flush"]


def install_rsc():
    rsc = [
        f"/routing table add name={TABLE} fib",
        f"/interface list add name={IFLIST}",
        f"/interface veth add name={VETH} address={CONT_IP}/30 gateway={ROUTER_IP} comment={TAG}",
        f"/ip address add address={ROUTER_IP}/30 interface={VETH} comment={TAG}",
        f"/interface list member add list={IFLIST} interface={VETH} comment={TAG}",
        f"/container mounts add list={TAG} src=/{CONF_DIR} dst=/etc/amnezia/amneziawg comment={TAG}",
        f"/container config set registry-url=https://registry-1.docker.io tmpdir={DISK}/tmp",
        f"/container add name={TAG} remote-image={IMAGE} interface={VETH} root-dir={DISK}/awg/root "
        f'mountlists={TAG} cmd="/bin/bash /etc/amnezia/amneziawg/start.sh" dns={DNS} '
        # memory-max: роутеру самому нужно ~80 MB из 128, контейнеру больше не даём
        f"start-on-boot=yes logging=yes restart-policy=always memory-max=32M swap-max=1 comment={TAG}",
        # swap для самой RouterOS, контейнеру он запрещён (swap-max=1): выгруженный на флешку
        # amneziawg-go замирает, пусть лучше упрётся в лимит и перезапустится
        f"/disk add type=file file-path={DISK}/swap.img file-size=256M swap=yes slot=swapimg",
        # контейнер жив, значит он сам выберет туннель, иначе blackhole и мимо vpn ничего не уходит
        f"/ip route add dst-address=0.0.0.0/0 gateway={CONT_IP} check-gateway=ping distance=1 "
        f"routing-table={TABLE} comment={TAG}",
        f"/ip route add dst-address=0.0.0.0/0 blackhole distance=100 routing-table={TABLE} comment={TAG}",
        f"/ip firewall mangle add chain=prerouting in-interface-list=LAN dst-address-list={TAG} "
        f'connection-mark=no-mark action=mark-connection new-connection-mark={TAG} comment="{TAG} mark-conn"',
        f"/ip firewall mangle add chain=prerouting in-interface-list=LAN connection-mark={TAG} "
        f'action=mark-routing new-routing-mark={TABLE} passthrough=no comment="{TAG} mark-route"',
        # у кого в системе прописан чужой dns (8.8.8.8 и т.п.), всё равно резолвят через роутер,
        # иначе роутер не видит домены и не кладёт их адреса в список
        f"/ip firewall nat add chain=dstnat in-interface-list=LAN dst-port=53 protocol=udp "
        f'dst-address=!{ROUTER_IP} action=redirect to-ports=53 comment="{TAG} dns"',
        f"/ip firewall nat add chain=dstnat in-interface-list=LAN dst-port=53 protocol=tcp "
        f'dst-address=!{ROUTER_IP} action=redirect to-ports=53 comment="{TAG} dns"',
        # fasttrack пропускает mangle, поэтому помеченные соединения в него не пускаем
        f"/ip firewall filter add chain=forward connection-mark={TAG} action=accept "
        f'comment="{TAG} no fasttrack" place-before=[find comment="defconf: fasttrack"]',
    ]
    return rsc + domains_rsc(False)


def uninstall_rsc():
    rsc = [
        f'/container stop [find comment="{TAG}"]',
        ":delay 5s",
        f'/container remove [find comment="{TAG}"]',
        f'/container mounts remove [find comment="{TAG}"]',
        "/disk remove [find slot=swapimg]",
    ]
    rsc += [f'{m} remove [find where comment~"^{TAG}"]' for m in MENUS]
    rsc += [
        f'/interface list member remove [find where comment~"^{TAG}"]',
        f'/ip address remove [find where comment~"^{TAG}"]',
        f'/interface veth remove [find where comment~"^{TAG}"]',
        f"/interface list remove [find name={IFLIST}]",
        f"/routing table remove [find name={TABLE}]",
        f"/ip firewall address-list remove [find list={TAG}]",
    ]
    return rsc


def toggle_rsc(off):
    v = "yes" if off else "no"
    rsc = [f'{m} set [find where comment~"^{TAG}"] disabled={v}' for m in MENUS]
    if off:
        rsc += [
            f"/ip firewall connection remove [find connection-mark={TAG}]",
            f'/container stop [find comment="{TAG}"]',
        ]
    else:
        rsc += [f'/container start [find comment="{TAG}"]', "/ip dns cache flush"]
    return rsc


def apply(c, rsc):
    # в rsc бывают ключи, поэтому кладём не в папку с репой, а во временную
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{TAG}.rsc"
        path.write_text("\n".join(rsc) + "\n", encoding="utf-8")
        c.open_sftp().put(str(path), path.name)
        out = run(c, f"/import file-name={path.name}")
        run(c, f"/file remove [find name={path.name}]")
    print(out.strip() or "ok")


def upload_confs(c):
    run(c, f"/file add name={DISK}/awg type=directory; /file add name={CONF_DIR} type=directory")
    sftp = c.open_sftp()
    with sftp.open(f"{CONF_DIR}/start.sh", "w") as f:
        f.write((HERE / "awg" / "start.sh").read_text(encoding="utf-8").replace("\r\n", "\n"))
    for name, text in awg_confs().items():
        with sftp.open(f"{CONF_DIR}/{name}", "w") as f:
            f.write(text)


def prepare(c):
    """по шагам, каждый вызов делает следующий шаг, после ребута запускать ещё раз"""
    ver = run(c, ":put [/system resource get version]").split()[0]
    if tuple(int(x) for x in ver.split(".")[:2]) < (7, 15):
        print(f"нужен RouterOS 7.15+, сейчас {ver}. С 6.x обновляться через канал upgrade, см. README")
        return
    if run(c, ":put [:len [/system package find name=container]]").strip() == "0":
        arch = run(c, ":put [/system resource get architecture-name]").strip()
        print(run(c, f"/tool fetch url=https://download.mikrotik.com/routeros/{ver}/container-{ver}-{arch}.npk"))
        print("пакет container скачан, роутер уходит в ребут. Через 2-3 минуты запусти prepare ещё раз")
        run(c, "/system reboot")
        return
    if run(c, ":put [/system device-mode get container]").strip() != "true":
        print("в течение 5 минут коротко нажми reset на роутере (нажать и отпустить, не держать)")
        print("без этого RouterOS контейнеры не включит, после нажатия роутер перезагрузится")
        _, out, _ = c.exec_command("/system device-mode update container=yes", timeout=400)
        for _ in iter(out.readline, ""):
            pass
        print("через 2-3 минуты запусти prepare ещё раз")
        return
    if run(c, f':put [:len [/disk find slot={DISK}]]').strip() == "0":
        usb = ENV["ROS_USB"] or find_usb(c)
        if not usb:
            return
        print(f"создаю {usb}/mikrotik/vpn.img на 1 GB и форматирую в ext4, остальное на флешке не трогается")
        run(c, f"/file add name={usb}/mikrotik type=directory")
        run(c, f"/disk add type=file file-path={usb}/mikrotik/vpn.img file-size=1G slot={DISK}", timeout=600)
        time.sleep(5)
        run(c, f"/disk format {DISK} file-system=ext4 label=vpn mbr-partition-table=no", timeout=600)
    print(run(c, f"/disk print where slot={DISK}").strip())
    if not run(c, ":put [/ip dns get servers]").strip():
        print("\nроутер берёт DNS у провайдера, лучше поставить публичный, см. шаг 2 в README:")
        print("/ip dhcp-client set [find interface=ether1] use-peer-dns=no")
        print("/ip dns set servers=1.1.1.1,8.8.8.8")
    print("роутер готов, дальше python vpn.py install")


def find_usb(c):
    slots = run(c, ':foreach d in=[/disk find where mounted and type!=file] do={:put [/disk get $d slot]}')
    slots = [s for s in slots.split() if s != DISK]
    if len(slots) == 1:
        return slots[0]
    if not slots:
        print("флешка не найдена: вставь её в USB роутера, подойдёт любая от 2 GB (ext4, fat32 или ntfs)")
    else:
        print(f"дисков несколько ({', '.join(slots)}), укажи нужный в .env: ROS_USB=usb1-part1")
    return ""


def container_state(c):
    # отдельного статуса в 7.25 нет: stopped=true у остановленного,
    # у запущенного есть memory-current, иначе образ ещё качается
    get = f'/container get [find comment="{TAG}"]'
    if run(c, f":put [{get} stopped]").strip() == "true":
        return "stopped"
    if run(c, f":put [{get} memory-current]").strip():
        return "running"
    return "extracting"


def wait_and_start(c):
    for _ in range(120):
        state = container_state(c)
        if state in ("stopped", "running"):
            break
        print("контейнер:", state or "?")
        time.sleep(10)
    run(c, f'/container start [find comment="{TAG}"]')
    print("контейнер:", container_state(c))


def status(c):
    print("выключен" if is_off(c) else "включён")
    print("контейнер:", container_state(c))
    print(run(c, f"/ip route print where routing-table={TABLE}"))
    print(run(c, f'/log print without-paging where topics~"container"').strip()[-1500:])
    print(run(c, f':put ("адресов в списке: " . [:len [/ip firewall address-list find list={TAG}]])'))
    print(run(c, "/system resource print").strip())


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.stdout.reconfigure(encoding="utf-8")
    c = connect()
    try:
        if cmd == "prepare":
            prepare(c)
        elif cmd == "install":
            if run(c, f":put [:len [/disk find slot={DISK}]]").strip() == "0":
                print("нет диска на флешке, сначала python vpn.py prepare")
                return 1
            upload_confs(c)
            apply(c, install_rsc())
            wait_and_start(c)
        elif cmd == "conf":
            upload_confs(c)
            run(c, f'/container stop [find comment="{TAG}"]')
            time.sleep(5)
            wait_and_start(c)
        elif cmd == "sync":
            apply(c, domains_rsc(is_off(c)))
        elif cmd in ("on", "off"):
            apply(c, toggle_rsc(cmd == "off"))
        elif cmd == "uninstall":
            apply(c, uninstall_rsc())
        elif cmd == "status":
            status(c)
        else:
            print(__doc__)
            return 1
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
