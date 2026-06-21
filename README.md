# vrec — Mac 低调录音 + 语音转译（菜单栏 App + 命令行）

在 Mac（含无界面的 Mac mini）上**安静地**录音并一键转文字。常驻系统**菜单栏**（类似 ClashX，
没有 Dock 图标、没有主窗口），也保留命令行用法。转译支持两种引擎，可随时切换：

- ☁️ **云端**：阿里云 DashScope `qwen3-asr-flash`（OpenAI 兼容，base_url / key / model 可配）
- 🔒 **本地离线**：`faster-whisper`，**首次自动下载模型**，之后完全离线、音频不出本机（合规友好）

---

## 一键安装与启动（git clone 即用）

```bash
git clone <你的仓库地址> vrec
cd vrec
./start.sh
```

或在 Finder 里**双击 `start.command`**。

`start.sh` 会自动：① 装 `ffmpeg`（用 Homebrew）② 建 Python 虚拟环境 `.venv`
③ 装依赖 ④ 生成配置 ⑤ 在菜单栏启动 App（右上角出现 🎙）。
首次运行需要联网装依赖，之后秒开。想顺带装好本地离线引擎：`./start.sh --with-local`。

> 关于「把依赖都打包进去」：Python 的二进制依赖（ffmpeg、PyObjC、模型）是按机器/架构编译的，
> 直接塞进 git 既臃肿又不可移植。这里改用**锁定版本的 requirements + 一键引导脚本**，效果一样是
> 「clone 下来一条命令启动」，而且干净可复现。

---

## 菜单栏使用（无需命令行）

点右上角 🎙 图标：

```
🎙 / 🔴 12m03s        ← 待机 / 录音中（显示已录时长）
  ● 开始录音 / ■ 停止录音
  定时录音 ▸           15 / 30 分钟、1 / 2 小时、自定义…
  预约录音…            输入开始时间 + 时长，到点自动录
  取消预约
  ──────
  转译最近一次录音
  转译音频文件…        弹出文件选择框
  ✓ 录完自动转译
  ──────
  识别引擎 ▸           • 云端 · Aliyun   /   本地 · Whisper（离线）
  本地模型大小 ▸        tiny / base / small / medium / large-v3
  麦克风 ▸             选择输入设备（可刷新）
  ──────
  设置 ▸              API Key… / Base URL… / 云端模型名… / 打开配置 / 查看日志
  打开录音文件夹
  ──────
  退出 vrec
```

- 录音存为**单声道 16kHz MP3**，默认放 `~/.vrec/recordings/`。
- 转译结果会写到与音频同名的 `.txt`，并弹通知显示开头。

---

## 两种识别引擎（合规）

在菜单「识别引擎」里一键切换，选择会记到配置里：

| | 云端 · Aliyun | 本地 · Whisper（离线） |
|---|---|---|
| 准确度 | 高 | 取决于模型大小 |
| 速度 | 快（需联网） | 取决于机器；`small` 在 Mac 上够用 |
| 隐私/合规 | 音频上传到阿里云 | **音频不出本机**，完全离线 |
| 首次准备 | 填 API Key 即可 | 自动 `pip install faster-whisper` + 下载模型（约几百 MB） |
| 模型缓存 | — | `~/.vrec/models/` |

切到「本地」后，App 会在后台自动装依赖并下载所选模型；完成会有通知。模型大小：
`small` 是中文准确度/速度的平衡点，机器好可选 `medium` / `large-v3`，追求快用 `base` / `tiny`。

---

## 命令行用法（可选，SSH/无界面时用）

菜单栏 App 需要图形登录会话；若只通过 SSH 连 Mac mini，用命令行（功能等价）：

```bash
# 装一个全局命令（指向仓库里的脚本）
ln -sf "$PWD/vrec.py" /opt/homebrew/bin/vrec

vrec devices                          # 列出麦克风，选设备
vrec config --set audio_device=1
vrec rec -d 30m -t                    # 录 30 分钟并自动转译
vrec rec --at "14:00" -d 45m          # 14:00 起录 45 分钟（已过则顺延次日）
vrec rec --start "2026-06-22 09:00" --end "2026-06-22 10:30"
vrec rec                              # 一直录到 vrec stop
vrec stop ; vrec status
vrec transcribe 文件.mp3               # 转译（不带参数=转最近一次录音）
vrec transcribe 文件.mp3 --engine local   # 用本地离线引擎
```

长录音超过云端单次上限（10MB / 5 分钟）会**自动切段**识别再拼接。

---

## 配置

配置文件 `~/.vrec/config.json`（权限 600，**不会进 git**）。可在 App「设置」里改，或：

```bash
vrec config --show
vrec config --set engine=local
vrec config --set base_url=https://your-host/v1 --set model=your-asr-model
```

| 键 | 默认 | 说明 |
|---|---|---|
| `engine` | `cloud` | `cloud`=云端 / `local`=本地离线 |
| `language` | `null` | 语言提示，如 `zh`/`en`；`null`=自动 |
| `api_key` | （空） | 云端 API Key（在 App 设置里填） |
| `base_url` | DashScope 兼容端点 | 国际版用 `dashscope-intl` |
| `model` | `qwen3-asr-flash` | 云端模型名 |
| `enable_itn` | `true` | 数字/标点规整 |
| `local_model` | `small` | 本地模型大小 |
| `audio_device` | `0` | 麦克风序号 |
| `sample_rate`/`channels`/`bitrate` | 16000/1/48k | 录音参数 |
| `recordings_dir` | `~/.vrec/recordings` | 录音目录 |
| `chunk_seconds` | `240` | 云端长音频切段秒数 |

---

## ⚠️ 必读

1. **Mac mini 没有内置麦克风** —— 需接 USB/外置麦克风；先在菜单「麦克风」里选设备。
2. **要授权麦克风**：首次录音 macOS 会要求授权。把**运行它的程序**（终端，或登录项里的
   Python）加到「系统设置 → 隐私与安全性 → 麦克风」。否则录出来是空文件，App 会提示。
3. **录音时系统仍会亮橙色「麦克风使用中」指示灯** —— 这是 macOS 隐私机制，无法关闭，本工具
   也不绕过它。「低调」指无窗口、常驻菜单栏后台运行，不是对系统隐身。
4. 未经同意录制他人在许多地区**违法**，请仅在你有权录音的场合使用。

---

## 录制扬声器 / 系统声音（可选）

macOS 出于隐私限制**不能直接录制扬声器输出**。要录「显示器/扬声器里放出来的声音」，需要一个虚拟回环设备，推荐免费的 **BlackHole**：

```bash
brew install blackhole-2ch
```

装好后：
1. BlackHole 会作为一个**输入设备**出现在菜单「录音设备」里（标 🔊）。选它就能录系统声音。命令行同理：`vrec config --set audio_device="BlackHole 2ch"`。
2. 但默认声音会被「截走」、自己听不到。想**边听边录**：打开「音频 MIDI 设置」→ 左下角 `+` →「创建多输出设备」→ 勾选 你的显示器(PHL…) + BlackHole 2ch → 右键「将此设备用于声音输出」。
3. 想**同时录 麦克风 + 系统声音**（如会议）：在「音频 MIDI 设置」建一个「聚合设备」含 麦克风 + BlackHole，录这个聚合设备即可。

菜单栏「录音设备 → 🔊 录制系统声音…」里也有同样的引导，并会自动检测 BlackHole 是否已装。

设备选择本身已支持：菜单「录音设备」或 `vrec devices` 会列出所有输入设备，🎙=麦克风、🔊=系统声音，点选即用。

---

## 开机自启动（可选）

让 App 登录后自动出现在菜单栏：**系统设置 → 通用 → 登录项 → 添加** `start.command`。
（无界面/纯 SSH 的机器没有菜单栏，请改用命令行 + launchd，见下。）

命令行的定时录音也可交给 launchd 持久化，例如每天 9:00 录 1 小时并转译，新建
`~/Library/LaunchAgents/com.user.vrec.plist`，`ProgramArguments` 指向
`/opt/homebrew/bin/vrec rec -d 1h -t -q`，设置 `StartCalendarInterval` 为 9:00，
然后 `launchctl load`。

---

## 项目结构 / 提交到 GitHub

```
vrec/
  vrec.py                # 核心：录音 + 转译（云端/本地）+ 命令行
  menubar.py             # 菜单栏 App（rumps）
  requirements.txt       # 核心依赖
  requirements-local.txt # 本地离线引擎依赖（可选）
  start.sh / start.command  # 一键引导启动
  config.example.json    # 配置模板（不含 key）
  .gitignore             # 已忽略 .venv / 录音 / 模型 / config.json(含 key)
  README.md
```

`.gitignore` 已确保 **API key、录音、模型、虚拟环境都不会被提交**。
你的 key 只存在本机 `~/.vrec/config.json`。直接 `git init && git add . && git commit` 即可安全推送。
