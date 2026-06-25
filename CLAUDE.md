# AI 猫 (AI Cat) — 项目总览

> 一只会呼噜、会动耳朵、会说话的 AI 毛绒猫。
> 双层产品策略：标准品种款（工厂量产）+ 1对1 定制款（羊毛毡手工）。

## 架构

```
┌─────────────────────────────────────────────────┐
│  毛绒猫实体（ESP32-S3 固件）                      │
│  · 触摸传感器 ×3（头顶/背/肚皮）                   │
│  · 舵机 ×2（耳朵动作）                            │
│  · MEMS 麦克风 + 扬声器                          │
│  · 震动马达（呼噜模拟）                           │
│  · 电池 18650×2 + USB-C 充电                    │
│       │ WiFi / WSS (WebSocket over TLS)         │
│       ▼                                         │
│  Cloudflare Tunnel (cloudflared systemd 服务)    │
│  · TLS 终止 + 反向代理                           │
│  · 无需公网 IP/端口转发/DMZ                       │
│       │ QUIC outbound → CF Edge                 │
│       ▼                                         │
│  3090 服务器（Python asyncio）                    │
│  · STT: Faster-Whisper large-v3 (CUDA)          │
│  · LLM: Qwen3-14B via Ollama (本地)              │
│  · TTS: Qwen3-TTS (声音克隆) / Fish-Speech / Piper │
│  · 情绪状态机：content/sleepy/playful/annoyed/…  │
└─────────────────────────────────────────────────┘
```

**详细网络文档：** `docs/network-architecture.md`

## 目录结构

```
yf-ai-pet/
├── firmware/              # ESP32-S3 固件（ESP-IDF v5.5.1, C++17）
│   ├── CMakeLists.txt     # 顶层 CMake，项目名 ai-cat
│   ├── sdkconfig.defaults # ESP32-S3 16MB Flash, 8MB Octal PSRAM, WiFi 配置
│   ├── partitions.csv     # OTA 双分区 + SPIFFS 资源分区（16MB Flash）
│   ├── main/
│   │   ├── CMakeLists.txt       # 源文件注册 + 依赖声明
│   │   ├── main.cc              # 固件入口：初始化 → WiFi → WebSocket → 主循环 (10Hz)
│   │   ├── ai_cat_board.{h,cc}  # 板级初始化：I2C 总线、ES8311+ES7210 音频、GPIO
│   │   ├── cat_websocket.{h,cc} # WebSocket 客户端：JSON 命令 + Opus 音频双向通信
│   │   ├── wifi_manager.{h,cc}  # WiFi 连接管理（静态 SSID/PWD）
│   │   ├── box_audio_codec.{h,cc} # ES8311(输出)+ES7210(输入) 音频驱动
│   │   ├── audio_codec.{h,cc}   # 音频编解码器抽象接口
│   │   ├── i2c_device.{h,cc}    # I2C 设备基类
│   │   └── pca9557.{h,cc}       # PCA9557 GPIO 扩展（音频功放控制）
│   └── components/
│       └── esp_codec_dev/       # ESP 音频编解码器驱动组件
├── server/                # AI 猫服务器（Python 3.10+）
│   ├── main.py                  # 入口：WebSocket 服务 + STT/TTS/LLM 初始化
│   ├── websocket_handler.py     # ESP32 连接管理：JSON 命令 + Opus 音频处理
│   ├── cat_brain.py             # 猫人格引擎：情绪状态机 + LLM prompt 模板
│   ├── stt_engine.py            # STT：Faster-Whisper on CUDA
│   ├── tts_engine.py            # TTS：Edge-TTS（v1），预留本地 TTS
│   ├── audio_utils.py           # Opus 编解码、VAD（能量检测）、PCM 工具
│   └── requirements.txt         # websockets, faster-whisper, edge-tts, opuslib, numpy
└── docs/                  # 产品方案 + 技术文档
    ├── data-flow-architecture.md # 数据流与系统架构（详细）
    ├── network-architecture.md   # 网络链路（域名/Tunnel/DNS/运维）
    ├── quick-mvp-design.md       # 快速 MVP 方案（1 周跑通链路）
    ├── product-strategy-design.md # 完整产品策略
    └── embedded-learning-path.md # 嵌入式学习路线
```

## 硬件平台

| 组件 | 型号 | 连接 |
|------|------|------|
| 主板 | ESP32-S3（立创实战派标准版） | — |
| 音频输出 | ES8311 DAC | I2S (MCLK=38, BCLK=14, WS=13, DOUT=45) |
| 音频输入 | ES7210 ADC | I2S (DIN=12) |
| I2C 总线 | 共享总线 | SDA=GPIO1, SCL=GPIO2 |
| GPIO 扩展 | PCA9557 (0x19) | I2C — 音频功放控制 |
| 舵机驱动 | PCA9685 (0x40) | I2C — 耳朵舵机 PWM |
| 触摸 ×3 | ESP32 电容触摸 | GPIO3/4/5（头顶/背/肚皮） |
| 震动马达 | ERM 1027 | GPIO10 (LEDC PWM) |
| 状态 LED | — | GPIO48（板载） |
| BOOT 按钮 | — | GPIO0 |

## 通信协议（ESP32 ↔ 服务器）

WebSocket 长连接 over Cloudflare Tunnel，JSON + 二进制混合。

**连接地址：** `wss://cat.yfcat.fun/ws`（永久域名，Cloudflare TLS 终止 → Tunnel → `localhost:8081`）

**ESP32 → 服务器：**
```json
{"type": "sensors", "ts": 1234567890, "touch_head": 85, "touch_back": 20, "touch_belly": 5, "battery_pct": 92}
```
二进制帧：Opus 编码的麦克风音频（60ms/帧, 16kHz, 32kbps）

**服务器 → ESP32：**
```json
{"type": "command", "emotion": "content", "ear_left_deg": 60, "ear_right_deg": 55, "vibration": 150, "has_audio": true}
```
二进制帧：TTS 生成的声音（MP3/Opus）

## 编译固件

**环境要求：** ESP-IDF v5.5.1

```bash
# 1. 设置 ESP-IDF 环境（只需一次）
cd ~/esp/v5.5.1/esp-idf
./install.sh esp32s3        # 安装工具链（如未安装）
source export.sh            # 设置环境变量

# 2. 编译
cd /home/zyf/Code/yf-ai-pet/firmware
idf.py set-target esp32s3  # 首次执行
idf.py build               # 编译

# 3. 烧录
idf.py -p /dev/ttyUSB0 flash monitor  # 烧录 + 串口监视
```

**注意：** 首次编译前需要修改 `ai_cat_board.h` 中的 WiFi SSID/密码和 WebSocket 服务器地址。

## 运行服务器

```bash
cd /home/zyf/Code/yf-ai-pet/server
pip install -r requirements.txt
conda activate ai-cat
python main.py  # WS: 0.0.0.0:8081/ws, WSS: 0.0.0.0:8080/ws (预留)
```

**LLM 接入：** 本地 Ollama + Qwen3-14B（`http://127.0.0.1:11434/api/chat`）。

## 网络链路管理

```bash
# Cloudflare Tunnel 服务
systemctl --user status cloudflared        # 查看状态
systemctl --user restart cloudflared       # 重启（改了 config.yml 后）
journalctl --user -u cloudflared -f        # 实时日志

# DNS 诊断
dig @1.1.1.1 cat.yfcat.fun A              # 应返回 CF 边缘 IP
```

**详细网络文档：** `docs/network-architecture.md`

## 开发状态

| 模块 | 状态 | 备注 |
|------|------|------|
| 固件框架 | ✅ 骨架完成 | main.cc 主循环、WebSocket、板级初始化 |
| 音频链路 | ⚠️ 未调通 | 驱动代码就位，未在真实硬件上验证 |
| 舵机控制 | 🔲 待实现 | PCA9685 驱动代码未编写 |
| 触摸传感器 | ⚠️ 占位 | 目前用 GPIO 数字读代替电容触摸 API |
| Opus 编码(固件) | ✅ 已实现 | 固件侧 Opus 编码已集成（60ms帧, 32kbps） |
| 服务器 WebSocket | ✅ 基本完成 | JSON 命令 + Opus 音频 + VAD + 情绪状态机 |
| STT | ✅ 基本完成 | Faster-Whisper large-v3 on CUDA |
| TTS | ✅ 多引擎 | Qwen3-TTS(默认,声音克隆) + Fish-Speech + Piper + GPT-SoVITS |
| LLM 集成 | ✅ 本地 Ollama | Qwen3-14B @ 127.0.0.1:11434 |
| 硬件焊接 | 🔲 未开始 | 元件清单见 docs/quick-mvp-design.md |

## TTS 引擎对比

| 引擎 | 类型 | 速度 | 音质 | 声音克隆 | 启动方式 |
|------|------|------|------|---------|---------|
| Qwen3-TTS | 本地 HTTP (1.7B) | ~2.6s/句 | ⭐⭐⭐⭐⭐ | ✅ | `conda activate qwen3-tts && python tts_server.py` |
| Fish-Speech | 本地 HTTP (4B) | ~14s/句 | ⭐⭐⭐⭐ | ✅ | `conda activate fish-speech && python tts_server.py` |
| Piper | 进程内 ONNX | ~50ms/句 | ⭐⭐ | ❌ 固定音色 | 自动加载 |
| GPT-SoVITS | HTTP API | — | ⭐⭐⭐ | ✅ | 端口 9872 |

## 已完成的优化

### Qwen3-TTS torch.compile（2026-06-20）
- 文件：`/home/zyf/Code/Qwen3-TTS/tts_server.py`
- `torch.compile(model.model, mode="reduce-overhead", fullgraph=False)`
- 启动时自动 warmup（短文本触发 JIT 编译，首次 10-30s）
- 环境变量 `QWEN_TTS_COMPILE=0` 可禁用
- 预期收益：10-30% 推理延迟降低
- `mode="reduce-overhead"` 使用 CUDA graphs 减少 kernel launch 开销

## 参考项目：xiaozhi-esp32

`~/Code/old/xiaozhi-esp32/` 是一个跑通了的 ESP32 语音交互项目，可作为显示驱动和硬件初始化的代码参考。

### 架构

```
app_main()
  → Application::Initialize()
      → Board 初始化（I2C、SPI、音频、显示）
      → display->SetupUI()
      → 音频服务 + 网络回调注册
  → Application::Run()
      → FreeRTOS 事件循环
      → 处理：网络、唤醒词、VAD、时钟、状态变化
```

### 显示子系统

```
Display（抽象基类）
  → LvglDisplay → LcdDisplay → SpiLcdDisplay  （LVGL + SPI LCD）
  → emote::EmoteDisplay                         （轻量 AAF 动画）
  → OledDisplay                                 （OLED）
```

- 驱动芯片：ESP-IDF 内置 `esp_lcd` 组件（`esp_lcd_panel_st7789`、`esp_lcd_panel_ili9341` 等）
- 立创开发板配置：ST7789，320×240，SPI3_HOST，MOSI=40, SCLK=41, DC=39, CS=NC, RST=NC, BL=42(反相)
- 颜色格式：RGB565，16bpp
- 可选 LVGL（`espressif__lvgl` 托管组件）或 `esp_emote_gfx` 轻量动画库

### 音频

- 与 AI 猫使用相同的 BoxAudioCodec（ES8311 + ES7210）
- 采样率 24000（立创板），AI 猫用 16000
- I2S 引脚：MCLK=38, BCLK=14, WS=13, DIN=12, DOUT=45
- I2C 引脚：SDA=1, SCL=2
- PCA9557 GPIO 扩展（0x19）控制音频功放

### 关键文件速查

| 文件 | 作用 |
|------|------|
| `main/application.cc` | 应用主循环、事件处理 |
| `main/boards/common/board.h` | Board 抽象基类 |
| `main/boards/lichuang-dev/lichuang_dev_board.cc` | 立创板实现（I2C/SPI/显示/触摸/摄像头） |
| `main/boards/lichuang-dev/config.h` | 立创板引脚定义 |
| `main/display/lcd_display.cc` | LVGL 显示核心（SetupUI、SetEmotion） |
| `main/display/emote_display.cc` | Emote 轻量动画显示 |
| `main/Kconfig.projbuild` | 电路板/显示/功能配置选项 |

## 关键设计决策

- **固件用 C++17 而非 C**：模块化更好，WebSocket/音频驱动适合用类封装
- **JSON 用 cJSON**：轻量级，ESP-IDF 友好
- **音频帧 60ms**：平衡延迟和带宽，适合 Opus 编码
- **WebSocket 无断线重连机制 v1**：MVP 阶段简化，依赖 WiFi 稳定性
- **服务器端不做声纹识别**：v1 只做 VAD + STT，多猫场景留到 v2
- **无 OTA 固件升级**：v1 用 USB 烧录，OTA 留到 v2
