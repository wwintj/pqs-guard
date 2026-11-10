#!/usr/bin/env bash
# PQS IP 守护 —— 一键安装 / 升级 / 卸载 / 管理
#   bash <(curl -fsSL https://raw.githubusercontent.com/wwintj/pqs-guard/main/pqs-guard.sh)
#   安装后可直接运行: pqs-guard [install|upgrade|uninstall|config|status|log|restart|check]
set -euo pipefail

REPO="${PQS_GUARD_REPO:-wwintj/pqs-guard}"
BRANCH="${PQS_GUARD_BRANCH:-main}"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"

APP_DIR=/opt/pqs-guard
CONF_DIR=/etc/pqs-guard
CONF=$CONF_DIR/config.env
STATE_DIR=/var/lib/pqs-guard
SERVICE=/etc/systemd/system/pqs-guard.service
BIN=/usr/local/bin/pqs-guard
PY=$APP_DIR/venv/bin/python
PIP_PKGS=("python-telegram-bot>=21,<23" "httpx>=0.26")

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
info()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
die()   { red "✗ $*"; exit 1; }

# 兼容 curl | bash：交互输入从终端读
ask() {  # ask 变量名 提示 [默认值]
  local __v __p="$2" __d="${3:-}"
  [ -n "$__d" ] && __p="$__p [$__d]"
  read -r -p "$__p: " __v < /dev/tty || true
  printf -v "$1" '%s' "${__v:-$__d}"
}
confirm() {  # confirm 提示 默认(y/n)
  local a d="${2:-n}"
  read -r -p "$1 [$( [ "$d" = y ] && echo Y/n || echo y/N )]: " a < /dev/tty || true
  a="${a:-$d}"; [[ "$a" =~ ^[Yy] ]]
}

need_root() { [ "$EUID" -eq 0 ] || die "请用 root 运行"; }
is_installed() { [ -f "$APP_DIR/pqs_guard.py" ] && [ -f "$SERVICE" ]; }

fetch() {  # fetch 远程文件名 本地路径
  curl -fsSL --retry 3 -m 60 "$RAW/$1" -o "$2" || die "下载 $1 失败（$RAW/$1）"
}

set_conf() {  # set_conf KEY VALUE —— 原样写入，不做转义
  local tmp; tmp=$(mktemp "$CONF_DIR/.cfg.XXXXXX")
  K="$1" V="$2" awk 'BEGIN{k=ENVIRON["K"]; v=ENVIRON["V"]; done=0}
       index($0, k"=")==1 {print k"="v; done=1; next} {print}
       END{if(!done) print k"="v}' "$CONF" > "$tmp"
  chmod 600 "$tmp"; mv "$tmp" "$CONF"
}

pkg_install() {  # 安装失败只警告，不中断（老系统软件源可能已失效）
  local rc=0
  if command -v apt-get >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --fix-missing "$@" >/dev/null 2>&1 || rc=1
  elif command -v dnf >/dev/null; then
    dnf install -y -q "$@" >/dev/null 2>&1 || rc=1
  elif command -v yum >/dev/null; then
    yum install -y -q "$@" >/dev/null 2>&1 || rc=1
  else
    rc=1
  fi
  [ "$rc" = 0 ] || yellow "⚠ 软件包安装失败: $*（软件源可能失效，将尝试其他方式）"
}

has_ensurepip() { python3 -c 'import ensurepip, venv' >/dev/null 2>&1; }

install_deps() {
  info "检查系统依赖"
  local missing=()
  command -v python3 >/dev/null || missing+=(python3)
  command -v curl >/dev/null || missing+=(curl)
  if [ ${#missing[@]} -gt 0 ]; then
    pkg_install "${missing[@]}"
  fi
  command -v python3 >/dev/null || die "未找到 python3，且自动安装失败，请先修复软件源后手动安装 python3"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "需要 Python 3.9 及以上（当前 $(python3 -V 2>&1)）"
  if ! has_ensurepip && command -v apt-get >/dev/null; then
    pkg_install python3-venv
  fi
  green "✓ $(python3 -V 2>&1)"
  check_memory
}

check_memory() {
  local total avail swap
  total=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
  avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
  swap=$(awk '/^SwapTotal:/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
  info "内存: 总 ${total}MB，可用 ${avail}MB，swap ${swap}MB（本程序常驻约 40-60MB，已限制最多 150MB）"
  if [ "$avail" -gt 0 ] && [ "$avail" -lt 200 ] && [ "$swap" -eq 0 ]; then
    yellow "⚠ 可用内存不足 200MB 且没有 swap，安装依赖时可能被系统杀掉"
    if confirm "创建 512MB swap 文件 /swapfile？" y; then
      add_swap
    fi
  fi
}

add_swap() {
  [ -e /swapfile ] && { yellow "/swapfile 已存在，跳过"; return; }
  if fallocate -l 512M /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=512 status=none; then
    chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile \
      && { grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab; } \
      && green "✓ 已启用 512MB swap" || { yellow "启用 swap 失败（部分 VPS 不支持），继续安装"; rm -f /swapfile; }
  else
    yellow "创建 swap 失败，继续安装"; rm -f /swapfile
  fi
}

setup_venv() {
  info "安装 Python 依赖"
  if [ ! -x "$APP_DIR/venv/bin/pip" ]; then
    rm -rf "$APP_DIR/venv"
    if has_ensurepip && python3 -m venv "$APP_DIR/venv" >/dev/null 2>&1; then
      :
    else
      # 没有 ensurepip（如 Debian 未装 python3-venv 且软件源失效）：建空 venv + 官方 get-pip.py
      yellow "系统缺少 python3-venv，改用 get-pip.py 安装 pip"
      rm -rf "$APP_DIR/venv"
      python3 -m venv --without-pip "$APP_DIR/venv" || die "创建 venv 失败"
      local ver getpip
      ver=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
      getpip=$(mktemp)
      curl -fsSL --retry 3 -m 120 "https://bootstrap.pypa.io/pip/$ver/get-pip.py" -o "$getpip" 2>/dev/null \
        || curl -fsSL --retry 3 -m 120 "https://bootstrap.pypa.io/get-pip.py" -o "$getpip" \
        || die "下载 get-pip.py 失败"
      "$PY" "$getpip" -q || die "安装 pip 失败"
      rm -f "$getpip"
    fi
  fi
  # --no-cache-dir：不留下载缓存，省磁盘和内存
  "$PY" -m pip install -q --no-cache-dir --upgrade pip
  "$PY" -m pip install -q --no-cache-dir --upgrade "${PIP_PKGS[@]}" || die "安装 Python 依赖失败（内存不足时可先加 swap）"
  green "✓ Python 依赖就绪"
}

download_app() {  # 下载到临时目录，校验通过后再替换
  local tmp; tmp=$(mktemp -d)
  fetch pqs_guard.py "$tmp/pqs_guard.py"
  fetch pqs-guard.service "$tmp/pqs-guard.service"
  fetch config.env.example "$tmp/config.env.example"
  fetch pqs-guard.sh "$tmp/pqs-guard.sh"
  python3 -m py_compile "$tmp/pqs_guard.py" || die "下载的 pqs_guard.py 校验失败"
  bash -n "$tmp/pqs-guard.sh" || die "下载的 pqs-guard.sh 校验失败"
  mkdir -p "$APP_DIR" "$CONF_DIR"
  install -m 755 "$tmp/pqs_guard.py" "$APP_DIR/pqs_guard.py"
  install -m 644 "$tmp/config.env.example" "$APP_DIR/config.env.example"
  install -m 644 "$tmp/pqs-guard.service" "$SERVICE"
  install -m 755 "$tmp/pqs-guard.sh" "$BIN"
  rm -rf "$tmp"
  systemctl daemon-reload
}

stop_old_scripts() {
  local pat='ipc\.sh|nf\.sh|cf-ddns|cf-v4-ddns|bot\.py'
  if pgrep -f 'nf\.sh|bot\.py' >/dev/null 2>&1 || crontab -l 2>/dev/null | grep -Eq "$pat"; then
    yellow "发现旧的 nf.sh / ipc.sh / bot.py / cf-ddns 进程或定时任务，它们会和本程序抢着换 IP"
    if confirm "停止旧进程并从 crontab 移除旧任务？" y; then
      pkill -f 'nf\.sh' 2>/dev/null || true
      pkill -f 'bot\.py' 2>/dev/null || true
      if crontab -l 2>/dev/null | grep -Eq "$pat"; then
        local bak; bak=/root/crontab.bak.$(date +%Y%m%d%H%M%S)
        crontab -l > "$bak"
        crontab -l | grep -Ev "$pat" | crontab - || true
        green "已清理 crontab（备份: $bak）"
      fi
    fi
  fi
}

tg_check() {  # 校验 token，输出机器人用户名
  curl -fsS -m 15 "https://api.telegram.org/bot$1/getMe" 2>/dev/null \
    | grep -o '"username":"[^"]*"' | cut -d'"' -f4
}

configure() {
  info "填写配置（直接回车使用括号里的默认值）"
  local v name token ids url mode
  ask name "服务器名称（会显示在通知里）" "$(hostname)"
  set_conf SERVER_NAME "$name"

  while :; do
    ask token "Telegram 机器人 token（@BotFather 获取）"
    [ -n "$token" ] || { red "不能为空"; continue; }
    if v=$(tg_check "$token") && [ -n "$v" ]; then green "✓ 机器人 @$v"; break; fi
    red "token 无效或无法连接 Telegram"
    confirm "仍然使用这个 token？" n && break
  done
  set_conf TG_BOT_TOKEN "$token"

  while :; do
    ask ids "允许控制的 Telegram 用户 ID（@userinfobot 查询，多个用逗号分隔）"
    [[ "$ids" =~ ^-?[0-9]+(,-?[0-9]+)*$ ]] && break
    red "格式不对，应为数字，如 123456789"
  done
  set_conf TG_ALLOWED_IDS "$ids"

  while :; do
    ask url "PQS 换 IP 接口 URL（PQS 后台生成）"
    [[ "$url" =~ ^https?:// ]] && break
    red "请输入以 http:// 或 https:// 开头的地址"
  done
  set_conf PQS_CHANGE_IP_URL "$url"

  echo "DDNS（换 IP 后自动更新域名）:"
  echo "  1) 不需要 / 已使用 PQS 生成的 DDNS 域名"
  echo "  2) Cloudflare（API Token）"
  echo "  3) 自定义 URL（换 IP 后访问一次，可含 {ip}）"
  ask mode "请选择" 1
  case "$mode" in
    2)
      set_conf DDNS_MODE cloudflare
      ask v "Cloudflare API Token（只需 Zone.DNS:Edit 权限）"; set_conf CF_API_TOKEN "$v"
      ask v "根域名，如 example.com"; set_conf CF_ZONE_NAME "$v"
      ask v "要更新的完整域名，如 node.example.com"; set_conf CF_RECORD_NAME "$v"
      ;;
    3)
      set_conf DDNS_MODE url
      ask v "DDNS URL"; set_conf DDNS_URL "$v"
      ;;
    *) set_conf DDNS_MODE none ;;
  esac

  if confirm "Netflix 必须能看非自制剧？（否 = 能看自制剧就行）" y; then
    set_conf NF_REQUIRE full
  else
    set_conf NF_REQUIRE originals
  fi
  green "配置已保存到 $CONF（其余高级选项可用 pqs-guard config 修改）"
}

merge_new_keys() {  # 升级时把新版本新增的配置项补进旧配置
  local k line added=0
  while IFS= read -r line; do
    [[ "$line" =~ ^([A-Z_]+)= ]] || continue
    k="${BASH_REMATCH[1]}"
    if ! grep -q "^$k=" "$CONF"; then
      printf '\n# 升级时新增\n%s\n' "$line" >> "$CONF"; added=1
      yellow "新增配置项: $line"
    fi
  done < "$APP_DIR/config.env.example"
}

self_check() {
  info "自检（只检测，不换 IP）"
  "$PY" "$APP_DIR/pqs_guard.py" --env "$CONF" --check || yellow "自检有异常，请看上面的输出"
}

start_service() {
  systemctl enable pqs-guard >/dev/null 2>&1
  systemctl restart pqs-guard
  sleep 3
  if systemctl is-active --quiet pqs-guard; then
    green "✓ pqs-guard 运行中（版本 $("$PY" "$APP_DIR/pqs_guard.py" --version)），Telegram 应该已收到启动通知"
  else
    red "✗ 启动失败，最近日志："
    journalctl -u pqs-guard -n 30 --no-pager
    exit 1
  fi
}

do_install() {
  need_root
  command -v systemctl >/dev/null || die "需要 systemd"
  if is_installed; then
    yellow "已经安装过了"
    confirm "改为执行升级？" y && { do_upgrade; return; }
    return
  fi
  install_deps
  stop_old_scripts
  info "下载程序"
  download_app
  setup_venv
  mkdir -p "$STATE_DIR"
  if [ -f "$CONF" ] && ! confirm "发现已有配置 $CONF，重新填写？" n; then
    merge_new_keys
  else
    install -m 600 "$APP_DIR/config.env.example" "$CONF"
    configure
  fi
  chmod 600 "$CONF"
  self_check
  start_service
  echo
  green "安装完成！以后管理用命令: pqs-guard"
}

do_upgrade() {
  need_root
  is_installed || die "尚未安装，请先安装"
  local old new
  old=$("$PY" "$APP_DIR/pqs_guard.py" --version 2>/dev/null || echo "?")
  new=$(curl -fsSL -m 30 "$RAW/pqs_guard.py" | grep -m1 '^__version__' | cut -d'"' -f2 || true)
  info "当前版本 $old，最新版本 ${new:-未知}"
  if [ -n "$new" ] && [ "$old" = "$new" ] && ! confirm "已是最新，仍要重新安装？" n; then
    return
  fi
  cp -a "$APP_DIR/pqs_guard.py" "$APP_DIR/pqs_guard.py.bak"
  download_app
  setup_venv
  merge_new_keys
  start_service
  green "升级完成（旧程序备份在 $APP_DIR/pqs_guard.py.bak）"
}

do_uninstall() {
  need_root
  confirm "确定卸载 pqs-guard？" n || return
  systemctl disable --now pqs-guard 2>/dev/null || true
  rm -f "$SERVICE"
  systemctl daemon-reload
  rm -rf "$APP_DIR"
  if confirm "同时删除配置（含 token）和换 IP 记录？" y; then
    rm -rf "$CONF_DIR" "$STATE_DIR"
  else
    yellow "保留了 $CONF_DIR 和 $STATE_DIR"
  fi
  rm -f "$BIN"
  green "已卸载"
}

do_config() {
  need_root; [ -f "$CONF" ] || die "配置文件不存在，请先安装"
  echo "  1) 重新走一遍配置向导"
  echo "  2) 用编辑器直接编辑（全部选项）"
  local c; ask c "请选择" 1
  if [ "$c" = 2 ]; then
    "${EDITOR:-$(command -v nano || command -v vi)}" "$CONF" < /dev/tty
  else
    configure
  fi
  chmod 600 "$CONF"
  start_service
}

do_status() { systemctl --no-pager status pqs-guard | head -n 12 || true; }
do_log()    { journalctl -u pqs-guard -n 100 -f; }
do_check()  { need_root; is_installed || die "尚未安装"; self_check; }

menu() {
  echo
  echo "================ PQS IP 守护 ================"
  if is_installed; then
    echo " 状态: $(systemctl is-active pqs-guard 2>/dev/null || true)  版本: $("$PY" "$APP_DIR/pqs_guard.py" --version 2>/dev/null || echo ?)"
  else
    echo " 状态: 未安装"
  fi
  echo "---------------------------------------------"
  echo "  1) 安装"
  echo "  2) 升级"
  echo "  3) 卸载"
  echo "  4) 修改配置"
  echo "  5) 查看状态"
  echo "  6) 查看日志"
  echo "  7) 重启"
  echo "  8) 立即检测一次（不换 IP）"
  echo "  0) 退出"
  local c; ask c "请选择" 0
  case "$c" in
    1) do_install ;; 2) do_upgrade ;; 3) do_uninstall ;; 4) do_config ;;
    5) do_status ;; 6) do_log ;; 7) need_root; start_service ;; 8) do_check ;;
    *) exit 0 ;;
  esac
}

case "${1:-}" in
  install)   do_install ;;
  upgrade|update) do_upgrade ;;
  uninstall|remove) do_uninstall ;;
  config)    do_config ;;
  status)    do_status ;;
  log|logs)  do_log ;;
  restart)   need_root; start_service ;;
  check)     do_check ;;
  "")        menu ;;
  *) echo "用法: pqs-guard [install|upgrade|uninstall|config|status|log|restart|check]"; exit 1 ;;
esac
