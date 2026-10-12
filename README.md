# ds_voicecontrol

**本地只做两件事**：判断有没有人在说话、听有没有人喊唤醒词。
**命令理解交给大模型**：唤醒后的那一段语音直接（音频）发给多模态模型，由它从你配置的**命令表**里挑一条。
**动作在本地执行**：按键、输入中文、打开程序、激活窗口、组合动作。

```
说：牛来 ……（蜂鸣）…… 打开记事本
    │                     │
    │ 本地 KWS 唤醒        │ 本地 VAD 判断说话起止，录下这一句
    ▼                     ▼
 sherpa-onnx 关键词检测 ──> 音频(16k wav) ──> 大模型(OpenAI 兼容 / qwen-audio)
                                                    │
                                    {"command":"open_notepad","transcript":"打开记事本"}
                                                    │
                                     命令表校验 id ──> 本地执行动作
```

这样设计的三个理由：

1. **准确率**：中文口语 + 远场，靠本地小模型识别整句本来就不靠谱；多模态大模型直接听音频，同义说法（“搞一下”“帮我拷一份”）也能对上。
2. **隐私与成本**：唤醒词之前的音频**完全不出本地**；只说了唤醒词的那次**不会联网**（只开一个等待窗口）；每次命令只上传 1~2 秒语音。
3. **安全**：模型只能返回你命令表里存在的 `id`，返回别的（比如 `rm_rf`）会被拒绝执行，什么都不会发生。

---

## 目录

- [快速开始](#快速开始)
- [命令行](#命令行)
- [配置文件](#配置文件)
- [命令表（安全边界）](#命令表安全边界)
- [唤醒词](#唤醒词)
- [大模型配置](#大模型配置)
- [麦克风与门限](#麦克风与门限)
- [常见问题](#常见问题)
- [安全说明](#安全说明)
- [项目结构与开发](#项目结构与开发)
- [已知限制与后续方向](#已知限制与后续方向)

## 快速开始

前置：Windows 10/11、[uv](https://docs.astral.sh/uv/)（Python 由 uv 管理）、一个阿里云百炼（DashScope）API Key。

```powershell
uv sync                                          # 1. 安装依赖（含 sherpa-onnx 唤醒词引擎）
$env:DASHSCOPE_API_KEY = "sk-你的key"              # 2. 设置大模型 Key（也可以写进配置）
uv run vc init                                   # 3. 生成 config.toml
uv run vc calibrate                              # 4. 安静 5 秒，测环境底噪
uv run vc setup                                  # 5. 下载中文唤醒词模型（约 31MB）
uv run vc wake                                   # 6. 只测唤醒词：喊“牛来”看有没有反应
uv run vc doctor                                 # 7. 自检 + 真实调用一次大模型
uv run vc run                                    # 8. 开始监听，Ctrl+C 退出
```

然后说：**“牛来”**（听到蜂鸣）**“打开记事本”** —— 记事本会被打开。

调试期建议加 `--dry-run`：识别和决策照常，只是不真的按键。

```powershell
uv run vc run --dry-run                # 演练模式
uv run vc say "把声音调小一点"          # 不碰麦克风，直接把一句话交给大模型（--dry-run 可演练）
uv run vc run --wav 录音.wav --dry-run  # 用音频文件跑完整链路
uv run vc run --no-wake                # 本次不需要唤醒词
```

## 命令行

| 命令 | 作用 |
| --- | --- |
| `vc init [--force]` | 生成带中文注释的默认配置 |
| `vc devices` | 列出可用麦克风 |
| `vc meter [--seconds 10]` | 实时电平表：麦克风有没有声音、门限合不合适 |
| `vc calibrate [--seconds 5] [--no-write]` | 测环境底噪并写入 `audio.noise_threshold` |
| `vc setup [--model 名称]` | 下载本地唤醒词模型 |
| `vc wake [--seconds 20] [--threshold 0.15] [--wav 文件]` | **只测唤醒词**，不联网 |
| `vc rules [--all]` | 查看允许执行的命令表 |
| `vc keytest [--cross-app]` | **按键自测**：判断“日志显示成功但程序没反应”到底是哪种原因 |
| `vc doctor [--text "复制一下"]` | 自检（配置 / Key / 模型 / 完整性级别）+ 真实调用一次大模型 |
| `vc say "文本" [--dry-run]` | 不走麦克风，直接把一句话交给大模型并执行 |
| `vc run [--dry-run] [--no-wake] [--once] [--duration 秒] [--wav 文件] [--device 设备]` | **开始监听（主功能）** |

通用：`--config/-c 路径`（也可用环境变量 `VC_CONFIG`）、`--no-color`。

## 配置文件

`vc init` 生成的 `config.toml` 每一项都有中文注释，关键项如下。

### `[audio]` 麦克风与“有没有说话”

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `device` | `""` | 留空=系统默认麦克风；可填编号或名称片段（`vc devices` 查看） |
| `sample_rate` / `frame_ms` | 16000 / 30 | 音频参数，一般不用改 |
| `pre_roll_ms` | 300 | 说话前多留一点，避免第一个字被切掉 |
| `min_speech_ms` | 250 | 短于这个时长不算说话（过滤咳嗽、敲键盘） |
| `silence_ms` | 700 | 停顿超过这个时长认为这句话说完 → 送去大模型 |
| `noise_threshold` | 0.010 | 静音门限，`vc calibrate` 自动写入 |
| `threshold_factor` | 2.5 | 实际门限 = `max(noise_threshold, 实时底噪 × 倍数)` |
| `adaptive_noise` | `true` | 是否跟随底噪自动调整（空调/风扇变化时有用） |

### `[wake]` 本地唤醒词

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | `false` = 不需要唤醒词，检测到说话就直接发给大模型 |
| `words` | `["牛来"]` | 中文唤醒词，程序自动转成拼音 token 生成关键词文件 |
| `model` | `sherpa-onnx-kws-...-wenetspeech-3.3M-2024-01-01` | 中文唤醒词模型 |
| `models_dir` | `models` | 模型目录（相对配置文件所在目录） |
| `keywords_file` | `""` | 留空 = 自动生成 `models/wake-keywords.txt` |
| `threshold` | `0.25` | 命中阈值：调大更严格（不易误唤醒），调小更容易唤醒 |
| `wait_ms` | `5000` | 唤醒后等你开口的最长时间（毫秒） |
| `command_max_ms` | `8000` | 唤醒后单条口令最长录制时长 |
| `auto_download` | `true` | 模型缺失时自动下载 |
| `num_threads` | `2` | 关键词检测线程数 |

### `[llm]` 大模型

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容接口地址 |
| `model` | `qwen-audio-turbo-latest` | 支持音频输入的模型 |
| `api_key_env` | `DASHSCOPE_API_KEY` | **推荐**：Key 放环境变量，配置里只写变量名 |
| `api_key` | `""` | 也可以直接写（优先级更高，但不建议提交到仓库） |
| `timeout_s` | `30` | 单次请求超时 |
| `response_format_json` | `true` | 要求返回 JSON；模型不支持时会自动去掉重试一次 |
| `extra_prompt` | `""` | 补充提示词，可以写你自己的说法习惯 |
| `save_audio_dir` | `""` | 调试用：把每次发给模型的音频存下来 |

### `[commands]` 执行策略

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `allow` | `["hotkey","press","text","app","focus","wait","sequence"]` | 动作类型白名单，不在里面的动作会被拒绝 |
| `allow_shell` | `false` | 是否允许 `shell` 动作（执行命令行），默认关闭 |
| `unknown` | `log` | 没匹配到命令时：`log` / `ignore` / `beep` |
| `cooldown_ms` | `400` | 同一条命令的最小间隔，防止一次说话重复触发 |
| `fallback_local` | `true` | 模型没给命令但返回了原话时，用本地规则再匹配一次（兜底） |

## 命令表（安全边界）

大模型**只能**从这里挑命令，挑不到就什么都不做。所以这张表就是你的安全边界：写进去什么，它才可能做什么。

```toml
[[rules]]
id = "open_notepad"                    # 模型返回的就是这个 id；留空则用 name
name = "打开记事本"                      # 只用于显示
description = "打开 Windows 记事本"      # 给模型看的说明，写清楚“什么时候用它”
phrases = ["打开记事本", "记事本"]         # 示例说法（也是本地兜底匹配的触发词）
type = "app"
target = "notepad"
```

带槽位的例子（把说的话带进动作里）：

```toml
[[rules]]
id = "search"
name = "搜索"
description = "在浏览器里搜索用户说的内容，搜索词放进 query 槽位"
phrases = ["搜索今天天气", "查一下", "百度一下"]
slots = ["query"]
type = "sequence"
steps = [
    { type = "hotkey", keys = ["ctrl", "t"] },
    { type = "text", text = "{query}" },
    { type = "press", keys = ["enter"] },
]
```

说“搜索今天天气”→ 模型返回 `{"command":"search","slots":{"query":"今天天气"}}` → 新标签页输入“今天天气”并回车。

### 动作类型

| `type` | 参数 | 说明 |
| --- | --- | --- |
| `hotkey` | `keys` | 按下组合键，例如 `["ctrl", "shift", "t"]` |
| `press` | `keys`、`repeat`、`interval_ms` | 依次点按键，可重复（音量加 `repeat = 3`） |
| `text` | `text` | 输入文字，支持 `{槽位}` `{text}` `{date}` `{time}` `{datetime}` |
| `app` | `target` | 打开程序/文件/网址：`"notepad"`、`"https://…"` |
| `focus` | `title` | 把标题包含该文字的窗口切到前台（按键总发错窗口时用） |
| `wait` | `ms` | 等待 |
| `sequence` | `steps = [ … ]` | 组合动作，某步失败会停止后续步骤 |
| `shell` | `command`、`cwd` | 执行命令行（需要 `allow_shell = true`） |

关于槽位：规则里声明了 `slots = ["query"]`，而模型这次没给出 `query` 时，`{query}` 会渲染成**空**，
动作会明确报“渲染后的文本为空”，而不是把 `{query}` 这串字面量打出去。

### 可用的按键名

| 类别 | 名称 |
| --- | --- |
| 修饰键 | `ctrl` `alt` `shift` `win`（`cmd`/`super` 同义） |
| 常用 | `enter` `esc` `tab` `space` `backspace` `delete` `insert` `home` `end` |
| 翻页/方向 | `pageup` `pagedown` `up` `down` `left` `right` |
| 锁定/系统 | `capslock` `numlock` `printscreen` `scrolllock` `pause` `menu` |
| 功能键 | `f1` ~ `f24` |
| 媒体 | `volumeup` `volumedown` `mute` `playpause` `nexttrack` `prevtrack` `stop` |
| 字符 | 单个字母/数字/符号，例如 `["ctrl", "shift", "t"]` |

实现说明：Windows 下**按键和中文输入默认都走自带的 `SendInput` 封装**（组合键用虚拟键码 + 扩展键标志，
文字用 `KEYEVENTF_UNICODE`），所以不依赖输入法、不受键盘布局影响，也不需要额外依赖。
非 Windows 平台用 pynput 兜底：`uv sync --extra pynput`。

### 按键发错了窗口？

Windows 的按键只会发给**当前前台窗口**。如果你的终端占着焦点，`Ctrl+C` 就发给了终端。
三种办法：

1. 用 `focus` 动作先把目标窗口切到前台：

```toml
[[rules]]
id = "save_in_notepad"
name = "在记事本里保存"
description = "先切到记事本再保存"
phrases = ["记事本保存"]
type = "sequence"
steps = [
    { type = "focus", title = "记事本" },
    { type = "hotkey", keys = ["ctrl", "s"] },
]
```

2. 说话时保证目标程序就在前台。
3. 目标程序以**管理员身份**运行时，Windows 会阻止普通权限程序给它发模拟按键
   （UIPI 限制）；这时只能让 `vc run` 也以管理员身份运行。

## 唤醒词

唤醒词用 **sherpa-onnx 的关键词检测（KWS）**，不是整句识别：它只判断“有没有人喊这几个词”，
所以 CPU 占用很低，也不会把唤醒词之外的语音送去识别。

程序会把中文唤醒词自动转成模型要的「声母 + 韵母」token：

```text
牛来       ->  n iú l ái @牛来
你好牛来    ->  n ǐ h ǎo n iú l ái @你好牛来   （可选：加进 words 更不容易漏唤醒）
```

改唤醒词只要改 `[wake] words`，下次运行会自动重建关键词文件。

**调优建议**

- 喊了没反应：`vc wake` 单独测；把 `threshold` 从 `0.25` 降到 `0.18`；换 2~4 个字的常见词。
- 老是自己醒：把 `threshold` 提到 `0.35~0.5`；别用“好的”“嗯”这类常用词。
- 只能纯中文唤醒词（当前中文模型）。要英文/中英混合唤醒，把 `model` 换成
  `sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` 并重新 `vc setup`（关键词 token 规则不同，
  非中文唤醒词需要自己准备 keywords 文件）。

## 大模型配置

默认走**阿里云百炼（DashScope）的 OpenAI 兼容接口** + `qwen-audio-turbo-latest`：

```toml
[llm]
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen-audio-turbo-latest"
api_key_env = "DASHSCOPE_API_KEY"
```

请求长这样（程序内部实现，无需你手写）：

```json
{
  "model": "qwen-audio-turbo-latest",
  "messages": [
    {"role": "system", "content": "你是一个语音命令解析器……命令表……只输出 JSON"},
    {"role": "user", "content": [
      {"type": "input_audio", "input_audio": {"data": "data:;base64,UklGR…", "format": "wav"}},
      {"type": "text", "text": "请解析这段语音，并按格式返回 JSON。"}
    ]}
  ],
  "temperature": 0,
  "response_format": {"type": "json_object"}
}
```

**换更聪明的模型**：`qwen3-omni-flash`、`qwen-omni-turbo-latest` 等支持音频输入的模型都可以；
若接口报“模型不支持音频输入”，用 `vc doctor` 能看到服务端原文，换一个模型名即可。

**换成别的服务**：任何兼容 OpenAI `chat/completions` 且支持 `input_audio` 的服务都能用
（自建 vLLM / Ollama 跑 Qwen2-Audio 之类也一样），只改 `base_url`、`model`、`api_key_env`。

**成本与延迟**：一次命令 = 一次请求、1~2 秒音频、几百 token；`vc doctor` 会打印实际耗时。
只说了唤醒词的那次不会发请求（程序在本地就把这段丢弃了）。

## 麦克风与门限

```powershell
uv run vc meter --seconds 15      # 说话时盯着电平看
uv run vc calibrate --seconds 5   # 安静时测底噪
```

- 说话电平通常 0.02~0.15，门限在 0.004~0.02 之间；说话时应明显高于门限。
- 一直没反应 → 门限太高或麦克风没收到声音：调小 `noise_threshold`，或用 `vc devices` 换 `audio.device`。
- 老是被环境噪音触发 → 调大 `noise_threshold`/`threshold_factor`，或把 `min_speech_ms` 提到 400。
- 校准期间保持“日常环境”（空调风扇正常开），但别说话。

## 常见问题

**Q：喊了唤醒词没反应？**
A：`uv run vc wake` 单独测。依次检查：`vc meter` 有没有电平 → 说的是不是配置里的唤醒词 →
把 `wake.threshold` 降到 0.18 → 换个更好发音的唤醒词。

**Q：唤醒后说了话，但什么都没执行？**
A：`vc doctor` 会一次性检查配置、Key、模型、关键词，并真实调用一次大模型。再按下面顺序看：
① 有没有说完整（唤醒后 `wait_ms` 内要开口）；② 命令表里有没有对应说法，必要时补充
`description` 和 `phrases` 示例；③ 动作类型是否在 `commands.allow` 里；④ 是不是发给了错误的前台窗口（见 `focus`）。

**Q：日志里显示“模型没有匹配到任何命令”？**
A：模型听到了（会打印 `transcript`），但命令表里没有合适的命令。往 `[[rules]]` 里加一条，
或者把这条命令的 `description`/`phrases` 写得更贴近你的说法。

**Q：接口报 400 / 401 / 404？**
A：400 多为模型不支持音频输入（换 `llm.model`）；401 是 Key 无效或没读到
（检查 `api_key_env` 指向的环境变量）；404 是 `base_url`/`model` 写错。`vc doctor` 会打印服务端原文。

**Q：能不能完全离线？**
A：唤醒词和“有没有说话”本来就是离线的；命令理解需要联网（或你自己在内网起一个兼容
OpenAI 接口的多模态模型，把 `base_url` 指过去）。如果大模型不可用，程序会明确报错，不会乱猜。

**Q：中文输入没反应？**
A：文字输入走 Windows `SendInput` 的 Unicode 模式，需要有一个能接收键盘输入的前台窗口。
如果目标程序以管理员权限运行，普通权限的程序无法给它发按键（UIPI），此时要么以管理员身份
运行 `vc run`，要么就别在那个程序里用语音控制。

**Q：组合键“看起来执行了”但目标程序没反应？**
A：同上，先确认前台窗口对不对（用 `--dry-run` 看命中的动作，再看 `focus` 是否成功）。
`focus` 受 Windows 前台窗口锁限制，个别情况下会提示“没能切到前台”，先手动点一下目标窗口即可。

**Q：日志显示 `[OK] hotkey: ctrl+c`，但目标程序一点反应都没有？**
A：先跑一次 `uv run vc keytest --cross-app`（会倒计时给你时间切到记事本）。模拟按键
Windows 只发给**当前前台窗口**，而且有三种情况会被“成功但无效”地拦下来：

| `vc keytest` 告诉你 | 原因 | 怎么办 |
| --- | --- | --- |
| `进程完整性级别：Low` | 命令跑在**受限沙箱**里，Windows 的 UIPI 会静默丢掉模拟按键（`SendInput` 返回成功） | 换成普通终端（直接开 Windows Terminal / PowerShell）运行 `vc run`，不要在沙箱终端里跑 |
| 前台窗口是别的程序（终端、浏览器…） | 按键发给了前台窗口，不是你想操作的程序 | 说话时让目标程序保持在前台；或在规则第一步加 `{ type = "focus", title = "记事本" }` |
| 前台窗口所属进程完整性是 `High` | 目标程序以**管理员身份**运行，普通权限发不进按键 | 让 `vc run` 也以管理员身份运行 |

`vc run` 启动时的横幅里也会直接提示“运行在受限沙箱里”，`vc doctor` 同样会报这一项。

**Q：`uv sync` / `uv run` 报 `PermissionError: ...\uv\cache\builds-v0\.tmpXXXX\get_requires_for_build_editable.txt`？**
A：uv 想写它的全局缓存 `%LOCALAPPDATA%\uv\cache`，但当前命令跑在**受限环境**里
（进程是低完整性级别，只能写工作区），所以被拒绝。**普通终端里不会出现这个问题**——
换个窗口（Win+R → `wt`）重跑即可。确实要在受限环境里装依赖时，临时把缓存指到工作区：

```powershell
$env:UV_CACHE_DIR = "$PWD\.uv-cache"                 # 缓存放进工作区
$env:UV_PYTHON_INSTALL_DIR = "$PWD\.uv-python"       # 需要下载 Python 时也要指到工作区
uv sync
```

想对这个项目永久生效，可以在项目根目录放一个 `uv.toml`（会让本项目的 uv 缓存落在项目目录里，
而不是 `%LOCALAPPDATA%`）：

```toml
cache-dir = ".uv-cache"
```

只想跳过重新构建、直接跑（虚拟环境已经装好时）：

```powershell
uv run --no-sync vc run
```

**Q：`uv sync` 报 sherpa-onnx 相关错误？**
A：本项目显式声明了 `sherpa-onnx-core`（原生库，含 onnxruntime），版本必须和 `sherpa-onnx` 一致；
升级其中一个时请同步另一个的版本号。

## 安全说明

- 模型只能返回命令表里的 `id`，其他一律拒绝（日志会写明“已拒绝执行”）。
- `shell` 动作默认被 `commands.allow_shell = false` 拦住；动作类型还受 `commands.allow` 白名单限制。
- 调试用 `--dry-run`：只显示要做什么，不动系统。
- 建议用环境变量存 API Key，不要把 key 写进 `config.toml`（该文件已在 `.gitignore` 里）。
- 不建议让 `vc run` 常驻管理员权限；确实需要时，请只保留你完全信任的命令。

## 项目结构与开发

```
ds_voicecontrol/
├─ pyproject.toml              # 依赖、入口点 vc、pytest 配置
├─ config.toml                 # 你的配置（vc init 生成，已 gitignore）
├─ models/                     # 唤醒词模型 + 自动生成的 wake-keywords.txt
├─ src/voice_control/
│  ├─ cli.py                   # 命令行子命令
│  ├─ config.py                # TOML 读写 + 校验
│  ├─ config_default.py        # 带注释的默认配置模板
│  ├─ audio.py                 # 麦克风、底噪校准、说话分段、音量门限
│  ├─ kws.py                   # 本地唤醒词（拼音转 token、关键词文件、关键词检测）
│  ├─ models.py                # 唤醒词模型下载/查找
│  ├─ llm.py                   # 大模型：命令表提示词、audio 请求、JSON 决策解析与校验
│  ├─ actions.py               # 动作执行（pynput 按键 + SendInput 中文输入 + focus 窗口）
│  ├─ matching.py              # 文本归一化与本地兜底匹配
│  ├─ listener.py              # 状态机：listening → waiting → capturing → 决策 → 执行
│  └─ ui.py                    # 终端输出
└─ tests/                      # 单元测试（含一个本地假大模型接口）
```

```powershell
uv run pytest                                     # 跑测试
uv run pytest tests/test_llm.py -q                # 只跑大模型接入层
uv run vc say "把音量调小一点" --dry-run            # 不改配置直接验证命令表
uv run vc run --wav 录音.wav --dry-run             # 用真实音频验证整条链路
```

调试技巧：`[general] log_level = "debug"`，`[llm] save_audio_dir = "debug-audio"`，
可以把每次发给模型的语音存下来，用播放器听一遍就知道模型听到了什么。

## 已知限制与后续方向

- 命令理解依赖网络（或自建兼容接口）；离线时只有唤醒和“有没有说话”可用。
- 唤醒词是“喊一声就开窗”，不支持连续对话；每次命令都要先喊唤醒词。
- 一次只处理一句话，没有上下文记忆（比如“再大一点”这种指代需要自己写进 `description`）。
- `focus` 受 Windows 前台窗口限制，个别情况下需要先手动点一下目标窗口。
- 后续可做：连续对话模式、把常用命令做成托盘菜单、本地小模型做“免联网”的降级、鼠标动作。
