# AI 猫（AI Cat）— 数据流与系统架构

> 本文档详细描述 AI 猫项目的**完整数据流**，覆盖固件端（ESP32-S3）和服务器端（Python）。

---

## 目录

1. [系统总览](#1-系统总览)
2. [通信协议](#2-通信协议)
3. [上行数据流：麦克风 → 文本](#3-上行数据流麦克风--文本)
4. [下行数据流：回复文本 → 扬声器](#4-下行数据流回复文本--扬声器)
5. [传感器数据流](#5-传感器数据流)
6. [情绪状态机](#6-情绪状态机)
7. [文件级函数调用关系](#7-文件级函数调用关系)
8. [线程/任务模型](#8-线程任务模型)
9. [自检清单](#9-自检清单)

---

## 1. 系统总览

```
┌──────────────────────────────────────────────────────────────────┐
│  ESP32-S3 固件 (C++17 / ESP-IDF 5.5.1)                         │
│                                                                  │
│  ┌──────────┐   60ms PCM帧    ┌────────────┐   Opus帧  ┌──────┐ │
│  │ 麦克风(I2S)│ ──────────────→ │ Opus Encoder │ ───────→ │      │ │
│  │ ES7210   │   16kHz/16bit   │ (960 samples)│          │      │ │
│  └──────────┘                 └────────────┘          │      │ │
│                                                        │ WS  │ │
│  ┌──────────┐   PCM帧(来自Opus) ┌────────────┐  Opus帧  │ 连  │ │
│  │ 扬声器(I2S)│ ←────────────── │ Opus Decoder │ ←─────── │ 接  │ │
│  │ ES8311   │                 └────────────┘          │      │ │
│  └──────────┘                                         │      │ │
│                                                        │      │ │
│  ┌──────────┐   JSON: sensors   ┌──────────────┐      │      │ │
│  │ 触摸×3    │ ───────────────→ │ CatWebSocket │ ────→ │      │ │
│  │ GPIO3/4/5│                  │  (wss 客户端) │       └──┬───┘ │
│  └──────────┘                  └──────────────┘          │     │
│                                                          │     │
│  ┌──────────┐   JSON: command    接收 ←─────────────────┘     │
│  │ 舵机×2   │ ←───────────────                              │
│  │ PCA9685  │   耳角度 + 震动PWM                             │
│  └──────────┘                                              │
│                                                            │
│  ┌──────────┐                                              │
│  │ 显示屏    │ ← 待调试 (ST7789 SPI)                        │
│  │ ST7789   │  EyeDisplay::Render()                        │
│  └──────────┘                                              │
├──────────────────────────────────────────────────────────────┤
│  ︎ ︎ ︎TCP/IP (WiFi)                                              │
├──────────────────────────────────────────────────────────────┤
│  3090 服务器 (Python 3.10+ / asyncio)                       │
│                                                            │
│  ┌────────────────┐    ┌────────────┐    ┌──────────────┐  │
│  │ CatConnection   │──→ │ STT: WHISPER│──→│ STT纠错: LLM │  │
│  │ (WS Handler)   │    │ large-v3   │   │ (Qwen3-14B)  │  │
│  │                │    └────────────┘    └──────┬───────┘  │
│  │ · VAD检测      │                            │           │
│  │ · Opus解码     │                            ▼           │
│  │ · 情绪更新     │    ┌──────────────────────────┐         │
│  │ · 回复下发     │←───│      CatBrain (猫人格)      │         │
│  └────────────────┘    │ · 情绪状态机 (6状态)        │         │
│                         │ · LLM Prompt 模板         │         │
│  ┌────────────────┐    │ · 对话历史管理              │         │
│  │ TTS: Qwen3-TTS │←───└──────────────────────────┘         │
│  │ (本地HTTP API) │                                        │
│  │ → Opus帧      │────→ CatConnection → WebSocket → ESP32  │
│  └────────────────┘                                        │
│                                                            │
│  ┌────────────────┐   /voice命令行界面                      │
│  │ VoiceManager   │   · list / use / add / delete 音色     │
│  │ (多引擎管理)    │                                        │
│  └────────────────┘                                        │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. 通信协议

WebSocket 长连接，JSON + 二进制混合。

### 2.1 ESP32 → 服务器（上行）

**JSON 帧（传感器数据，10Hz）：**
```json
{
  "type": "sensors",
  "ts": 1234567890,
  "touch_head": 85,
  "touch_back": 20,
  "touch_belly": 5,
  "battery_pct": 92
}
```

**二进制帧（麦克风音频，60ms间隔）：**
- 内容：Opus 编码后的音频（60ms 帧）
- 采样率：16kHz，单声道，16bit
- Opus 码率：32kbps
- 原始 PCM 帧大小：960 samples × 2 bytes = 1920 bytes
- Opus 帧大小：~60-240 bytes（取决于音频内容）

### 2.2 服务器 → ESP32（下行）

**JSON 帧（动作指令，随情绪变化推送）：**
```json
{
  "type": "command",
  "emotion": "content",
  "ear_left_deg": 60,
  "ear_right_deg": 55,
  "vibration": 150,
  "has_audio": true
}
```

**二进制帧（TTS 音频）：**
- 内容：Opus 编码后的合成语音（60ms 帧）
- 编码参数同上
- 连续发送多帧组成一句话

### 2.3 连接管理

| 特性 | 实现 |
|------|------|
| 地址 | `ws://192.168.2.138:8080/ws` |
| 自动重连 | 启用，5s 间隔 |
| Ping/Pong | 每 10s，30s 超时 |
| TCP Keepalive | 5s 空闲，5s 间隔，3 次重试 |
| 接收缓冲区 | 16384 bytes |
| 内部任务优先级 | 8 |
| 内部任务栈 | 16384 bytes |

---

## 3. 上行数据流：麦克风 → 文本

这是最关键的音频链路，涉及 7 个组件。

### 3.1 固件端（ESP32）

```
ES7210 麦克风
  │ I2S DMA (16kHz, 16bit, mono)
  ▼
box_audio_codec.cc :: Read()
  每次读取 960 samples (= 60ms @ 16kHz)
  ▼
main.cc :: [主循环] (while true, 每10ms tick一次)
  │ 调用 codec->Read(pcm, FRAME_SAMPLES)
  │ 如果读到一帧完整的 960 样本:
  │   通过 xStreamBufferSend 发送到编码器任务
  ▼
EncoderTask (独立 FreeRTOS 任务, 32KB 栈)
  │ xStreamBufferReceive 阻塞等待 PCM 帧
  │ 调用 AudioEncoder::Encode() → opus_encode()
  │ 输出 ~60-240 bytes Opus 帧
  ▼
CatWebSocket::SendAudio()
  │ esp_websocket_client_send_bin() 发送二进制帧
  ▼
  ＝＝WebSocket (WiFi/TCP/IP)＝＝
```

**关键设计：**
- 编码器任务与主循环分离：`opus_encode()` 需要大量栈空间（~8-12KB），放在主任务里容易爆栈
- PCM 到编码器的传递用 `xStreamBuffer`：零堆分配，存储区放在 PSRAM 中避免内部 DRAM 不足
- 主循环 10ms 一次，但 PCM 帧每 60ms 才有一帧
- 音频采集在 TTS 播放时暂停（`!g_audio_playing`），防止采集到自己的声音

### 3.2 服务器端

```
  ＝＝WebSocket (WiFi/TCP/IP)＝＝
  │
CatConnection::_recv_loop() ─── 独立的 asyncio 协程
  │  async for message in websocket:
  │    if isinstance(message, bytes):
  │      → _handle_audio()
  │    else:
  │      → _handle_json()
  ▼
CatConnection::_handle_audio()
  │ OpusCodec::decode() → 1920 bytes PCM
  │ 追加到 self.pcm_buffer (bytearray)
  ▼
CatConnection::_process_loop() ─── 另一个 asyncio 协程
  │ 每 60ms 执行一次 (asyncio.sleep(0.06))
  │ 从 pcm_buffer 取一帧 PCM
  │
  ├─→ VAD::is_speech()  ─── 自适应能量 VAD
  │   │ 实时追踪噪声基底 (noise_floor)
  │   │ speech 能量需 > noise_floor × 3.0
  │   │ 静音 > 30 帧 (1.8s) 才切回非说话状态
  │   │
  │   ├─→ 有语音: 累计到 accumulated_speech_pcm
  │   │   │ speech_frames ≥ 10 帧 (~600ms) → 启动后台流式 STT
  │   │   │ speech_frames ≥ 50 帧 (~3s)      → 强制结束（最大长度）
  │   │   │
  │   │   └→ 静音 > 8 帧 (~480ms) → 结束一句话
  │   │
  │   └─→ 结束条件触发
  │       │
  ▼       ▼
CatConnection::_finalize_utterance()
  │ 能量过滤 (avg_energy > 0.01 才转写)
  ▼
CatConnection::_transcribe()
  │ 1. _build_stt_context() ─ 最近5轮对话作为 context
  │ 2. stt.transcribe(pcm, initial_prompt=context)
  ▼
STTEngine :: faster-whisper (large-v3, CUDA, float16)
  │ beam_size=8, best_of=8
  │ 内置 VAD 过滤
  │ 多温度退火 [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
  │ 返回中文文本
  ▼
CatBrain :: (含 STT 纠错，当前跳过)
  │ 目的是纠沟通音字/错字
  │ 当前版本因耗时 ~1s 而默认跳过
  ▼
  用户文本 (例如："你今天开心吗")
```

**VAD 参数：**
| 参数 | 值 | 含义 |
|------|-----|------|
| 帧长 | 60ms | 每次分析一帧 |
| 最小发言 | 5 帧 (300ms) | 少于这个长度不触发 |
| 流式 STT 触发 | 10 帧 (600ms) | 后台启动 Whisper 转写 |
| 静音结束阈值 | 8 帧 (480ms) | 连续静音超过这个值判定一句话结束 |
| 最长语句 | 50 帧 (3s) | 到时间强制结束 |
| 能量阈值 | 动态 | noise_floor × 3.0，最低 0.008 |
| 噪声基底自适应 | α=0.05 | 静音帧更新噪声估计 |

---

## 4. 下行数据流：回复文本 → 扬声器

### 4.1 文本生成（服务器端）

```
用户文本 (例如："你今天开心吗")
  │
CatConnection::_respond()
  ▼
CatBrain :: process()
  │ 1. EmotionFSM::update() ─ 更新情绪状态
  │ 2. 构建 LLM Prompt：
  │    System: "你是一只名叫小咪的AI猫..."
  │    Context: 当前情绪 + 用户文本
  │ 3. llm_callable(prompt) ── 调用 Ollama API
  │    Qwen3:14B 本地推理，temperature=0.9
  │    回复限制：~60 tokens，30字以内
  │ 4. 返回 dict: {emotion, ear_deg, vibration, response_text}
  ▼
  猫回复 (例如："(摇尾巴) 当然开心啦！看到你我就高兴")
  │
  ├─→ 记录对话历史（STT context + LLM context）
  │
  ├─→ 从文本中剥离括号动作指令: re.sub(r'[（(][^)）]*[)）]', '', text)
  │   → "当然开心啦！看到你我就高兴"
  │
  ▼
TTSEngine :: synthesize_opus_frames()
  │ 根据当前音色配置路由到对应引擎
  │
  ├─→ Qwen3-TTS (默认) ── HTTP API (localhost:9874)
  │   │ 支持声音克隆（参考音频 + prompt_text）
  │   │ torch.compile 加速
  │   │ 返回 WAV → 提取 16kHz 16bit PCM
  │   │
  ├─→ Fish-Speech (备选) ── HTTP API (localhost:9873)
  │   │ 4B 参数，质量高但慢 (~14s/句)
  │   │
  ├─→ Piper (超快备选) ── 进程内 ONNX 推理
  │   │ ~50ms/句，固定音色
  │   │
  └─→ GPT-SoVITS ── HTTP API (localhost:9872)
  │
  ▼
  PCM → Opus 编码
  │ _pcm_to_opus() 使用 opuslib
  │ 60ms 帧，16kHz，32kbps
  │ 一回合 20-40 帧（约 1-2 秒）
  ▼
CatConnection::ws.send() 逐个发送 Opus 二进制帧
  │ 每帧间隔 10ms (asyncio.sleep(0.01))
  ▼
  ＝＝WebSocket (WiFi/TCP/IP)＝＝
```

### 4.2 播放（ESP32 固件端）

```
  ＝＝WebSocket (WiFi/TCP/IP)＝＝
  │
CatWebSocket :: WsEventHandler (事件回调，在 WS 任务上下文中)
  │ 收到 op_code=0x02 (二进制) → audio_cb_
  │ 收到 op_code=0x01 (文本)   → 解析 JSON → command_cb_
  │
  ├─→ audio_cb_ (在 main.cc 中注册)
  │   │ g_audio_playing = true (暂停上行采集，防止回声)
  │   │ AudioDecoder::Decode() → PCM
  │   │ codec->OutputData(pcm) → I2S DMA → ES8311 → 扬声器
  │   ▼
  │   播放完成后 800ms → g_audio_playing = false
  │
  └─→ command_cb_ (在 main.cc 中注册)
      │ 当前仅 LOG：TODO 更新显示/舵机/震动
      ▼
      （待实现）
```

---

## 5. 传感器数据流

### 5.1 触觉传感器

```
GPIO3/4/5 (电容触摸)
  │ 目前实现: GPIO 数字读（0/100 二值）
  │ TODO: ESP32 原生电容触摸 API (touch_pad_read)
  ▼
main.cc :: ReadTouchSensors()
  │ 主循环中每 100ms 调用一次（10Hz，TTS 播放时暂停）
  ▼
SensorPacket 结构体
  ▼
CatWebSocket::SendSensorData() → JSON → WebSocket 发送
  ▼
CatConnection::_handle_json()   (服务器端)
  │ self.touch_values = [head, back, belly]
  ▼
CatBrain :: EmotionFSM::update(touch_values)
  │ max_touch > 30 → 触摸激活
  │ 20 次连续触摸 → ANNOYED
  │ 触摸 + 说话 → PLAYFUL
  │ 仅触摸 → CONTENT
  │ 30s 无交互 → SLEEPY
  ▼
EmotionFSM::to_command() → {emotion, ear_deg, vibration}
  ▼
CatConnection::_send_command() → JSON → WebSocket 发送到 ESP32
```

### 5.2 电量

```
当前状态：硬编码 100%，TODO 读取 ADC
```

---

## 6. 情绪状态机

### 6.1 状态定义

```
┌─────────────────────────────────────────────────────┐
│                  情绪状态机 (6 状态)                   │
│                                                     │
│          ┌──────────────────────────────┐           │
│          │        touch_count > 20      │           │
│          ▼                              │           │
│  ┌──────────┐     30s无交互     ┌─────────┐        │
│  │  SLEEPY  │ ←────────────── │ CONTENT │          │
│  └──────────┘                  └────┬────┘          │
│       │                              │              │
│       │ 触摸                         │ 触摸+说话     │
│       │                              ▼              │
│       │                         ┌──────────┐        │
│       │                         │ PLAYFUL  │        │
│       │                         └────┬─────┘        │
│       │                              │              │
│       │                              │ 触摸>20次    │
│       │                              ▼              │
│       │                         ┌──────────┐        │
│       └────────────────────────→│ ANNOYED  │        │
│                                 └──────────┘        │
│                                                     │
│  SCARED / CURIOUS: 定义而尚未触发                    │
└─────────────────────────────────────────────────────┘
```

### 6.2 情绪 → 执行器映射

| 情绪 | 左耳(°) | 右耳(°) | 震动(PWM) | 显示表情 |
|------|---------|---------|-----------|---------|
| content | 60 | 55 | 150 | NORMAL |
| sleepy | 20 | 25 | 60 | SLEEPY |
| playful | 85 | 80 | 200 | HAPPY |
| annoyed | 10 | 80 | 30 | ANNOYED |
| scared | 5 | 5 | 0 | SURPRISED |
| curious | 90 | 45 | 0 | SURPRISED |

---

## 7. 文件级函数调用关系

### 7.1 固件端

```
app_main()                          [main.cc]
 ├─ AiCatBoard::Initialize()        [ai_cat_board.cc]
 │   ├─ InitializeI2c()
 │   │   └─ i2c_new_master_bus()
 │   ├─ InitializeAudio()
 │   │   ├─ BoxAudioCodec()         [box_audio_codec.cc]
 │   │   │   ├─ ES8311 init
 │   │   │   └─ ES7210 init
 │   │   └─ pca9557_->SetOutputState()
 │   ├─ InitializeGpio()
 │   └─ [InitializeDisplay() — 注释中]
 │       └─ EyeDisplay::Initialize()  [display.cc]
 │           ├─ spi_bus_initialize()
 │           ├─ esp_lcd_new_panel_st7789()
 │           └─ heap_caps_malloc(framebuffer)
 │
 ├─ WifiManager::Connect()          [wifi_manager.cc]
 │
 ├─ g_websocket.Connect()           [cat_websocket.cc]
 │   ├─ esp_websocket_client_init()
 │   ├─ esp_websocket_register_events()
 │   └─ esp_websocket_client_start()
 │
 ├─ g_websocket.OnCommand(λ)        [注册 JSON 命令回调]
 ├─ g_websocket.OnAudio(λ)          [注册音频回调]
 │
 ├─ xTaskCreate(EncoderTask)
 │   └─ EncoderTask():
 │       └─ AudioEncoder::Encode()    [audio_pipeline.cc]
 │           └─ opus_encode()
 │
 └─ 主循环 (10ms tick):
     ├─ ReadTouchSensors()
     ├─ g_websocket.SendSensorData()
     ├─ codec->Read() → xStreamBufferSend()
     └─ g_websocket.ProcessIncoming()
         └─ (事件回调处理下行命令+音频)
             ├─ audio_cb_:
             │   AudioDecoder::Decode()  [audio_pipeline.cc]
             │   └─ codec->OutputData()
             └─ command_cb_:
                 └─ (TODO 显示/舵机/震动)
```

### 7.2 服务器端

```
AICatServer.start()                   [main.py]
 ├─ __init__():
 │   ├─ STTEngine()                  [stt_engine.py]
 │   ├─ VoiceManager()               [voice_manager.py]
 │   └─ TTSEngine()                  [tts_engine.py]
 │
 ├─ CatBrain()                       [cat_brain.py]
 │   └─ EmotionStateMachine()        [cat_brain.py]
 │
 ├─ _console_reader() [异步]:
 │   └─ CatConnection.feed_text()
 │
 └─ serve(handle_connection):
     └─ handle_connection(websocket):
         └─ CatConnection(websocket, stt, tts, brain)
             └─ handle()              [websocket_handler.py]
                 ├─ _recv_loop() [并发协程]:
                 │   ├─ _handle_audio():
                 │   │   └─ OpusCodec::decode()
                 │   └─ _handle_json():
                 │       └─ (存储传感器数据)
                 │
                 └─ _process_loop() [并发协程]:
                     ├─ VAD::is_speech()  [audio_utils.py]
                     │
                     ├─ _finalize_utterance():
                     │   └─ _transcribe():
                     │       └─ STTEngine.transcribe()
                     │           └─ faster-whisper 推理
                     │
                     └─ 结束 → _respond(): (当 VAD 检测到完整语句后)
                         ├─ CatBrain.process()
                         │   ├─ EmotionFSM.update()
                         │   └─ llm_callable(prompt)
                         │       └─ Ollama API → Qwen3:14B
                         │
                         ├─ _send_command(命令JSON)
                         │
                         └─ TTS: synthesize_opus_frames()
                             ├─ [路由到Qwen3-TTS/Fish/Piper/GSV]
                             ├─ _pcm_to_opus()
                             └─ ws.send(opus_frame) × N
```

---

## 8. 线程/任务模型

### 8.1 固件端（FreeRTOS 多任务）

| 任务 | 栈大小 | 优先级 | 职责 |
|------|--------|--------|------|
| **main task** | 默认 | 1 | 主循环：传感器采集 + 音频发送 + 连接管理 |
| **EncoderTask** | 32KB | 5 | Opus 编码：从流缓冲区取 PCM，编码后发送 |
| **WS internal task** | 16KB | 8 | WebSocket 内部：消息收发 + 事件分发 |
| **IDLE** | — | 0 | 空闲任务 |

**数据同步：**
- 主循环 → EncoderTask：`xStreamBuffer`（PSRAM 存储区，77KB）
- WS 回调 → 音频播放：直接回调（在 WS 任务上下文中执行）

### 8.2 服务器端（asyncio 协程）

| 协程 | 创建点 | 职责 |
|------|--------|------|
| `handle_connection` | `serve()` | 连接生命周期管理 |
| `_recv_loop` | `handle()` | 持续接收 WebSocket 消息（JSON + 二进制） |
| `_process_loop` | `handle()` | 每 60ms 处理 VAD、触发 STT/LLM/TTS |
| `_console_reader` | `start()` | 接受命令行输入（调试 + 音色管理） |
| `_transcribe()` | `_finalize_utterance()` | 一次性调用 Whisper 转写 |
| `_respond()` | `_finalize_utterance()` | LLM → TTS 全链路 |

**关键：** 每只猫连接有 2 个长期协程（recv + process），用 `asyncio.gather` 并发运行。Whisper 同步调用（Faster-Whisper 内部有线程池）。

---

## 9. 自检清单

### 音频链路

| 检查项 | 正常表现 |
|--------|---------|
| 麦克风采集 | `box_audio_codec.cc` 无 I2S 错误 |
| Opus 编码 | 日志输出 "Opus send OK: X bytes/frame" |
| VAD 检测 | 说话时显示 "VAD ON: energy=..." |
| STT 转写 | 日志输出 "User said: ..." |
| LLM 回复 | 日志输出 "Ollama response: ..." 或 "Cat says: ..." |
| TTS 合成 | 日志输出 "TTS: X Opus frames, sending to ESP32..." |
| 扬声器播放 | ESP32 收到二进制帧，扬声器有声音 |

### 传感器链路

| 检查项 | 正常表现 |
|--------|---------|
| 触摸传感器 | 主循环 10Hz 发送 JSON 传感器数据包 |
| 情绪更新 | 触摸触发情绪切换，日志显示对应 emotion |

### 链路时序

| 阶段 | 预估耗时 | 备注 |
|------|---------|------|
| VAD 检测到说话结束 | 480ms-3s | 最短 300ms + 480ms 静音 |
| Whisper 转写 | 0.5-2s | large-v3 on RTX 3090 |
| LLM 推理 | 0.4-1.5s | Qwen3-14B 本地 |
| TTS 合成 | 1-3s | Qwen3-TTS |
| 网络传输 | 50-200ms | 局域网 |
| 端到端延迟 | 2.5-7s | 用户说话完成 → 听到猫回复 |

---

## 附录：关键文件索引

| 文件 | 行数 | 核心职责 |
|------|------|---------|
| `firmware/main/main.cc` | 253 | 固件入口、主循环、任务创建、回调注册 |
| `firmware/main/ai_cat_board.cc` | 109 | 板级初始化：I2C、音频、GPIO、显示 |
| `firmware/main/cat_websocket.cc` | 225 | WebSocket 客户端：收发 + 事件分发 |
| `firmware/main/audio_pipeline.cc` | 91 | Opus 编解码封装 |
| `firmware/main/display.cc` | 304 | ST7789 显示驱动 + 猫眼渲染（待调试） |
| `firmware/main/box_audio_codec.cc` | — | ES8311(出) + ES7210(入) 音频驱动 |
| `server/main.py` | 368 | 服务器入口、LLM/STT/TTS 初始化、控制台交互 |
| `server/websocket_handler.py` | 344 | 连接管理、VAD、STT/LLM/TTS 编排 |
| `server/cat_brain.py` | 199 | 猫人格引擎、情绪状态机、LLM Prompt |
| `server/stt_engine.py` | 86 | Faster-Whisper 封装 |
| `server/tts_engine.py` | 245 | 多引擎 TTS 路由 |
| `server/voice_manager.py` | 204 | 音色配置管理 |
| `server/audio_utils.py` | 103 | Opus 编解码、VAD、PCM 工具 |
