# pqs-guard

PQS 服务器的 IP 守护程序，可以用 Telegram 机器人控制：

| 功能 | 说明 |
|---|---|
| 手动换 IP | 控制面板点「🔄 换 IP」，需要二次确认 |
| 状态检测 | 控制面板点「🔍 检测」，查看当前 IP、是否被墙、Netflix 解锁情况 |
| 被墙自动换 IP | 国外能通、国内全部不通，连续 2 次才换 |
| Netflix 自动换 IP | 看不了非自制剧（或可设为只要求自制剧），连续 2 次才换 |
| 换 IP 后自动处理 | 等新 IP 生效 → 更新 DDNS（Cloudflare / 自定义 URL）→ 确认 DNS 已生效 → 再检测一次 → 推送结果 |
| 防止换个不停 | 冷却 10 分钟、每天自动换 IP 默认最多 8 次（可在面板里改）、同一时间只跑一个换 IP 任务 |
| 家庭探针 | 国内设备定时连节点端口并上报，能发现「只封端口」和线路问题；全部探针连不上才换 IP，只有一地连不上只提醒 |
| 控制面板 | 主面板 4 个按钮：检测、换 IP、自动开关、更多。「更多」里有同步 DDNS、换 IP 记录和各项设置，点一下就生效 |

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

换 IP 后，你的域名要跟着指向新 IP，客户端和 SSH 才连得上。有三种做法：

| 方式 | 适合 | 安装时选 |
|---|---|---|
| PQS 生成的 DDNS 域名 | 不想折腾，PQS 自己会让域名跟着 IP 变 | `1) 不需要` |
| Cloudflare | 想用自己的域名，域名托管在 Cloudflare（**推荐**） | `2) Cloudflare` |
| 自定义 URL | 其他 DDNS 服务，访问一个地址就能更新 | `3) 自定义 URL`，地址里的 `{ip}` 会被替换成新 IP |

用 PQS 的 DDNS 域名，又想换成自己的域名时，可以在 DNS 服务商那里加一条 **CNAME** 指向 PQS 的域名，然后选「不需要」。

## Cloudflare DDNS 详细设置

下面用 `example.com` 举例，实际操作时换成你自己的域名。假设要让 `node.example.com` 始终指向这台服务器。

### 第 1 步：先建好 A 记录

**程序只修改已有记录的 IP，不会新建记录**，所以要先手动建一条。

1. 登录 [Cloudflare 控制台](https://dash.cloudflare.com)，在首页的 **Domains** 列表里点你的域名，例如 `example.com`
2. 左侧菜单进入 **DNS → Records**，点 **Add record**
3. 按下表填写，然后点 **Save**：

| 字段 | 填写 |
|---|---|
| Type | `A` |
| Name | `node`（只填前缀，完整域名就是 `node.example.com`） |
| IPv4 address | 服务器当前 IP（随便填也可以，换 IP 时会自动改掉） |
| Proxy status | **关闭，小云朵必须是灰色（DNS only）** |
| TTL | `Auto` 或 `1 min`（程序更新时会自动设成 60 秒） |

> ⚠️ **小云朵一定要是灰色**。橙色表示流量走 Cloudflare 的代理，代理节点和 SSH 都连不上。

### 第 2 步：创建 API Token

API Token 只给改 DNS 的权限，比 Global API Key 安全得多。Global API Key 能操作整个 Cloudflare 账号，**不要用**。

1. 打开 <https://dash.cloudflare.com/profile/api-tokens>，也可以点右上角头像 → **My Profile** → **API Tokens**
2. 点 **Create Token**
3. **Token name**：随便起，例如 `pqs-guard-node`
4. **Policies**：选 **Edit zone DNS** 模板，会自动生成一条 `Zone policy · Include · DNS Write`
5. **把范围限制到一个域名（重要）**：点这条策略右边的 ✏️ 铅笔图标，把 zone 设为 **Specific zone → `example.com`** 后保存。默认可能是「所有域名」，那样这个 token 就能改你账号下所有域名的 DNS
6. **Token expiration**：选 **No expiration**。设了过期时间的话，到期后 DDNS 会悄悄失效
7. **Client IP address filtering**：**留空**。不要填服务器 IP，程序就是负责换 IP 的，填了以后 IP 一换 token 就用不了了
8. 点 **Continue to summary**，确认显示的是 `example.com - DNS:Edit`，然后点 **Create Token**
9. **马上复制 token**，它只显示这一次。弄丢了只能重新创建

### 第 3 步：填进程序

**新安装**：安装向导问 DDNS 时选 `2`，依次填：

```
Cloudflare API Token（只需 Zone.DNS:Edit 权限）: 粘贴第 2 步的 token
根域名，如 example.com: example.com
要更新的完整域名，如 node.example.com: node.example.com
```

**已经装好了**：运行 `pqs-guard config` → `1) 重新走一遍配置向导`。也可以直接编辑 `/etc/pqs-guard/config.env`：

```ini
DDNS_MODE=cloudflare
CF_API_TOKEN=你的token
CF_ZONE_NAME=example.com
CF_RECORD_NAME=node.example.com
```

改完后运行 `pqs-guard restart`。

### 第 4 步：验证

在 Telegram 控制面板点 **⚙️ 更多 → 🌐 同步 DDNS**：

| 机器人回复 | 意思 |
|---|---|
| `node.example.com 已是 1.2.3.4` | ✅ 配置正确，记录已经是当前 IP |
| `node.example.com: 旧IP → 新IP` | ✅ 配置正确，刚刚改好了 |
| `Cloudflare 找不到 zone example.com` | token 权限范围不对，或根域名填错了。先检查第 2 步第 5 点；还不行就给 token 再加一条 **Zone → Zone → Read** 权限，范围同样只选这个域名 |
| `Cloudflare 找不到 A 记录 node.example.com` | 第 1 步的记录没建，或完整域名填错了，也可能建成了 CNAME 等其他类型 |
| `Invalid API Token` / `Authentication error` | token 复制错了或已被删除，重新创建一个 |

### 一个服务器一个域名

多台服务器时，**每台用自己的子域名**，例如 `node1.example.com`、`node2.example.com`，各建一条 A 记录。

API Token 可以共用同一个，也可以每台单独建一个。单独建更安全：某台服务器出问题时，只需要删掉它那一个 token。

### 换 IP 后域名为什么还连不上？

Cloudflare 那边通常几秒内就生效了，换 IP 完成的通知里会显示「✅ 已生效」。但国内的 DNS（114、阿里、运营商等）经常**不遵守 TTL**，会把旧 IP 多缓存几分钟到十几分钟。这段时间里：

- 客户端用域名会连到旧 IP，所以连不上
- 急用的话，直接用通知里写的新 IP 连接
- 电脑上可以把 DNS 换成 `1.1.1.1` / `8.8.8.8`。Mac 还可以运行 `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder` 清掉本机缓存

这是 DNS 缓存的特性，程序没办法彻底解决。所以自动换 IP 只在真正被墙或 Netflix 不达标时才会触发。

## 家庭探针

服务器自己只能从台湾往国内测，发现不了「只封了节点端口」和「某地线路不通」。探针反过来：放在你国内常开的设备上（unraid / NAS / 路由器 / 电脑），每 3 分钟从家里连一次节点端口，把结果报给 pqs-guard。

判定规则：

| 探针结果 | 处理 |
|---|---|
| 全部探针都连不上节点端口 | 算被墙，连续 2 次后自动换 IP |
| 全部探针同时失联（连服务器都连不上） | 同上（服务器自身断网时不算） |
| 只有部分地区连不上 | 只推送「线路提醒」，不换 IP |
| 刚换过 IP 的 20 分钟内 | 不采信（家里 DNS 还缓存着旧 IP） |

### 服务器端开启

```bash
pqs-guard probe
```

按提示填节点端口（会自动检测 x-ui 的监听端口）、探针名称（如 `浙江,天津`）、上报端口（默认 58080，脚本会自动放行防火墙）。结束时会打印每个探针的安装命令。

### 探针端：Docker 方式（unraid 推荐）

镜像 `ghcr.io/wwintj/pqs-guard/probe`只有几 MB，常驻循环、开机自启。在 unraid 终端里执行服务器打印出的那条命令即可，形如：

```bash
docker run -d --name pqs-probe-1 --restart unless-stopped \
  -e PROBE_NAME=浙江 -e PROBE_SERVER=tw.example.com -e PROBE_PORT=58080 -e PROBE_KEY=密钥 \
  ghcr.io/wwintj/pqs-guard/probe:latest
```

- 看日志：`docker logs -f pqs-probe-1`，每 3 分钟一行「已上报」
- 之后它会出现在 unraid 的 Docker 页里，可以像其他容器一样启停
- 不想敲命令也可以在 Docker 页点 **Add Container**：Repository 填 `ghcr.io/wwintj/pqs-guard/probe:latest`，Network Type 选 bridge，再用 **Add another Path, Port, Variable** 加 4 个 Variable：`PROBE_NAME`、`PROBE_SERVER`、`PROBE_PORT`、`PROBE_KEY`
- 国内拉 ghcr.io 慢的话，可在 unraid 的 Docker 设置里加镜像加速，或先在能上网的机器上 `docker pull` 再导入

### 探针端：脚本方式（没有 Docker 的设备）

1. 下载脚本（unraid 放 flash 盘 `/boot/config/`，重启不丢）：
   ```bash
   curl -fsSL https://raw.githubusercontent.com/wwintj/pqs-guard/main/probe.sh -o /boot/config/pqs-probe.sh && chmod +x /boot/config/pqs-probe.sh
   ```
2. 先手动跑一次服务器打印的命令，应看到「已上报」：
   ```bash
   bash /boot/config/pqs-probe.sh 浙江 tw.example.com 58080 密钥
   ```
3. 让它每 3 分钟跑一次：安装 **User Scripts** 插件（Apps 里搜），新建脚本，内容就是上面那行；Schedule 选 **Custom**，填 `*/3 * * * *`。
   也可以不用 cron，改成常驻：在命令末尾加 `--loop`，用 User Scripts 的 **At First Array Start Only** 运行，并勾选 Run in Background。

其他设备：OpenWrt / 群晖 / Linux 用 cron 同样的命令；只需要 `bash` 和 `curl`。
脚本也接受环境变量 `PROBE_NAME` / `PROBE_SERVER` / `PROBE_PORT` / `PROBE_KEY` / `PROBE_LOOP=1`，Docker 镜像就是这样用的。

### 查看

Telegram 面板会多一行 `🏠 探针　浙江 ✅ · 天津 ✅`；⚙️ 更多 → 🏠 探针状态 能看到每个探针的上报时间、每个端口的结果和来源 IP。超过 10 分钟没上报显示「离线」。

## 检测原理与局限

- **被墙**：IP 被墙后，服务器和国内之间的流量两个方向都会被丢弃。程序先确认国外目标能通，排除服务器自身断网，再用 TCP 连国内的几个公共 DNS（114 / DNSPod / 百度 / 阿里），全部连不上才算被墙。
  只封某个端口、或只做 SNI 阻断的情况探测不到。
- **Netflix**：请求自制剧 `81280792` 和非自制剧 `70143836`（绝命毒师）两个标题页，看返回的状态码。网络出错算「未知」，不会因此换 IP。

## 安全

- token、接口 URL 都只写在服务器本地的配置文件里，不会写进代码。
- 拿到 PQS 换 IP 接口 URL 的人都能给你换 IP，请不要泄露。
