#!/bin/bash
# Запускается в контейнере вместо openrc
# Держит поднятым первый живой туннель из awg1, awg2..., остальные поднимает, только если он упал
D=/etc/amnezia/amneziawg
TABLE=200
CHECK=9.9.9.9
# GOMEMLIMIT не ставить: при нагрузке go уходит в бесконечную сборку мусора и туннель встаёт

[ -c /dev/net/tun ] || { mkdir -p /dev/net; mknod /dev/net/tun c 10 200; }
sysctl -qw net.ipv4.ip_forward=1
iptables -t nat -A POSTROUTING -o awg+ -j MASQUERADE
iptables -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
# если живых туннелей нет, отвечаем unreachable, а не пускаем мимо
ip route add unreachable default metric 100 table $TABLE
# трафик из локалки приходит через интерфейс, где висит default, на микротике это veth
LAN_IF=$(ip route show default | awk '{print $5; exit}')
ip rule add iif "$LAN_IF" lookup $TABLE priority 300

log() { echo "$(date '+%F %T') $*"; }

tunnels=$(ls $D/awg*.conf | xargs -n1 basename | sed 's/\.conf$//' | sort)
alive() { ping -c"$2" -W3 -I "$1" $CHECK >/dev/null 2>&1; }
# каждый туннель это отдельный amneziawg-go на ~5 MB, поэтому держим поднятым только текущий
up() { ip link show "$1" >/dev/null 2>&1 || awg-quick up "$D/$1.conf" >/dev/null 2>&1; }
down() { ip link show "$1" >/dev/null 2>&1 && awg-quick down "$D/$1.conf" >/dev/null 2>&1; }

use() {
    ip route replace default dev "$1" table $TABLE
    [ -n "$cur" ] && [ "$cur" != "$1" ] && down "$cur"
    cur=$1
    fails=0
    log "трафик идёт через $1"
}

# пробует туннели по порядку, начиная с первого, останавливаясь перед $1
try_from_top() {
    for t in $tunnels; do
        [ "$t" = "$1" ] && return 1
        if up "$t" && sleep 3 && alive "$t" 2; then use "$t"; return 0; fi
        down "$t"
    done
    return 1
}

cur=
fails=0
n=0
while true; do
    n=$((n + 1))
    if [ -z "$cur" ]; then
        try_from_top "" || { [ $((n % 6)) = 1 ] && log "все туннели лежат"; }
    else
        # забитый закачкой канал может потерять пинг, поэтому текущий туннель
        # бросаем только после 3 провалов подряд, это ~30 сек
        if alive "$cur" 2; then fails=0; else fails=$((fails + 1)); fi
        if [ $fails -ge 3 ]; then
            dead=$cur
            ip route del default dev "$dead" table $TABLE
            down "$dead"
            cur=
            log "$dead не отвечает"
            # сначала те, что ниже упавшего, потом с начала
            for t in $tunnels; do
                [ "$t" \> "$dead" ] || continue
                if up "$t" && sleep 3 && alive "$t" 2; then use "$t"; break; fi
                down "$t"
            done
            [ -z "$cur" ] && try_from_top ""
        elif [ $((n % 30)) = 0 ]; then
            # раз в 5 минут смотрим, не ожил ли кто-то главнее текущего
            try_from_top "$cur"
        fi
    fi
    sleep 10
done
