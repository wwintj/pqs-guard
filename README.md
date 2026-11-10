# pqs-guard

PQS 服务器的 IP 守护程序，可以用 Telegram 机器人控制：

| 功能 | 说明 |
|---|---|
| 手动换 IP | 机器人里发 `/change_ip` 或点菜单按钮，需要二次确认 |
| 状态检测 | `/status` 查看当前 IP、是否被墙、Netflix 解锁情况 |
| 被墙自动换 IP | 国外能通、国内全部不通，连续 2 次才换 |
| Netflix 自动换 IP | 看不了非自制剧（或可设为只要求自制剧），连续 2 次才换 |
| 换 IP 后自动处理 | 等新 IP 生效 → 更新 DDNS（Cloudflare / 自定义 URL）→ 再检测一次 → 推送结果 |
| 防止换个不停 | 冷却 10 分钟、每天上限 8 次、同一时间只跑一个换 IP 任务 |
| 开关 | `/auto on`、`/auto off`，重启后依然有效 |

## 一键安装

以 root 登录服务器后执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/wwintj/pqs-guard/main/pqs-guard.sh)
```

在菜单里选 `1) 安装`，然后按提示填写：

1. 服务器名称
2. Telegram 机器人 token（脚本会自动校验）
3. 你的 Telegram 用户 ID（可在 @userinfobot 查询）
4. PQS 后台生成的换 IP 接口 URL
5. DDNS 方式

安装脚本还会：

- 检测旧的 `nf.sh` / `ipc.sh` / `bot.py` / `cf-ddns` 进程和定时任务，问你要不要清理（会先备份 crontab）
- 安装完先自检一次（只检测，不换 IP），再启动 systemd 服务

## 管理

装好后直接运行 `pqs-guard` 打开菜单，或者带上子命令：

```bash
pqs-guard upgrade      # 一键升级（保留配置，自动补上新版本新增的配置项）
pqs-guard uninstall    # 一键卸载（会问要不要删配置和记录）
pqs-guard config       # 修改配置：向导，或直接用编辑器改
pqs-guard check        # 立即检测一次，不换 IP
pqs-guard status       # 查看服务状态
pqs-guard log          # 实时日志
pqs-guard restart      # 重启
```

配置文件在 `/etc/pqs-guard/config.env`（权限 600），所有选项见 [config.env.example](config.env.example)。

## 多台服务器

每台服务器单独装一份，并且**每台必须用不同的 Telegram 机器人**。同一个 token 在两台机器上同时轮询会报 409 Conflict。

## DDNS 选哪种

- **PQS 后台生成的 DDNS 域名**（会自动跟着 IP 变）：选「不需要」。如果想用自己的域名，就在 DNS 服务商那里加一条 CNAME 指向它。
- **Cloudflare**：用 API Token，只给 `Zone → DNS → Edit` 权限。不要用 Global API Key。
- **自定义 URL**：换完 IP 后访问一次这个地址，地址里的 `{ip}` 会替换成新 IP。

## 检测原理与局限

- **被墙**：IP 被墙后，服务器和国内之间的流量两个方向都会被丢弃。程序先确认国外目标能通，排除服务器自身断网，再用 TCP 连国内的几个公共 DNS（114 / DNSPod / 百度 / 阿里），全部连不上才算被墙。
  只封某个端口、或只做 SNI 阻断的情况探测不到。
- **Netflix**：请求自制剧 `81280792` 和非自制剧 `70143836`（绝命毒师）两个标题页，看返回的状态码。网络出错算「未知」，不会因此换 IP。

## 安全

- token、接口 URL 都只写在服务器本地的配置文件里，不会写进代码。
- 拿到 PQS 换 IP 接口 URL 的人都能给你换 IP，请不要泄露。
