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
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

__version__ = "1.1.0"

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

        self.state_file = Path(_env("STATE_FILE", "/var/lib/pqs-guard/state.json"))

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

    def today_count(self):
        today = dt.date.today().isoformat()
        return sum(1 for h in self.history if h["t"].startswith(today))


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
    if g["state"] == "blocked":
        head = "🔴 IP 被墙"
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
        "",
        f"🤖 自动换 IP <b>{'开' if ST.auto else '关'}</b> · 今日 {ST.today_count()}/{CFG.max_per_day}",
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
            if ST.today_count() >= CFG.max_per_day:
                log.warning("今日自动换 IP 已达上限")
                return

        old_ip = await get_public_ip()
        await notify(app, f"🔄 <b>{html.escape(CFG.server_name)}</b> 开始换 IP\n"
                          f"原因: {html.escape(reason)}\n旧 IP: <code>{old_ip}</code>")
        try:
            async with http_client(60) as c:
                r = await c.get(CFG.pqs_url)
            api_resp = f"HTTP {r.status_code}: {r.text.strip()[:200]}"
            api_ok = r.status_code < 400
        except Exception as e:
            api_resp, api_ok = f"请求异常: {e}", False
        log.info("PQS API: %s", api_resp)

        entry = {"t": dt.datetime.now().isoformat(timespec="seconds"), "reason": reason,
                 "old": old_ip, "new": None, "ok": False}
        ST.last_change = time.time()
        ST.history.append(entry)
        ST.save()

        if not api_ok:
            await notify(app, f"❌ 换 IP 接口调用失败\n<code>{html.escape(api_resp)}</code>", markup=panel_kb())
            return

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
        await asyncio.sleep(5)
        report = await run_checks()
        await notify(app, f"✅ <b>换 IP 完成</b>\n<code>{old_ip}</code> → <code>{new_ip}</code>\n"
                          f"DDNS：{html.escape(ddns)}\n\n{fmt_report(report)}", markup=panel_kb())


# ───────────────────────────── 自动监控循环 ─────────────────────────────

async def monitor_loop(app):
    await asyncio.sleep(20)
    fails = {"gfw": 0, "nf": 0}
    limit_warned = ""
    while True:
        try:
            r = await run_checks()
            g, n = r["gfw"], r["nf"]
            if g["state"] == "blocked":
                fails["gfw"] += 1
            elif g["state"] == "ok":
                fails["gfw"] = 0
            if n.get("ok") is False:
                fails["nf"] += 1
            elif n.get("ok") is True:
                fails["nf"] = 0
            log.info("检测 ip=%s gfw=%s nf=%s fails=%s", r["ip"], g["state"], n["state"], fails)

            reason = None
            if fails["gfw"] >= CFG.fail_threshold:
                reason = f"IP 被墙（连续 {fails['gfw']} 次国内不可达）"
            elif fails["nf"] >= CFG.fail_threshold:
                reason = f"Netflix 不达标：{n['detail']}"

            if reason:
                if not ST.auto:
                    await notify(app, f"⚠️ 检测异常但自动换 IP 已关闭\n{html.escape(reason)}\n\n"
                                      f"{fmt_report(r)}", markup=panel_kb())
                    fails = {"gfw": 0, "nf": 0}  # 避免每轮都提醒
                elif ST.today_count() >= CFG.max_per_day:
                    today = dt.date.today().isoformat()
                    if limit_warned != today:
                        limit_warned = today
                        await notify(app, f"🛑 今日自动换 IP 已达上限 {CFG.max_per_day} 次，暂停自动更换\n"
                                          f"{html.escape(reason)}", markup=panel_kb())
                else:
                    await change_ip(app, reason)
                    fails = {"gfw": 0, "nf": 0}
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
    auto = "🟢 自动换 IP：已开启" if ST.auto else "⚪️ 自动换 IP：已关闭"
    return InlineKeyboardMarkup([
        [B("🔍 立即检测", callback_data="refresh"), B("🔄 更换 IP", callback_data="change")],
        [B("🌐 同步 DDNS", callback_data="ddns"), B("📜 换 IP 记录", callback_data="history")],
        [B(auto, callback_data="auto")],
        [B("⚙️ 设置", callback_data="settings"), B("❓ 说明", callback_data="help")],
    ])


def back_kb():
    return InlineKeyboardMarkup([[B("◀️ 返回面板", callback_data="home")]])


def confirm_kb():
    return InlineKeyboardMarkup([[B("✅ 确认更换", callback_data="change_yes"),
                                  B("✖️ 取消", callback_data="home")]])


def settings_kb():
    on = lambda b: "开" if b else "关"  # noqa: E731
    return InlineKeyboardMarkup([
        [B(f"🧱 被墙检测：{on(CFG.check_gfw)}", callback_data="s:check_gfw"),
         B(f"🎬 Netflix 检测：{on(CFG.check_netflix)}", callback_data="s:check_netflix")],
        [B(f"🎯 Netflix 要求：{'全解锁' if CFG.nf_require == 'full' else '仅自制剧'}",
           callback_data="s:nf_require")],
        [B(f"⏱ 检测间隔：{CFG.interval // 60} 分钟", callback_data="s:interval"),
         B(f"🔁 连续失败：{CFG.fail_threshold} 次", callback_data="s:fail_threshold")],
        [B(f"📅 每日自动上限：{CFG.max_per_day} 次", callback_data="s:max_per_day")],
        [B("◀️ 返回面板", callback_data="home")],
    ])


def panel_text():
    if LAST:
        return fmt_report(LAST["r"], LAST["t"])
    return (f"<b>🖥 {html.escape(CFG.server_name)}</b>\n\n还没有检测结果，点「🔍 立即检测」")


SETTINGS_TEXT = (
    "<b>⚙️ 设置</b>\n\n"
    "点按钮即可切换，立即生效并永久保存。\n\n"
    "• <b>被墙检测 / Netflix 检测</b>：关掉后不再因此自动换 IP\n"
    "• <b>Netflix 要求</b>：全解锁 = 必须能看非自制剧；仅自制剧 = 能看自制剧就行\n"
    "• <b>检测间隔</b>：多久自动检测一次\n"
    "• <b>连续失败</b>：连续几次异常才换 IP，防误判\n"
    "• <b>每日自动上限</b>：每天最多自动换几次（手动不受限）"
)

HELP_TEXT = (
    "<b>❓ 说明</b>\n\n"
    "🔍 <b>立即检测</b>：查看当前 IP、是否被墙、Netflix 解锁情况\n"
    "🔄 <b>更换 IP</b>：手动换 IP，完成后自动同步 DDNS 并复检\n"
    "🌐 <b>同步 DDNS</b>：把域名解析更新为当前 IP\n"
    "📜 <b>换 IP 记录</b>：最近 10 次换 IP\n"
    "🤖 <b>自动换 IP</b>：被墙或 Netflix 不达标时自动更换\n\n"
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
        rows.append(f"{mark} <b>{h['t'][5:16].replace('T', ' ')}</b>　{html.escape(h['reason'])}\n"
                    f"　　<code>{h['old']}</code> → <code>{h['new'] or '-'}</code>")
    rows.append(f"\n今日共 {ST.today_count()} 次")
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
        await safe_edit(q, await do_ddns(), back_kb())
    elif d == "history":
        await safe_edit(q, history_text(), back_kb())
    elif d == "help":
        await safe_edit(q, HELP_TEXT, back_kb())
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
    await run_checks()
    await notify(app, f"🚀 <b>守护已启动</b> v{__version__}\n\n{panel_text()}", markup=panel_kb())


async def post_shutdown(app: Application):
    t = app.bot_data.get("monitor")
    if t:
        t.cancel()


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
