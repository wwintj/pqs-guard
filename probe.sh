#!/bin/bash
# pqs-guard 家庭探针 —— 放在国内常开的设备上（unraid / NAS / 路由器 / 电脑）
# 每次运行：向服务器要"当前 IP + 节点端口" → 从本地连一遍 → 把结果报回去
#
# 用法（四个参数依次是：探针名 服务器域名 探针端口 密钥，都在服务器上运行 pqs-guard probe 时给出）：
#   bash pqs-probe.sh 浙江 tw.example.com 58080 密钥          # 跑一次（配合 cron，每 3 分钟）
#   bash pqs-probe.sh 浙江 tw.example.com 58080 密钥 --loop   # 常驻循环，每 3 分钟一次
# 也可以用环境变量 PROBE_NAME / PROBE_SERVER / PROBE_PORT / PROBE_KEY / PROBE_LOOP=1 代替参数（Docker 方式）。
# 只依赖 bash 和 curl。

NAME="${1:-${PROBE_NAME:-}}"
SERVER="${2:-${PROBE_SERVER:-}}"
PORT="${3:-${PROBE_PORT:-58080}}"
KEY="${4:-${PROBE_KEY:-}}"
LOOP="${PROBE_LOOP:-0}"; [ "${5:-}" = "--loop" ] && LOOP=1
INTERVAL="${PROBE_INTERVAL:-180}"
CACHE="${PROBE_CACHE:-/tmp/pqs-probe-$(printf '%s' "$NAME" | { md5sum 2>/dev/null || md5 2>/dev/null; } | cut -c1-8).cache}"

[ -n "$NAME" ] && [ -n "$SERVER" ] && [ -n "$KEY" ] || { echo "用法: $0 探针名 服务器域名 探针端口 密钥 [--loop]"; exit 1; }
command -v curl >/dev/null || { echo "需要 curl"; exit 1; }

log() { printf '%s %s\n' "$(date '+%m-%d %H:%M:%S')" "$*"; }

tcp_check() {  # tcp_check IP 端口 → 0 通 / 1 不通
  if command -v timeout >/dev/null 2>&1; then
    timeout 5 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null && return 0
    return 1
  fi
  if command -v nc >/dev/null 2>&1; then
    nc -z -w 5 "$1" "$2" >/dev/null 2>&1 && return 0
    nc -z -G 5 "$1" "$2" >/dev/null 2>&1 && return 0   # macOS
    return 1
  fi
  # 没有 timeout 也没有 nc：裸 /dev/tcp + 后台看门狗
  ( exec 3<>/dev/tcp/"$1"/"$2" ) 2>/dev/null & local pid=$!
  ( sleep 5; kill "$pid" 2>/dev/null ) & local dog=$!
  wait "$pid" 2>/dev/null; local rc=$?
  kill "$dog" 2>/dev/null; wait "$dog" 2>/dev/null
  return $rc
}

resolve_doh() {  # 用阿里 DoH 解析域名（比运营商 DNS 更新得快）
  curl -fsS -m 8 "https://dns.alidns.com/resolve?name=$SERVER&type=A" 2>/dev/null \
    | grep -o '"data":"[0-9.]*"' | head -1 | cut -d'"' -f4
}

# 依次尝试：上次成功的 IP → 域名 → DoH 解析到的 IP
candidate_hosts() {
  local cached=""
  [ -f "$CACHE" ] && cached=$(sed -n '1p' "$CACHE" | awk '{print $1}')
  [ -n "$cached" ] && echo "$cached"
  echo "$SERVER"
  local doh; doh=$(resolve_doh)
  [ -n "$doh" ] && [ "$doh" != "$cached" ] && echo "$doh"
}

run_once() {
  local host targets="" report_host="" code
  for host in $(candidate_hosts); do
    code=$(curl -sS -m 8 -o "$CACHE.tmp" -w '%{http_code}' -G "http://${host}:${PORT}/targets" \
      --data-urlencode "key=$KEY" --data-urlencode "name=$NAME" 2>/dev/null)
    if [ "$code" = 403 ]; then log "密钥错误（服务器返回 403），请核对第 4 个参数"; rm -f "$CACHE.tmp"; return 1; fi
    if [ "$code" = 200 ] && [ -s "$CACHE.tmp" ]; then targets=$(sed -n '1p' "$CACHE.tmp"); report_host=$host; break; fi
  done
  rm -f "$CACHE.tmp"
  if [ -n "$targets" ]; then
    printf '%s\n' "$targets" > "$CACHE"
  elif [ -f "$CACHE" ]; then
    targets=$(sed -n '1p' "$CACHE")
    log "连不上服务器，使用缓存目标: $targets"
  else
    log "连不上服务器，且无缓存，跳过"
    return 1
  fi

  local ip; ip=$(printf '%s' "$targets" | awk '{print $1}')
  local ports; ports=$(printf '%s' "$targets" | cut -d' ' -f2-)
  [ -n "$ip" ] && [ -n "$ports" ] || { log "目标格式不对: $targets"; return 1; }

  local r="" p
  for p in $ports; do
    if tcp_check "$ip" "$p"; then r="$r,$p:ok"; else r="$r,$p:fail"; fi
  done
  r=${r#,}
  log "测 $ip → $r"

  local sent=0
  for host in ${report_host:+$report_host} $(candidate_hosts); do
    curl -fsS -m 8 -X POST "http://${host}:${PORT}/report" \
      --data-urlencode "key=$KEY" --data-urlencode "name=$NAME" \
      --data-urlencode "ip=$ip" --data-urlencode "r=$r" >/dev/null 2>&1 && { sent=1; break; }
  done
  if [ "$sent" = 1 ]; then log "已上报（经 ${host}）"; else log "上报失败：服务器不可达（若持续如此，可能是 IP 被墙）"; fi
}

if [ "$LOOP" = 1 ]; then
  log "探针 [$NAME] 启动，每 $INTERVAL 秒一次，服务器 $SERVER:$PORT"
  while true; do run_once; sleep "$INTERVAL"; done
else
  run_once
fi
