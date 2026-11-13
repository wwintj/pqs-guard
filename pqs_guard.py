#!/usr/bin/env python3
"""
PQS 服务器 IP 守护 + Telegram 控制机器人（单进程，systemd 常驻）

功能
  * Telegram 手动换 IP（带确认按钮）、查看状态、开关自动模式、查看换 IP 记录
  * 自动检测：被墙（国内 TCP 不可达）→ 换 IP
  * 自动检测：Netflix 不解锁非自制剧 → 换 IP
  * 换 IP 后等待新 IP 生效 → 自动更新 DDNS（Cloudflare / 自定义 URL）→ 复检并推送结果
  * 冷却时间、每日上限、并发锁，防止无限换 IP

用法
  python3 pqs_guard.py                 # 正常运行（读取环境变量，systemd 用 EnvironmentFile 注入）
  python3 pqs_guard.py --env FILE      # 从指定配置文件读取后运行
  python3 pqs_guard.py --env FILE --check   # 只做一次检测并打印结果，不启动机器人、不换 IP
"""

import argparse
import asyncio
import datetime as dt
import functools
import html
import ipaddress
import json
import logging
import os
import random
import socket
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

__version__ = "1.3.2"

log = logging.getLogger("pqs-guard")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
IP_SOURCES = ["https://api.ipify.org", "https://ipv4.icanhazip.com", "https://ifconfig.me/ip"]
NF_ORIGINAL = "81280792"  # Netflix 自制剧（所有可用地区都有）
NF_LICENSED = "70143836"  # 绝命毒师，非自制剧（只有"全解锁"IP 才能看）


# ───────────────────────────── 配置 ─────────────────────────────

def load_env_file(path):
    """简单的 KEY=VALUE 解析（与 systemd EnvironmentFile 兼容），不覆盖已有环境变量。"""
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


def _env(key, default=""):
    return os.environ.get(key, default).strip()


def _env_bool(key, default):
    return _env(key, str(default)).lower() in ("1", "true", "yes", "on")


def _env_list(key, default):
    return [x.strip() for x in _env(key, default).split(",") if x.strip()]


class Config:
    def __init__(self):
        self.server_name = _env("SERVER_NAME", socket.gethostname())
        self.tg_token = _env("TG_BOT_TOKEN")
        self.allowed_ids = [int(x) for x in _env_list("TG_ALLOWED_IDS", "")]
        self.notify_chat = int(_env("TG_NOTIFY_CHAT_ID") or (self.allowed_ids[0] if self.allowed_ids else 0))
        self.pqs_url = _env("PQS_CHANGE_IP_URL")

        self.auto_default = _env_bool("AUTO_CHANGE", True)
        self.check_gfw = _env_bool("CHECK_GFW", True)
        self.check_netflix = _env_bool("CHECK_NETFLIX", True)
        self.nf_require = _env("NF_REQUIRE", "full")  # full | originals
        self.interval = int(_env("CHECK_INTERVAL", "300"))
        self.fail_threshold = int(_env("FAIL_THRESHOLD", "2"))
        self.cooldown = int(_env("CHANGE_COOLDOWN", "600"))
        self.max_per_day = int(_env("MAX_CHANGES_PER_DAY", "8"))
        self.ip_wait = int(_env("IP_WAIT_TIMEOUT", "180"))

        self.cn_targets = _env_list(
            "CN_TARGETS", "114.114.114.114:53,119.29.29.29:53,180.76.76.76:53,223.5.5.5:53")
        self.intl_targets = _env_list("INTL_TARGETS", "1.1.1.1:443,8.8.8.8:443,9.9.9.9:443")

        self.ddns_mode = _env("DDNS_MODE", "none").lower()  # none | cloudflare | url
        self.ddns_url = _env("DDNS_URL")  # 可含 {ip} 占位符
        self.cf_token = _env("CF_API_TOKEN")
        self.cf_email = _env("CF_EMAIL")
        self.cf_key = _env("CF_GLOBAL_KEY")
        self.cf_zone = _env("CF_ZONE_NAME")
        self.cf_record = _env("CF_RECORD_NAME")

        # 家庭探针：国内设备定时连接节点端口并上报，用来发现"只封端口"和线路问题
        self.probe_port = int(_env("PROBE_PORT", "0") or 0)  # 0 = 不启用
        self.probe_key = _env("PROBE_KEY")
        self.probe_names = _env_list("PROBE_NAMES", "")
        self.node_ports = [int(x) for x in _env_list("NODE_PORTS", "") if x.isdigit()]
        self.probe_stale = int(_env("PROBE_STALE", "600"))
        self.probe_grace = int(_env("PROBE_GRACE", "1200"))

        self.state_file = Path(_env("STATE_FILE", "/var/lib/pqs-guard/state.json"))

    @property
    def probes_enabled(self):
        return self.probe_port > 0 and bool(self.node_ports)

    def validate(self, need_bot=True):
        errs = []
        if need_bot:
            if not self.tg_token:
                errs.append("TG_BOT_TOKEN 未设置")
            if not self.allowed_ids:
                errs.append("TG_ALLOWED_IDS 未设置")
            if not self.pqs_url:
                errs.append("PQS_CHANGE_IP_URL 未设置")
        if self.nf_require not in ("full", "originals"):
            errs.append("NF_REQUIRE 只能是 full 或 originals")
        if self.ddns_mode not in ("none", "cloudflare", "url"):
            errs.append("DDNS_MODE 只能是 none / cloudflare / url")
        if self.ddns_mode == "cloudflare":
            if not (self.cf_token or (self.cf_email and self.cf_key)):
                errs.append("DDNS_MODE=cloudflare 需要 CF_API_TOKEN（推荐）或 CF_EMAIL+CF_GLOBAL_KEY")
            if not (self.cf_zone and self.cf_record):
                errs.append("DDNS_MODE=cloudflare 需要 CF_ZONE_NAME 和 CF_RECORD_NAME")
        if self.ddns_mode == "url" and not self.ddns_url:
            errs.append("DDNS_MODE=url 需要 DDNS_URL")
        if self.probe_port and not self.probe_key:
            errs.append("启用探针（PROBE_PORT）需要设置 PROBE_KEY")
        if self.probe_port and not self.node_ports:
            errs.append("启用探针（PROBE_PORT）需要设置 NODE_PORTS")
        if errs:
            sys.exit("配置错误:\n  - " + "\n  - ".join(errs))


CFG = None  # type: Config


# ───────────────────────────── 状态持久化 ─────────────────────────────

TUNABLE = ("check_gfw", "check_netflix", "nf_require", "interval", "fail_threshold", "max_per_day")


class State:
    def __init__(self, path, auto_default):
        self.path = path
        data = {}
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("状态文件损坏，重新开始: %s", e)
        self.auto = data.get("auto", auto_default)
        self.last_change = data.get("last_change", 0.0)
        self.history = data.get("history", [])  # [{t, reason, old, new, ok}]
        self.settings = {k: v for k, v in data.get("settings", {}).items() if k in TUNABLE}

    def apply_settings(self):
        """Telegram 里改过的设置覆盖配置文件里的值"""
        for k, v in self.settings.items():
            setattr(CFG, k, v)

    def set(self, key, value):
        self.settings[key] = value
        setattr(CFG, key, value)
        self.save()

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"auto": self.auto, "last_change": self.last_change,
                                       "settings": self.settings, "history": self.history[-100:]},
                                      ensure_ascii=False, indent=1))
            tmp.replace(self.path)
        except Exception as e:
            log.error("保存状态失败: %s", e)

    @staticmethod
    def is_manual(h):
        # 旧记录没有 manual 字段，按原因判断
        return h.get("manual", h.get("reason") == "手动更换")

    def today_count(self, kind="all"):
        """kind: all / auto / manual"""
        today = dt.date.today().isoformat()
        hs = [h for h in self.history if h["t"].startswith(today)]
        if kind == "auto":
            hs = [h for h in hs if not self.is_manual(h)]
        elif kind == "manual":
            hs = [h for h in hs if self.is_manual(h)]
        return len(hs)


ST = None  # type: State
CHANGE_LOCK = None  # 在事件循环内创建（兼容 Python 3.9）
WAKE = None  # 设置变更时唤醒监控循环
LAST = None  # 最近一次检测结果 {"r": ..., "t": timestamp}


# ───────────────────────────── 网络检测 ─────────────────────────────

def http_client(timeout=15.0):
    # local_address=0.0.0.0 强制走 IPv4，避免 IPv6 出口导致误判
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1)
    return httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=True,
                             headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


async def get_public_ip():
    async with http_client(8) as c:
        for url in IP_SOURCES:
            try:
                r = await c.get(url)
                return str(ipaddress.IPv4Address(r.text.strip()))
            except Exception:
                continue
    return None


async def tcp_ok(target, timeout=3.0):
    host, port = target.rsplit(":", 1)
    try:
        _, w = await asyncio.wait_for(
            asyncio.open_connection(host, int(port), family=socket.AF_INET), timeout)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def probe(targets, rounds):
    res = {t: False for t in targets}
    for i in range(rounds):
        pending = [t for t in targets if not res[t]]
        if not pending:
            break
        if i:
            await asyncio.sleep(2)
        for t, ok in zip(pending, await asyncio.gather(*(tcp_ok(t) for t in pending))):
            res[t] = ok
    return res


async def check_gfw():
    """
    被墙判定：国外目标能通 && 所有国内目标都不通（多轮重试）。
    原理：IP 被墙后，服务器到国内的双向流量都会被丢弃，所以从服务器向国内主动连接也会失败。
    局限：只封端口 / 只做 SNI 阻断的情况探测不到，需要国内节点探测才能发现。
    """
    intl = await probe(CFG.intl_targets, 2)
    if not any(intl.values()):
        return {"state": "unknown", "detail": "国外目标也不通，疑似服务器网络故障，不判定"}
    cn = await probe(CFG.cn_targets, 3)
    ok = sum(cn.values())
    return {"state": "ok" if ok else "blocked", "detail": f"国内目标 {ok}/{len(cn)} 可达", "cn": cn}


async def _nf_title(c, tid):
    try:
        r = await c.get(f"https://www.netflix.com/title/{tid}")
        return r.status_code, str(r.url)
    except Exception as e:
        log.debug("netflix %s error: %s", tid, e)
        return None, ""


def _nf_region(url):
    # https://www.netflix.com/hk-en/title/xxx → HK ；无前缀 → US
    parts = [p for p in urlparse(url).path.split("/") if p]
    if parts and parts[0] != "title":
        return parts[0].split("-")[0].upper()
    return "US" if url else "?"


async def check_netflix():
    async with http_client(15) as c:
        (oc, ou), (lc, lu) = await asyncio.gather(_nf_title(c, NF_ORIGINAL), _nf_title(c, NF_LICENSED))
    region = _nf_region(lu or ou)
    if lc == 200:
        state, detail = "full", f"完整解锁（含非自制剧），地区 {region}"
    elif oc == 200:
        state, detail = "originals", f"仅解锁自制剧，地区 {region}"
    elif 403 in (oc, lc):
        state, detail = "none", "Netflix 拒绝此 IP（403）"
    elif oc == 404 and lc == 404:
        state, detail = "none", "该地区无 Netflix 服务"
    else:
        state, detail = "unknown", f"检测失败（自制剧 {oc}，非自制剧 {lc}），不判定"
    ok = None if state == "unknown" else (
        state == "full" if CFG.nf_require == "full" else state in ("full", "originals"))
    return {"state": state, "ok": ok, "detail": detail, "region": region}


async def run_checks():
    global LAST
    ip, gfw, nf = await asyncio.gather(
        get_public_ip(),
        check_gfw() if CFG.check_gfw else _skip(),
        check_netflix() if CFG.check_netflix else _skip(),
    )
    r = {"ip": ip, "gfw": gfw, "nf": nf}
    LAST = {"r": r, "t": time.time()}
    return r


async def _skip():
    return {"state": "skip", "ok": None, "detail": "未启用"}


def _gfw_text(g):
    cn = g.get("cn") or {}
    reach = f" · 国内 {sum(cn.values())}/{len(cn)} 可达" if cn else ""
    return {"ok": "✅ 正常", "blocked": "🚫 被墙", "unknown": "❔ 无法判断",
            "skip": "➖ 未启用"}[g["state"]] + reach


def _nf_text(n):
    st, region = n["state"], n.get("region", "")
    if st == "skip":
        return "➖ 未启用"
    if st == "full":
        return f"✅ 全解锁 · {region}"
    if st == "originals":
        return ("✅" if n.get("ok") else "⚠️") + f" 仅自制剧 · {region}"
    if st == "none":
        return "❌ " + n["detail"]
    return "❔ 检测失败"


def _ddns_text():
    if CFG.ddns_mode == "cloudflare":
        return CFG.cf_record
    if CFG.ddns_mode == "url":
        return "自定义 URL"
    return "未配置"


def fmt_report(r, ts=None):
    g, n = r["gfw"], r["nf"]
    pv = probe_verdict()
    if g["state"] == "blocked":
        head = "🔴 IP 被墙"
    elif pv["state"] in ("blocked", "silent"):
        head = "🔴 国内连不上节点"
    elif n.get("ok") is False:
        head = "🟡 Netflix 不达标"
    elif g["state"] == "unknown" or (n["state"] == "unknown"):
        head = "⚪ 部分检测失败"
    else:
        head = "🟢 一切正常"
    when = dt.datetime.fromtimestamp(ts or time.time()).strftime("%H:%M:%S")
    return "\n".join([
        f"<b>🖥 {html.escape(CFG.server_name)}</b>　{head}",
        "",
        f"🌐 <b>IP</b>　<code>{r['ip'] or '获取失败'}</code>",
        f"🧱 <b>墙</b>　{_gfw_text(g)}",
        f"🎬 <b>Netflix</b>　{html.escape(_nf_text(n))}",
        f"🔗 <b>DDNS</b>　{html.escape(_ddns_text())}",
    ] + ([f"🏠 <b>探针</b>　{html.escape(_probe_text(pv))}"] if CFG.probes_enabled else []) + [
        "",
        f"🤖 自动换 IP <b>{'开' if ST.auto else '关'}</b> · 今日自动 {ST.today_count('auto')}/{CFG.max_per_day}"
        f" · 手动 {ST.today_count('manual')}",
        f"<i>⏱ 每 {CFG.interval // 60} 分钟检测 · 上次 {when}</i>",
    ])


# ───────────────────────────── DDNS ─────────────────────────────

async def update_ddns(ip):
    if CFG.ddns_mode == "none":
        return "未配置 DDNS"
    try:
        async with http_client(20) as c:
            if CFG.ddns_mode == "url":
                r = await c.get(CFG.ddns_url.replace("{ip}", ip))
                return f"DDNS URL 返回 {r.status_code}: {r.text.strip()[:100]}"

            api = "https://api.cloudflare.com/client/v4"
            h = ({"Authorization": f"Bearer {CFG.cf_token}"} if CFG.cf_token
                 else {"X-Auth-Email": CFG.cf_email, "X-Auth-Key": CFG.cf_key})
            z = (await c.get(f"{api}/zones", params={"name": CFG.cf_zone}, headers=h)).json()
            if not z.get("success") or not z["result"]:
                return f"Cloudflare 找不到 zone {CFG.cf_zone}: {z.get('errors')}"
            zid = z["result"][0]["id"]
            ZONE_NS[:] = z["result"][0].get("name_servers") or []
            rec = (await c.get(f"{api}/zones/{zid}/dns_records",
                               params={"type": "A", "name": CFG.cf_record}, headers=h)).json()
            if not rec.get("success") or not rec["result"]:
                return f"Cloudflare 找不到 A 记录 {CFG.cf_record}: {rec.get('errors')}"
            rid, cur = rec["result"][0]["id"], rec["result"][0]["content"]
            if cur == ip:
                return f"{CFG.cf_record} 已是 {ip}"
            res = (await c.patch(f"{api}/zones/{zid}/dns_records/{rid}",
                                 json={"content": ip, "ttl": 60}, headers=h)).json()
            if res.get("success"):
                return f"{CFG.cf_record}: {cur} → {ip}"
            return f"Cloudflare 更新失败: {res.get('errors')}"
    except Exception as e:
        return f"DDNS 更新异常: {e}"


ZONE_NS = []  # Cloudflare 权威 DNS 服务器，更新 DDNS 时从 API 获取


def _dns_query_a(server, name, timeout=3.0):
    """向指定 DNS 服务器发一个不递归的 A 记录查询（UDP），返回 IP 列表"""
    tid = random.randint(0, 0xFFFF)
    pkt = struct.pack(">HHHHHH", tid, 0, 1, 0, 0, 0)
    pkt += b"".join(bytes([len(x)]) + x.encode() for x in name.rstrip(".").split(".")) + b"\0"
    pkt += struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(pkt, (server, 53))
        data = sock.recv(4096)
    if struct.unpack(">H", data[:2])[0] != tid:
        raise ValueError("DNS 响应 ID 不匹配")
    qd, an = struct.unpack(">HH", data[4:8])

    def skip_name(pos):
        while True:
            n = data[pos]
            if n & 0xC0 == 0xC0:
                return pos + 2
            if n == 0:
                return pos + 1
            pos += n + 1

    pos = 12
    for _ in range(qd):
        pos = skip_name(pos) + 4
    ips = []
    for _ in range(an):
        pos = skip_name(pos)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(data[pos:pos + 4]))
        pos += rdlen
    return ips


async def _authoritative_ok(ip):
    """所有 Cloudflare 权威服务器都返回新 IP 才算生效；查不了返回 None"""
    if not ZONE_NS:
        return None
    loop = asyncio.get_running_loop()
    results = []
    for ns in ZONE_NS:
        try:
            ns_ip = (await loop.getaddrinfo(ns, 53, family=socket.AF_INET))[0][4][0]
            results.append(ip in await loop.run_in_executor(None, _dns_query_a, ns_ip, CFG.cf_record))
        except Exception as e:
            log.debug("权威 DNS %s 查询失败: %s", ns, e)
    return all(results) if results else None


async def _doh_ok(ip):
    async with http_client(10) as c:
        r = await c.get("https://cloudflare-dns.com/dns-query",
                        params={"name": CFG.cf_record, "type": "A"},
                        headers={"accept": "application/dns-json"})
        return ip in [a.get("data") for a in r.json().get("Answer", [])]


async def verify_dns(ip, timeout=60):
    """
    确认 Cloudflare 已把域名解析到新 IP。
    优先直接问 Cloudflare 的权威服务器（不受任何缓存影响，通常几秒内就是新 IP）；
    权威服务器查不了时才退回用 1.1.1.1 的 DoH 查询（会受它自己缓存的影响）。
    """
    if CFG.ddns_mode != "cloudflare":
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ok = await _authoritative_ok(ip)
            if ok is None:
                ok = await _doh_ok(ip)
            if ok:
                return True
        except Exception as e:
            log.debug("DNS 生效检查失败: %s", e)
        await asyncio.sleep(5)
    return False


# ───────────────────────────── 家庭探针 ─────────────────────────────
# 协议（HTTP，明文，靠 key 鉴权）：
#   GET  /targets?key=K&name=N            → "IP PORT PORT ..."   探针据此知道要测哪个 IP 的哪些端口
#   POST /report   key=K&name=N&ip=..&r=443:ok,8443:fail        探针上报结果
# 被墙时探针根本连不上本服务器 → 所有探针同时失联，也是信号之一

PROBES = {}  # name → {"t": ts, "ip": tested_ip, "results": {port: bool}, "src": remote_ip}
PROBE_SERVER = None
_PARTIAL_NOTIFIED = {}  # name → 上次通知时间，避免反复提醒


def probe_status(name, now=None):
    now = now or time.time()
    p = PROBES.get(name)
    if not p or now - p["t"] > CFG.probe_stale:
        return "offline"
    res = p["results"]
    if not res:
        return "offline"
    ok = sum(1 for v in res.values() if v)
    return "ok" if ok == len(res) else ("fail" if ok == 0 else "mixed")


def probe_verdict():
    """
    综合所有探针：
      skip    未启用
      grace   刚换过 IP，探针还在用旧 IP/旧 DNS，不采信
      silent  所有探针都失联（被墙时探针连不上本机，或探针都掉线了）
      blocked 在线探针全部连不上节点端口
      partial 部分探针连不上（线路问题，不换 IP）
      ok      正常
    """
    if not CFG.probes_enabled:
        return {"state": "skip", "per": {}, "detail": "未启用"}
    now = time.time()
    names = list(dict.fromkeys(CFG.probe_names + list(PROBES.keys())))
    per = {n: probe_status(n, now) for n in names}
    if now - ST.last_change < CFG.probe_grace:
        left = int((CFG.probe_grace - (now - ST.last_change)) // 60) + 1
        return {"state": "grace", "per": per, "detail": f"刚换 IP，{left} 分钟后开始采信"}
    online = {n: st for n, st in per.items() if st != "offline"}
    if not names:
        return {"state": "silent", "per": per, "detail": "尚无探针上报"}
    if not online:
        return {"state": "silent", "per": per, "detail": "全部失联"}
    bad = [n for n, st in online.items() if st == "fail"]
    if bad and len(bad) == len(online):
        return {"state": "blocked", "per": per, "detail": "、".join(bad) + " 均连不上节点端口"}
    if bad:
        return {"state": "partial", "per": per, "detail": "、".join(bad) + " 连不上，其余正常"}
    mixed = [n for n, st in online.items() if st == "mixed"]
    if mixed:
        return {"state": "partial", "per": per, "detail": "、".join(mixed) + " 部分端口不通"}
    return {"state": "ok", "per": per, "detail": "正常"}


def _probe_text(pv):
    icon = {"ok": "✅", "fail": "❌", "mixed": "⚠️", "offline": "⚪"}
    parts = [f"{n} {icon[st]}" + (" 离线" if st == "offline" else "") for n, st in pv["per"].items()]
    if not parts:
        parts = ["等待上报"]
    s = " · ".join(parts)
    if pv["state"] == "grace":
        s += f"（{pv['detail']}）"
    return s


def probe_detail_text():
    pv = probe_verdict()
    rows = ["<b>🏠 家庭探针</b>", ""]
    if not CFG.probes_enabled:
        rows.append("未启用。在服务器运行 <code>pqs-guard probe</code> 开启。")
        return "\n".join(rows)
    rows.append(f"节点端口：<code>{', '.join(map(str, CFG.node_ports))}</code>")
    rows.append(f"综合判定：{html.escape(pv['detail'])}")
    rows.append("")
    now = time.time()
    for name, st in pv["per"].items():
        p = PROBES.get(name)
        if not p:
            rows.append(f"⚪ <b>{html.escape(name)}</b>　从未上报")
            continue
        ago = int(now - p["t"])
        ago_s = f"{ago}s 前" if ago < 120 else f"{ago // 60} 分钟前"
        icon = {"ok": "✅", "fail": "❌", "mixed": "⚠️", "offline": "⚪"}[st]
        ports = " ".join(f"{k}{'✓' if v else '✗'}" for k, v in sorted(p["results"].items()))
        rows.append(f"{icon} <b>{html.escape(name)}</b>　{ago_s}　测 <code>{p['ip']}</code>\n"
                    f"　　{html.escape(ports)}　来自 {p['src']}")
    rows.append("")
    rows.append(f"<i>超过 {CFG.probe_stale // 60} 分钟没上报算离线；换 IP 后 {CFG.probe_grace // 60} 分钟内不采信</i>")
    return "\n".join(rows)


def _parse_qs(qs):
    from urllib.parse import parse_qsl
    return dict(parse_qsl(qs, keep_blank_values=True))


def _http_resp(status, body):
    b = body.encode()
    return (f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(b)}\r\nConnection: close\r\n\r\n").encode() + b


async def _probe_http(reader, writer):
    src = writer.get_extra_info("peername")[0]
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        if len(head) > 8192:
            raise ValueError("header too large")
        lines = head.decode(errors="replace").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        length = 0
        for ln in lines[1:]:
            if ln.lower().startswith("content-length:"):
                length = min(int(ln.split(":", 1)[1].strip()), 4096)
        body = (await asyncio.wait_for(reader.readexactly(length), 10)).decode(errors="replace") if length else ""
        path, _, qs = target.partition("?")
        params = _parse_qs(qs)
        params.update(_parse_qs(body))

        if params.get("key") != CFG.probe_key:
            resp = _http_resp("403 Forbidden", "bad key")
        elif method == "GET" and path == "/targets":
            ip = (LAST or {}).get("r", {}).get("ip") or await get_public_ip() or ""
            resp = _http_resp("200 OK", f"{ip} {' '.join(map(str, CFG.node_ports))}\n")
        elif method == "POST" and path == "/report":
            name = params.get("name", "").strip()[:32]
            results = {}
            for item in params.get("r", "").split(","):
                port, _, val = item.partition(":")
                if port.strip().isdigit():
                    results[int(port)] = val.strip() == "ok"
            if not name or not results:
                resp = _http_resp("400 Bad Request", "need name and r")
            else:
                PROBES[name] = {"t": time.time(), "ip": params.get("ip", "?")[:45],
                                "results": results, "src": src}
                log.info("探针 %s(%s) 上报: %s", name, src, results)
                resp = _http_resp("200 OK", "ok\n")
        else:
            resp = _http_resp("404 Not Found", "not found")
        writer.write(resp)
        await writer.drain()
    except Exception as e:
        log.debug("探针请求异常 %s: %s", src, e)
    finally:
        writer.close()


async def start_probe_server():
    global PROBE_SERVER
    if not CFG.probes_enabled:
        return
    PROBE_SERVER = await asyncio.start_server(_probe_http, "0.0.0.0", CFG.probe_port)
    log.info("探针接收端口已开启: %d", CFG.probe_port)


# ───────────────────────────── 换 IP ─────────────────────────────

async def notify(app, text, chat_id=None, markup=None):
    # 换 IP 过程中网络会短暂中断，多重试几次
    for i in range(6):
        try:
            await app.bot.send_message(chat_id or CFG.notify_chat, text, parse_mode="HTML",
                                       reply_markup=markup, disable_web_page_preview=True)
            return
        except Exception as e:
            log.warning("Telegram 发送失败(%d): %s", i + 1, e)
            await asyncio.sleep(5 * (i + 1))


async def change_ip(app, reason, manual=False):
    global CHANGE_LOCK
    if CHANGE_LOCK is None:
        CHANGE_LOCK = asyncio.Lock()
    if CHANGE_LOCK.locked():
        await notify(app, "⏳ 已有换 IP 任务在进行，请稍候")
        return
    async with CHANGE_LOCK:
        if not manual:
            wait = CFG.cooldown - (time.time() - ST.last_change)
            if wait > 0:
                log.info("冷却中，%ds 后才能自动换 IP", wait)
                return
            if ST.today_count("auto") >= CFG.max_per_day:
                log.warning("今日自动换 IP 已达上限")
                return

        old_ip = await get_public_ip()
        await notify(app, f"🔄 <b>{html.escape(CFG.server_name)}</b> 开始换 IP\n"
                          f"原因: {html.escape(reason)}\n旧 IP: <code>{old_ip}</code>")
        try:
            async with http_client(30) as c:
                r = await c.get(CFG.pqs_url)
            api_resp = f"HTTP {r.status_code}: {r.text.strip()[:200]}"
            api_ok = r.status_code < 400
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            # 换 IP 会切断本机网络，接口响应回不来是正常现象 → 不判失败，看 IP 有没有变
            api_resp = f"无响应（{type(e).__name__}），换 IP 时连接中断属正常"
            api_ok = None
        except Exception as e:
            api_resp, api_ok = f"请求异常: {type(e).__name__} {e}".strip(), False
        log.info("PQS API: %s", api_resp)

        entry = {"t": dt.datetime.now().isoformat(timespec="seconds"), "reason": reason,
                 "manual": manual, "old": old_ip, "new": None, "ok": False}
        ST.last_change = time.time()
        ST.history.append(entry)
        ST.save()

        if api_ok is False:
            await notify(app, f"❌ 换 IP 接口调用失败\n<code>{html.escape(api_resp)}</code>", markup=panel_kb())
            return
        if api_ok is None:
            await notify(app, f"⏳ 接口{html.escape(api_resp)}\n正在确认 IP 是否已更换（最多 {CFG.ip_wait}s）…")

        new_ip, deadline = None, time.time() + CFG.ip_wait
        await asyncio.sleep(10)
        while time.time() < deadline:
            ip = await get_public_ip()
            if ip and ip != old_ip:
                new_ip = ip
                break
            await asyncio.sleep(10)

        if not new_ip:
            await notify(app, f"⚠️ 接口已调用，但 {CFG.ip_wait}s 内公网 IP 没变化\n"
                              f"接口返回: <code>{html.escape(api_resp)}</code>", markup=panel_kb())
            return

        entry.update(new=new_ip, ok=True)
        ST.save()
        ddns = await update_ddns(new_ip)
        dns_ok = await verify_dns(new_ip)
        report = await run_checks()
        dns_line = {True: "　✅ Cloudflare 已生效", False: "　⏳ Cloudflare 生效中", None: ""}[dns_ok]
        tip = ""
        if CFG.ddns_mode != "none":
            tip = (f"\n\n💡 国内 DNS 会缓存旧 IP，域名可能要几分钟到十几分钟才连得上。"
                   f"急用可直接连新 IP：<code>{new_ip}</code>")
        await notify(app, f"✅ <b>换 IP 完成</b>\n<code>{old_ip}</code> → <code>{new_ip}</code>\n"
                          f"🔗 <b>DDNS</b>　{html.escape(ddns)}{dns_line}{tip}\n\n{fmt_report(report)}",
                     markup=panel_kb())


# ───────────────────────────── 自动监控循环 ─────────────────────────────

async def monitor_loop(app):
    await asyncio.sleep(20)
    fails = {"gfw": 0, "nf": 0, "probe": 0}
    limit_warned = ""
    while True:
        try:
            r = await run_checks()
            g, n = r["gfw"], r["nf"]
            pv = probe_verdict()
            if g["state"] == "blocked":
                fails["gfw"] += 1
            elif g["state"] == "ok":
                fails["gfw"] = 0
            if n.get("ok") is False:
                fails["nf"] += 1
            elif n.get("ok") is True:
                fails["nf"] = 0
            # 探针：服务器自身网络异常（国外也不通）时不采信失联
            if pv["state"] == "blocked" or (pv["state"] == "silent" and g["state"] != "unknown"):
                fails["probe"] += 1
            elif pv["state"] in ("ok", "partial", "skip"):
                fails["probe"] = 0
            log.info("检测 ip=%s gfw=%s nf=%s probe=%s fails=%s",
                     r["ip"], g["state"], n["state"], pv["state"], fails)

            if pv["state"] == "partial":
                key = pv["detail"]
                if time.time() - _PARTIAL_NOTIFIED.get(key, 0) > 3600:
                    _PARTIAL_NOTIFIED[key] = time.time()
                    await notify(app, f"⚠️ <b>线路提醒</b>（不换 IP）\n{html.escape(key)}\n"
                                      f"只有部分地区连不上，通常是运营商线路问题，不是被墙。",
                                 markup=panel_kb())

            reason = None
            if fails["gfw"] >= CFG.fail_threshold:
                reason = f"IP 被墙（连续 {fails['gfw']} 次国内不可达）"
            elif fails["probe"] >= CFG.fail_threshold:
                reason = (f"家庭探针{pv['detail']}（连续 {fails['probe']} 次）" if pv["state"] == "silent"
                          else f"国内连不上节点端口：{pv['detail']}（连续 {fails['probe']} 次）")
            elif fails["nf"] >= CFG.fail_threshold:
                reason = f"Netflix 不达标：{n['detail']}"

            if reason:
                if not ST.auto:
                    await notify(app, f"⚠️ 检测异常但自动换 IP 已关闭\n{html.escape(reason)}\n\n"
                                      f"{fmt_report(r)}", markup=panel_kb())
                    fails = {"gfw": 0, "nf": 0, "probe": 0}  # 避免每轮都提醒
                elif ST.today_count("auto") >= CFG.max_per_day:
                    today = dt.date.today().isoformat()
                    if limit_warned != today:
                        limit_warned = today
                        await notify(app, f"🛑 今日自动换 IP 已达上限 {CFG.max_per_day} 次，暂停自动更换\n"
                                          f"{html.escape(reason)}", markup=panel_kb())
                else:
                    await change_ip(app, reason)
                    fails = {"gfw": 0, "nf": 0, "probe": 0}
        except Exception:
            log.exception("监控循环异常")
        try:
            await asyncio.wait_for(WAKE.wait(), CFG.interval)
        except asyncio.TimeoutError:
            pass
        WAKE.clear()


# ───────────────────────────── Telegram ─────────────────────────────

from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,  # noqa: E402
                      KeyboardButton, ReplyKeyboardMarkup, Update)
from telegram.error import BadRequest  # noqa: E402
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,  # noqa: E402
                          ContextTypes, MessageHandler, filters)

B = InlineKeyboardButton
PANEL_BTN = "🏠 控制面板"
CYCLES = {
    "interval": [180, 300, 600, 900, 1800],
    "fail_threshold": [1, 2, 3, 5],
    "max_per_day": [3, 5, 8, 12, 20],
}


def _next(cur, opts):
    return next((o for o in opts if o > cur), opts[0])


def panel_kb():
    auto = "🟢 自动：开" if ST.auto else "⚪️ 自动：关"
    return InlineKeyboardMarkup([
        [B("🔍 检测", callback_data="refresh"), B("🔄 换 IP", callback_data="change")],
        [B(auto, callback_data="auto"), B("⚙️ 更多", callback_data="settings")],
    ])


def back_kb(to="home"):
    return InlineKeyboardMarkup([[B("◀️ 返回", callback_data=to)]])


def confirm_kb():
    return InlineKeyboardMarkup([[B("✅ 确认更换", callback_data="change_yes"),
                                  B("✖️ 取消", callback_data="home")]])


def settings_kb():
    on = lambda b: "开" if b else "关"  # noqa: E731
    return InlineKeyboardMarkup([
        [B("🌐 同步 DDNS", callback_data="ddns"), B("📜 换 IP 记录", callback_data="history")],
    ] + ([[B("🏠 探针状态", callback_data="probes")]] if CFG.probes_enabled else []) + [
        [B(f"🧱 被墙检测：{on(CFG.check_gfw)}", callback_data="s:check_gfw"),
         B(f"🎬 Netflix 检测：{on(CFG.check_netflix)}", callback_data="s:check_netflix")],
        [B(f"🎯 Netflix 要求：{'全解锁' if CFG.nf_require == 'full' else '仅自制剧'}",
           callback_data="s:nf_require")],
        [B(f"⏱ 检测间隔：{CFG.interval // 60} 分钟", callback_data="s:interval"),
         B(f"🔁 连续失败：{CFG.fail_threshold} 次", callback_data="s:fail_threshold")],
        [B(f"📅 每日自动上限：{CFG.max_per_day} 次", callback_data="s:max_per_day")],
        [B("❓ 说明", callback_data="help"), B("◀️ 返回", callback_data="home")],
    ])


def panel_text():
    if LAST:
        return fmt_report(LAST["r"], LAST["t"])
    return (f"<b>🖥 {html.escape(CFG.server_name)}</b>\n\n还没有检测结果，点「🔍 检测」")


SETTINGS_TEXT = (
    "<b>⚙️ 更多</b>\n\n"
    "设置项点一下即切换，立即生效并永久保存。\n\n"
    "• <b>被墙检测 / Netflix 检测</b>：关掉后不再因此自动换 IP\n"
    "• <b>Netflix 要求</b>：全解锁 = 必须能看非自制剧；仅自制剧 = 能看自制剧就行\n"
    "• <b>检测间隔</b>：多久自动检测一次\n"
    "• <b>连续失败</b>：连续几次异常才换 IP，防误判\n"
    "• <b>每日自动上限</b>：每天最多自动换几次。手动换 IP 单独计数，不受限制"
)

HELP_TEXT = (
    "<b>❓ 说明</b>\n\n"
    "🔍 <b>检测</b>：查看当前 IP、是否被墙、Netflix 解锁情况\n"
    "🔄 <b>换 IP</b>：手动换 IP，完成后自动同步 DDNS 并复检\n"
    "🤖 <b>自动</b>：被墙或 Netflix 不达标时自动换 IP，点击开关\n"
    "⚙️ <b>更多</b>：同步 DDNS、换 IP 记录、探针状态、各项设置\n\n"
    "🏠 <b>探针</b>：你家里设备定时连节点端口并上报，能发现只封端口和线路问题。"
    "全部探针连不上才算被墙；只有一地连不上只提醒不换 IP。\n\n"
    "<b>被墙判定</b>：国外能通，但国内目标全部连不上，且连续多次。"
    "「国内 3/4 可达」表示 4 个国内目标连上 3 个，只要有 1 个能连就是正常。\n\n"
    f"底部「{PANEL_BTN}」按钮随时打开本面板。"
)


def history_text():
    if not ST.history:
        return "<b>📜 换 IP 记录</b>\n\n暂无记录"
    rows = ["<b>📜 最近 10 次换 IP</b>", ""]
    for h in ST.history[-10:][::-1]:
        mark = "✅" if h["ok"] else "❌"
        kind = "🖐 手动" if ST.is_manual(h) else "🤖 自动"
        reason = "" if ST.is_manual(h) else "　" + html.escape(h["reason"])
        rows.append(f"{mark} <b>{h['t'][5:16].replace('T', ' ')}</b>　{kind}{reason}\n"
                    f"　　<code>{h['old']}</code> → <code>{h['new'] or '-'}</code>")
    rows.append(f"\n今日：🤖 自动 {ST.today_count('auto')}/{CFG.max_per_day} 次 · 🖐 手动 {ST.today_count('manual')} 次（不限）")
    return "\n".join(rows)


def authorized(fn):
    @functools.wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if uid not in CFG.allowed_ids:
            log.warning("未授权用户 %s", uid)
            if update.callback_query:
                await update.callback_query.answer("无权限", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("您没有权限使用此机器人。")
            return
        return await fn(update, ctx)
    return wrapper


async def safe_edit(q, text, kb=None):
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb,
                                  disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def send_panel(update: Update, refresh=False):
    msg = update.effective_message
    if refresh or not LAST:
        m = await msg.reply_text("⏳ 正在检测，约 10-20 秒…")
        await run_checks()
        await m.edit_text(panel_text(), parse_mode="HTML", reply_markup=panel_kb())
    else:
        await msg.reply_text(panel_text(), parse_mode="HTML", reply_markup=panel_kb())


async def do_ddns():
    if CFG.ddns_mode == "none":
        return "未配置 DDNS。\n如需配置，在服务器运行 <code>pqs-guard config</code>"
    ip = await get_public_ip()
    if not ip:
        return "❌ 获取公网 IP 失败"
    return "🌐 " + html.escape(await update_ddns(ip))


# ── 命令 ──

@authorized
async def cmd_start(update: Update, ctx):
    await update.effective_message.reply_text(
        f"👋 欢迎使用 <b>{html.escape(CFG.server_name)}</b> IP 管理机器人\n"
        f"所有功能都在下面的面板里，底部「{PANEL_BTN}」随时打开。",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton(PANEL_BTN)]], resize_keyboard=True,
                                         is_persistent=True))
    await send_panel(update)


@authorized
async def cmd_panel(update: Update, ctx):
    await send_panel(update)


@authorized
async def cmd_status(update: Update, ctx):
    await send_panel(update, refresh=True)


@authorized
async def cmd_change(update: Update, ctx):
    await update.effective_message.reply_text(confirm_text(), parse_mode="HTML",
                                              reply_markup=confirm_kb())


@authorized
async def cmd_auto(update: Update, ctx):
    if ctx.args and ctx.args[0].lower() in ("on", "off"):
        ST.auto = ctx.args[0].lower() == "on"
        ST.save()
    await update.effective_message.reply_text(
        f"🤖 自动换 IP：{'已开启' if ST.auto else '已关闭'}", reply_markup=panel_kb())


@authorized
async def cmd_history(update: Update, ctx):
    await update.effective_message.reply_text(history_text(), parse_mode="HTML", reply_markup=back_kb())


@authorized
async def cmd_ddns(update: Update, ctx):
    m = await update.effective_message.reply_text("⏳ 正在同步 DDNS…")
    await m.edit_text(await do_ddns(), parse_mode="HTML", reply_markup=back_kb())


@authorized
async def cmd_settings(update: Update, ctx):
    await update.effective_message.reply_text(SETTINGS_TEXT, parse_mode="HTML", reply_markup=settings_kb())


def confirm_text():
    ip = LAST["r"]["ip"] if LAST else "未知"
    return (f"<b>🔄 确定更换 IP？</b>\n\n当前 IP：<code>{ip}</code>\n"
            "换 IP 期间网络会中断片刻，进度会单独推送。")


# ── 按钮 ──

@authorized
async def on_button(update: Update, ctx):
    q = update.callback_query
    d = q.data

    if d == "change_yes" and CHANGE_LOCK is not None and CHANGE_LOCK.locked():
        await q.answer("已有换 IP 任务在进行，请稍候", show_alert=True)
        return
    await q.answer()

    if d == "home":
        await safe_edit(q, panel_text(), panel_kb())
    elif d == "refresh":
        await safe_edit(q, panel_text() + "\n\n⏳ <b>正在检测，约 10-20 秒…</b>")
        await run_checks()
        await safe_edit(q, panel_text(), panel_kb())
    elif d == "change":
        await safe_edit(q, confirm_text(), confirm_kb())
    elif d == "change_yes":
        await safe_edit(q, "🔄 <b>已开始更换 IP</b>\n\n进度和结果会单独推送到这里。", back_kb())
        ctx.application.create_task(change_ip(ctx.application, "手动更换", manual=True))
    elif d == "auto":
        ST.auto = not ST.auto
        ST.save()
        await safe_edit(q, panel_text(), panel_kb())
    elif d == "ddns":
        await safe_edit(q, "⏳ 正在同步 DDNS…")
        await safe_edit(q, await do_ddns(), back_kb("settings"))
    elif d == "history":
        await safe_edit(q, history_text(), back_kb("settings"))
    elif d == "probes":
        await safe_edit(q, probe_detail_text(), back_kb("settings"))
    elif d == "help":
        await safe_edit(q, HELP_TEXT, back_kb("settings"))
    elif d == "settings":
        await safe_edit(q, SETTINGS_TEXT, settings_kb())
    elif d.startswith("s:"):
        key = d[2:]
        if key in ("check_gfw", "check_netflix"):
            ST.set(key, not getattr(CFG, key))
        elif key == "nf_require":
            ST.set(key, "originals" if CFG.nf_require == "full" else "full")
        elif key in CYCLES:
            ST.set(key, _next(getattr(CFG, key), CYCLES[key]))
            if key == "interval":
                WAKE.set()
        await safe_edit(q, SETTINGS_TEXT, settings_kb())


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("处理更新出错: %s", ctx.error, exc_info=ctx.error)


async def post_init(app: Application):
    global WAKE
    WAKE = asyncio.Event()
    await app.bot.set_my_commands([
        BotCommand("menu", "🏠 控制面板"),
        BotCommand("status", "🔍 立即检测"),
        BotCommand("change_ip", "🔄 更换 IP"),
        BotCommand("ddns", "🌐 同步 DDNS"),
        BotCommand("history", "📜 换 IP 记录"),
        BotCommand("auto", "🤖 自动换 IP（on/off）"),
        BotCommand("settings", "⚙️ 设置"),
    ])
    app.bot_data["monitor"] = asyncio.create_task(monitor_loop(app))
    await start_probe_server()
    await run_checks()
    await notify(app, f"🚀 <b>守护已启动</b> v{__version__}\n\n{panel_text()}", markup=panel_kb())


async def post_shutdown(app: Application):
    t = app.bot_data.get("monitor")
    if t:
        t.cancel()
    if PROBE_SERVER:
        PROBE_SERVER.close()


def register_handlers(app: Application):
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("menu", cmd_panel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("change_ip", cmd_change))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("ddns", cmd_ddns))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(MessageHandler(filters.Regex(f"^{PANEL_BTN}$"), cmd_panel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)


def main():
    global CFG, ST
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", help="配置文件路径")
    ap.add_argument("--check", action="store_true", help="只检测一次并打印")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()

    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.env:
        load_env_file(args.env)
    CFG = Config()
    CFG.validate(need_bot=not args.check)

    if args.check:
        ST = State(Path("/nonexistent/state.json"), CFG.auto_default)
        r = asyncio.run(run_checks())
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    ST = State(CFG.state_file, CFG.auto_default)
    ST.apply_settings()
    app = (Application.builder().token(CFG.tg_token).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    register_handlers(app)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
