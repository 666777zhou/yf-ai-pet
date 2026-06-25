# AI 猫（AI Cat）— 网络架构

> 本文档覆盖项目的完整网络链路：域名 → DNS → Cloudflare Tunnel → 服务器 → ESP32。

---

## 目录

1. [网络拓扑](#1-网络拓扑)
2. [域名与 DNS](#2-域名与-dns)
3. [Cloudflare Tunnel](#3-cloudflare-tunnel)
4. [服务器端口布局](#4-服务器端口布局)
5. [ESP32 网络配置](#5-esp32-网络配置)
6. [日常运维命令](#6-日常运维命令)
7. [故障排查](#7-故障排查)
8. [踩坑记录：为什么不用端口转发](#8-踩坑记录为什么不用端口转发)

---

## 1. 网络拓扑

```
┌─────────────────────────────────────────────────────────────────┐
│  外网（公网）                                                      │
│                                                                   │
│  ESP32-S3 ─── WiFi ─── wss://cat.yfcat.fun/ws                    │
│       │                                                           │
│       │  TLS 1.3 + WebSocket (WSS)                               │
│       ▼                                                           │
│  Cloudflare Edge (Anycast)                                       │
│  · TLS 终止（CF 公共 CA 证书）                                     │
│  · 根据 Host: cat.yfcat.fun 查 CNAME → 匹配隧道 ai-cat            │
│  · 通过 QUIC 隧道转发到本地 cloudflared                            │
│       │                                                           │
│       │ QUIC (UDP 7844), fallback HTTP/2                          │
│       ▼                                                           │
│  家庭网络 (192.168.2.0/24)                                        │
│       │                                                           │
│  cloudflared (systemd user service)                              │
│  · 出站连接到 CF，无需开放任何入站端口                               │
│  · ingress 规则：cat.yfcat.fun → http://127.0.0.1:8081            │
│       │                                                           │
│       │ plain HTTP (localhost)                                    │
│       ▼                                                           │
│  AI Cat Server (Python asyncio)                                  │
│  · WS: 0.0.0.0:8081/ws (LAN + Tunnel upstream)                  │
│  · WSS: 0.0.0.0:8080/ws (预留，自签证书)                         │
└─────────────────────────────────────────────────────────────────┘
```

**关键点：** 服务器无需公网 IP、无需端口转发、无需 DMZ。cloudflared 主动发起出站 QUIC 连接，所有入站流量通过 Cloudflare 边缘代理。

---

## 2. 域名与 DNS

### 域名信息

| 项目 | 值 |
|------|-----|
| 域名 | `yfcat.fun` |
| 注册商 | 阿里云（万网） |
| DNS 托管 | Cloudflare（免费计划） |
| CF 账户 | Google 登录 |
| NS 记录 | `elisa.ns.cloudflare.com` / `wilson.ns.cloudflare.com` |

### DNS 记录

| 类型 | 名称 | 值 | 代理 |
|------|------|-----|------|
| CNAME | `cat.yfcat.fun` | `cdbfd589-....cfargotunnel.com` | 🟠 Proxied |

**添加新子域名：**
```bash
# 方式 1：cloudflared 自动创建 CNAME + 开启代理
~/bin/cloudflared tunnel route dns ai-cat <subdomain>.yfcat.fun

# 方式 2：Cloudflare 控制台手动添加 CNAME → 同一个 cfargotunnel.com 地址
```

### NS 修改流程（从阿里云切换到 Cloudflare）

1. 域名实名认证通过
2. 阿里云控制台 → 域名管理 → DNS 修改 → 自定义 DNS
3. 填入 Cloudflare 提供的两个 NS 地址
4. 等待 Cloudflare 验证（1-24 小时）
5. Cloudflare Dashboard → DNS → 确认状态 Active

---

## 3. Cloudflare Tunnel

### 隧道信息

| 项目 | 值 |
|------|-----|
| 隧道名称 | `ai-cat` |
| 隧道 ID | `cdbfd589-bc66-43b1-81f1-499893ab112a` |
| 二进制 | `~/bin/cloudflared` v2026.6.1 |
| 配置文件 | `~/.cloudflared/config.yml` |
| 凭证文件 | `~/.cloudflared/cdbfd589-....json` |
| 协议 | QUIC（UDP），fallback HTTP/2 |
| 延迟 | +30~50ms |

### 配置文件：`~/.cloudflared/config.yml`

```yaml
tunnel: cdbfd589-bc66-43b1-81f1-499893ab112a
credentials-file: /home/zyf/.cloudflared/cdbfd589-bc66-43b1-81f1-499893ab112a.json

ingress:
  - hostname: cat.yfcat.fun
    service: http://127.0.0.1:8081
  # 添加更多服务只需加规则：
  # - hostname: api.yfcat.fun
  #   service: http://127.0.0.1:5000
  - service: http_status:404
```

**一个隧道可承载多个子域名**，只需：添加 DNS CNAME + 加一行 ingress 规则 → `systemctl --user restart cloudflared`。

### 完整搭建流程（从零开始）

```bash
# 1. 登录 Cloudflare
~/bin/cloudflared tunnel login

# 2. 创建隧道
~/bin/cloudflared tunnel create ai-cat

# 3. 配置 DNS 路由（自动创建 CNAME + 开启代理）
~/bin/cloudflared tunnel route dns ai-cat cat.yfcat.fun

# 4. 编写 config.yml（见上方）

# 5. 安装为系统服务（见第 6 节）

# 6. 启动
systemctl --user start cloudflared
```

---

## 4. 服务器端口布局

| 端口 | 协议 | 用途 | 绑定 |
|------|------|------|------|
| **8081** | WS (plain) | LAN 访问 + Cloudflare Tunnel 上游 | `0.0.0.0:8081` |
| **8080** | WSS (TLS) | 预留（自签证书，不用于生产） | `0.0.0.0:8080` |

**TLS 分工：** Cloudflare 负责公网 TLS 终止（CF 公共证书），服务器只需处理 plain WebSocket。自签证书 `server/certs/` 仅用于历史 WSS 尝试，保留备用。

---

## 5. ESP32 网络配置

### WebSocket URL

固件配置文件：`firmware/main/ai_cat_board.h`

```c
// Cloudflare Tunnel — permanent named tunnel
#define SERVER_WEBSOCKET_URL  "wss://cat.yfcat.fun/ws"
```

### TLS 证书验证

ESP32 使用 ESP-IDF 内置 Mozilla CA 证书包验证 Cloudflare 的公共证书：

```cpp
#include <esp_crt_bundle.h>
// ...
ws_cfg.crt_bundle_attach = esp_crt_bundle_attach;
```

`CMakeLists.txt` 需添加 `mbedtls` 依赖（`esp_crt_bundle` 属于 mbedtls 组件）。

### WiFi 配网

当前使用 SoftAP 模式（`esp-wifi-connect` 组件）—— 首次开机后用手机连接 ESP32 热点，通过 captive portal 配置 WiFi。

---

## 6. 日常运维命令

### cloudflared 服务管理

```bash
systemctl --user status cloudflared        # 查看状态
systemctl --user restart cloudflared       # 重启（改了 config.yml 后）
systemctl --user stop cloudflared          # 停止
journalctl --user -u cloudflared -f        # 实时日志
journalctl --user -u cloudflared -n 50     # 最近 50 行日志
```

### AI Cat 服务器管理

```bash
# 启动（后台运行）
cd /home/zyf/Code/yf-ai-pet/server
source ~/miniconda3/etc/profile.d/conda.sh && conda activate ai-cat
nohup python main.py > /tmp/ai-cat-server.log 2>&1 &

# 查看日志
tail -f /tmp/ai-cat-server.log

# 查看进程
ps aux | grep main.py
```

### DNS 诊断

```bash
dig @1.1.1.1 cat.yfcat.fun A         # 应返回 Cloudflare 边缘 IP
dig @1.1.1.1 cat.yfcat.fun CNAME     # 应返回 cfargotunnel.com
dig NS yfcat.fun +short               # 应返回 Cloudflare NS
```

### 连通性测试

```bash
# HTTPS
curl -sI --max-time 10 https://cat.yfcat.fun/

# WebSocket
python3 -c "
import asyncio, websockets
async def test():
    async with websockets.connect('wss://cat.yfcat.fun/ws') as ws:
        print('WSS connected')
asyncio.run(test())
"
```

---

## 7. 故障排查

### DNS 返回空 / 错误 IP

1. `dig @1.1.1.1 cat.yfcat.fun A` —— 如果返回 `cfargotunnel.com` 而非 Cloudflare IP，说明**代理（橙色云朵）没开启**
2. 到 Cloudflare Dashboard → DNS → Records → 确认 CNAME 记录的代理状态是 🟠 Proxied

### cloudflared 连接失败

1. 检查服务状态：`systemctl --user status cloudflared`
2. 检查日志：`journalctl --user -u cloudflared -n 30`
3. 常见原因：
   - 网络不通（检查能否访问 `api.cloudflare.com:443`）
   - 证书过期（重新 `cloudflared tunnel login`）
   - config.yml 语法错误

### ESP32 连不上 wss://

1. 先用电脑验证 WSS 连通性（见上方测试命令）
2. 检查 ESP32 WiFi 是否正常连接
3. 确认固件中 `SERVER_WEBSOCKET_URL` 是否正确
4. 确认 `esp_crt_bundle_attach` 已配置

### 本地服务器没启动

```bash
ss -tln | grep 8081    # 应看到 LISTEN
```

---

## 8. 踩坑记录：为什么不用端口转发

| 层级 | 问题 |
|------|------|
| **ISP** | 仅开放端口 80 和 8080。443、8765、9000 等全部 TCP SYN 超时 |
| **路由器端口 8080** | 路由器自身远程管理 Web UI 绑定 WAN:8080，所有流量（HTTP/TLS/WebSocket）被拦截 |
| **路由器端口 80** | 返回 HTTP 403 |
| **DMZ** | 设了也没用——ISP 层面就拦了，数据包到不了路由器 |
| **WSS on 8080** | TLS 加密理论上可绕过路由器检测，但路由器进程本身占了 8080，TLS ClientHello 也被拦截 |

**结论：** ISP + 路由器组合拳使得端口转发完全不可行。Cloudflare Tunnel（服务器主动出站）是唯一可行的远程访问方案。
