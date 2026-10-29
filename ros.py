"""Мелкий ssh-хелпер для микротика

python ros.py "/system resource print"   выполнить команду
python ros.py --import file.rsc          залить .rsc и сделать /import
python ros.py --put local [remote]       залить файл
python ros.py --get remote [local]       скачать файл

Доступ берётся из .env рядом (ROS_HOST, ROS_USER, ROS_PASS), пример в .env.example
Ключ роутера должен быть в ~/.ssh/known_hosts, для этого один раз зайди руками: ssh admin@192.168.88.1
"""
import os
import sys
from pathlib import Path

import paramiko

HERE = Path(__file__).parent


def load_env():
    env = {
        "ROS_HOST": "192.168.88.1", "ROS_USER": "admin", "ROS_PASS": "",
        "ROS_USB": "", "ROS_IMAGE": "drkivi/amneziawg-for-armv7:v0.0.20261026",
    }
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("ROS_")})
    return env


def connect():
    env = load_env()
    c = paramiko.SSHClient()
    # ключ роутера берём из ~/.ssh/known_hosts, незнакомый ключ не пускаем
    c.load_system_host_keys()
    c.set_missing_host_key_policy(paramiko.RejectPolicy())
    c.connect(env["ROS_HOST"], username=env["ROS_USER"], password=env["ROS_PASS"],
              look_for_keys=False, allow_agent=False, timeout=10)
    return c


def run(c, cmd, timeout=120):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    text = out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")
    return text


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    c = connect()
    try:
        if args[0] == "--put":
            remote = args[2] if len(args) > 2 else Path(args[1]).name
            c.open_sftp().put(args[1], remote)
        elif args[0] == "--get":
            local = args[2] if len(args) > 2 else Path(args[1]).name
            c.open_sftp().get(args[1], local)
        elif args[0] == "--import":
            name = Path(args[1]).name
            c.open_sftp().put(args[1], name)
            print(run(c, f"/import file-name={name} verbose=yes"))
            run(c, f"/file remove [find name={name}]")
        else:
            print(run(c, " ".join(args)))
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
