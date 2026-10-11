# ds_voicecontrol

本地跑的中文语音控制小工具：说一句 **“小助手，复制”**，它就替你按 `Ctrl+C`；
说 **“小助手，搜索今天天气”**，它打开新标签页、输入“今天天气”并回车。

全程离线（默认用 Vosk 中文小模型，42MB），音频不上传，规则、唤醒词、门限全部写在
一个 `config.toml` 里。

---

## 目录

- [特性](#特性)
- [工作原理](#工作原理)
- [快速开始](#快速开始)
- [命令行子命令](#命令行子命令)
- [配置文件详解](#配置文件详解)
- [口令规则怎么写](#口令规则怎么写)
- [唤醒词与门限调优](#唤醒词与门限调优)
- [换识别引擎（更准的模型）](#换识别引擎更准的模型)
- [常见问题](#常见问题)
- [安全说明](#安全说明)
- [项目结构与开发](#项目结构与开发)
- [已知限制与后续方向](#已知限制与后续方向)

---

## 特性

- **离线**：默认 Vosk 中文小模型（约 42MB），不需要联网、不上传音频。
- **自动测底噪**：`vc calibrate` 监听几秒环境噪音，自动算出一个静音门限写回配置；
  运行时还会跟随底噪自动抬高门限（空调、风扇变化也能适应）。
- **唤醒词可配**：`小助手` / `你好小助手` … 支持模糊匹配，识别听错一两个字也能唤醒；
  也可以关掉唤醒词，听到口令直接执行。
- **口令可配**：触发词 + 动作，全部写在 TOML 里，支持“槽位”：
  `搜索 今天天气` 会把“今天天气”填进动作模板。
- **动作够用**：组合键（`ctrl+c`）、按键（`enter`、`volumeup`…）、中文文字输入、
  打开程序/文件/网址、等待、动作组合序列、执行命令（默认禁用）。
- **安全的调试手段**：`--dry-run` 演练模式、`vc say` 直接测一句话、`vc run --wav` 用音频文件测，
  都不需要对着麦克风反复喊。
- **开箱 22 条规则**：复制、粘贴、剪切、撤销、保存、全选、输入、确认、关闭窗口、切换窗口、
  显示桌面、打开浏览器/记事本、搜索、新标签页、关闭标签页、翻页、音量加减、静音、播放暂停。

## 工作原理

```
麦克风 ──30ms 一帧──▶ 音量门限分段（VAD-lite）──▶ 语音识别（Vosk / faster-whisper）
                                                        │
                                   唤醒词匹配 ◀─────────┤
                                                        ▼
                                     口令匹配（归一化 + 模糊 + 槽位）
                                                        │
                                                        ▼
                                     动作执行（pynput 按键 / SendInput 中文输入）
```

几个设计取舍：

- **不用专用唤醒引擎**（如 Porcupine / openWakeWord），而是“识别文本里找唤醒词”。
  好处是零额外依赖、唤醒词随便改、中文友好；代价是比专用引擎费一点 CPU，
  嘈杂环境里可能误唤醒（后文有调优办法）。
- **分段只用音量**，不引入 Vosk 的流式部分结果，逻辑简单、可单元测试，也方便换引擎。
- **中文文字输入用 `SendInput` 的 Unicode 模式**，不走输入法、不受键盘布局影响。

## 快速开始

前置：Windows 10/11、[uv](https://docs.astral.sh/uv/)（Python 由 uv 自己管理）。

```powershell
uv sync                   # 1. 安装依赖（会自动准备好 Python 3.12）
uv run vc init            # 2. 生成带中文注释的 config.toml
uv run vc devices         # 3. （可选）看看麦克风编号和名称
uv run vc calibrate       # 4. 安静 5 秒，测环境底噪并写入静音门限
uv run vc setup           # 5. 下载中文识别模型（约 42MB，首次需要联网）
uv run vc run             # 6. 开始监听，Ctrl+C 退出
```

然后对着麦克风说：**“小助手，复制”** —— 当前窗口的选中内容会被复制。

建议第一次先演练，确认规则符合预期再真跑：

```powershell
uv run vc run --dry-run    # 识别、匹配照常，但不真的按键 / 不打开程序
uv run vc say "小助手 复制" --dry-run   # 直接测一句话，连麦克风都不用
```

不想每次都说唤醒词（比如戴着耳机一个人用）时可以：

```powershell
uv run vc run --no-wake               # 本次运行不需要唤醒词
# 或者改配置： [wake] require_wake = false
```

## 命令行子命令

| 命令 | 作用 |
| --- | --- |
| `vc init [--force]` | 生成默认配置（`--force` 覆盖已有文件） |
| `vc devices` | 列出可用的输入设备（编号 / 名称 / 默认设备） |
| `vc meter [--seconds 10]` | 实时电平表：看麦克风有没有声音、门限合不合适 |
| `vc calibrate [--seconds 5] [--no-write]` | 测环境底噪，给出并写入静音门限 |
| `vc setup [--model 名称] [--engine vosk\|whisper]` | 下载 / 检查识别模型 |
| `vc rules [--all]` | 打印当前所有口令规则 |
| `vc say "文本" [--dry-run] [--no-wake]` | 不走麦克风，直接把文本喂给口令匹配（调规则神器） |
| `vc run [选项]` | **开始监听（主功能）** |
| `vc version` | 版本号 |

`vc run` 的选项：

| 选项 | 说明 |
| --- | --- |
| `--dry-run` | 演练：不真正执行动作 |
| `--no-wake` | 本次运行不需要唤醒词 |
| `--once` | 只处理一段语音就退出 |
| `--duration 秒` | 监听指定秒数后自动退出（方便脚本化验证） |
| `--wav 文件.wav` | 不接麦克风，直接处理一个 wav 文件 |
| `--device 名称或编号` | 临时指定输入设备 |
| `--engine vosk\|whisper` | 临时切换识别引擎 |

通用选项：`--config/-c 路径` 指定配置文件（也可以用环境变量 `VC_CONFIG`），`--no-color` 关掉彩色输出。

实测例子（用合成的中文语音走完整链路）：

```text
> uv run vc run --wav .tmp-test\wake_search.wav --dry-run
处理音频文件：wake_search.wav（3.0 秒）
听到: 小助手搜索今天天气
已唤醒（唤醒词：小助手）
命中「搜索」得分=0.98 槽位='今天天气'
    [OK] hotkey: ctrl+t（演练，未真正按下）
    [OK] text: 输入 '今天天气'（演练，未真正输入）
    [OK] press: enter x1（演练，未真正按下）
    [OK] sequence: 共 3 步
```

## 配置文件详解

`vc init` 生成的 `config.toml` 里每一项都有中文注释，这里再解释几个关键点。

### `[general]`

| 项 | 说明 |
| --- | --- |
| `language` | `zh` / `en`，传给识别引擎 |
| `log_level` | `debug` 时会打印每段音频的时长、峰值电平等信息 |

### `[audio]`

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `device` | `""` | 留空=系统默认麦克风；也可填编号或名称片段（如 `"Headset"`） |
| `sample_rate` | `16000` | 识别模型要求的采样率，一般不要改 |
| `frame_ms` | `30` | 每帧时长，越小越灵敏、CPU 略高 |
| `pre_roll_ms` | `300` | 说话前多留一点音频，避免第一个字被切掉 |
| `min_speech_ms` | `250` | 短于这个时长的不算说话（过滤咳嗽、敲键盘） |
| `max_utterance_ms` | `8000` | 单句最长录音时长，超长强制截断 |
| `silence_ms` | `700` | 停顿超过这个时长认为一句话说完 |
| `noise_threshold` | `0.010` | **静音门限**，`vc calibrate` 自动写入（RMS，0~1） |
| `threshold_factor` | `2.5` | 动态倍数：实际门限 = `max(noise_threshold, 实时底噪 × 倍数)` |
| `adaptive_noise` | `true` | 是否跟随环境底噪自动调整门限 |

门限怎么理解：

- `noise_threshold` 是“底线”，低于它一律当静音；
- 运行时每秒都会估计一次环境底噪（只在判定为静音时更新），把它乘以 `threshold_factor`
  得到动态门限，取两者较大值。
- 所以：安静房间里门限≈`noise_threshold`；开着空调变吵了，门限会自己抬高，不会一直误触发。

### `[asr]`

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `engine` | `vosk` | `vosk`（离线轻量）或 `whisper`（faster-whisper，更准更慢） |
| `models_dir` | `models` | 模型目录（相对配置文件所在目录） |
| `vosk_model` | `vosk-model-small-cn-0.22` | 中文小模型；想更准可换 `vosk-model-cn-0.22` |
| `whisper_model` | `small` | `tiny`/`base`/`small`/`medium`/`large-v3` |
| `whisper_device` | `cpu` | 有 NVIDIA 显卡且装了 CUDA 可设 `cuda` |
| `whisper_compute_type` | `int8` | CPU 推荐 `int8`，显卡可 `float16` |
| `hf_endpoint` | `""` | 国内下载慢时可填 `https://hf-mirror.com` |
| `auto_download` | `true` | 模型缺失时是否自动下载 |

### `[wake]`

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用唤醒词 |
| `require_wake` | `true` | `false` = 不需要唤醒词，听到口令就执行 |
| `words` | `["小助手", "你好小助手"]` | 唤醒词列表，识别文本里出现任意一个即唤醒 |
| `fuzzy` | `0.82` | 整句模糊匹配阈值，越低越容易唤醒（也越容易误唤醒） |
| `window_s` | `5.0` | 只说了唤醒词时，接下来多少秒内的下一句话按口令处理 |

### `[commands]`

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `allow` | `["hotkey","press","text","app","wait","sequence"]` | 动作类型白名单，不在里面的动作会被拒绝 |
| `allow_shell` | `false` | 是否允许 `shell` 动作（跑命令行），默认关闭 |
| `unknown` | `log` | 没匹配到口令时：`log` 打印 / `ignore` 静默 / `beep` 响一声 |
| `match_threshold` | `0.75` | 口令模糊匹配阈值 |
| `cooldown_ms` | `400` | 同一条规则的最小执行间隔，防止一句话重复触发 |

### `[feedback]`

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `beep` | `true` | 唤醒成功 / 命中口令时用系统提示音反馈 |

## 口令规则怎么写

一条规则 = **触发词** + **动作**：

```toml
[[rules]]
name = "复制"                    # 规则名，只用于显示
phrases = ["复制", "拷贝"]        # 触发词：等于 / 以它开头 / 包含它 都算命中
type = "hotkey"                  # 动作类型
keys = ["ctrl", "c"]             # 动作参数
```

写动作有两种等价写法，随便挑：

```toml
# 扁平写法
[[rules]]
name = "保存"
phrases = ["保存"]
type = "hotkey"
keys = ["ctrl", "s"]

# 表写法
[[rules]]
name = "保存"
phrases = ["保存"]
action = { type = "hotkey", keys = ["ctrl", "s"] }
```

### 动作类型

| `type` | 参数 | 说明 |
| --- | --- | --- |
| `hotkey` | `keys` | 按下组合键，例如 `["ctrl", "shift", "t"]` |
| `press` | `keys`、`repeat`、`interval_ms` | 依次点按键，可重复，例如音量加 `repeat = 3` |
| `text` | `text` | 直接输入文字，支持 `{text}` `{date}` `{time}` `{datetime}` |
| `app` | `target` | 打开程序 / 文件 / 网址，例如 `"notepad"`、`"https://…"` |
| `wait` | `ms` | 等待若干毫秒 |
| `sequence` | `steps = [ … ]` | 组合动作，按顺序执行，某一步失败会停止后续步骤 |
| `shell` | `command`、`cwd` | 执行命令行（需要 `allow_shell = true`） |

### 槽位（把说的话带进动作里）

触发词后面的内容会作为 `{text}` 传下去：

```toml
[[rules]]
name = "搜索"                       # 说“搜索 今天天气”
phrases = ["搜索"]
type = "sequence"
steps = [
    { type = "hotkey", keys = ["ctrl", "t"] },
    { type = "text", text = "{text}" },      # → 输入“今天天气”
    { type = "press", keys = ["enter"] },
]
```

```toml
[[rules]]
name = "输入"
phrases = ["输入", "打字"]           # 说“输入你好世界” → 键盘输入“你好世界”
type = "text"
text = "{text}"
```

### 匹配规则（为什么“帮我复制一下”也能命中“复制”）

1. 归一化：全角转半角、去掉空格与中英文标点、转小写；
2. 去掉口语填充词：句首的“嗯/那个/就是/麻烦/请/帮我/给我/把…”，句尾的“一下/吧/呀/吗/呢”；
3. 依次比较每条规则的每个触发词：

| 情况 | 得分 |
| --- | --- |
| 完全相同 | 1.00 |
| 以触发词开头（后面还有内容 → 就是槽位） | 0.98 |
| 触发词出现在句中 | 0.80~0.95（越靠前越高） |
| 模糊相似（≥ `match_threshold`，触发词至少 3 个字） | 相似度 × 0.8 |

取得分最高的一条执行；同一规则 400ms 内不会重复执行。
匹配不上、且你没在唤醒窗口里说话时，就按 `commands.unknown` 处理。

### 可用的按键名

| 类别 | 名称 |
| --- | --- |
| 修饰键 | `ctrl` `alt` `shift` `win`（`cmd`/`super` 同义） |
| 常用 | `enter` `esc` `tab` `space` `backspace` `delete` `insert` `home` `end` |
| 翻页/方向 | `pageup` `pagedown` `up` `down` `left` `right` |
| 锁定/系统 | `capslock` `numlock` `printscreen` `scrolllock` `pause` `menu` |
| 功能键 | `f1` ~ `f20` |
| 媒体 | `volumeup` `volumedown` `mute` `playpause` `nexttrack` `prevtrack` `stop` |
| 字符 | 单个字母/数字/符号，例如 `["ctrl", "shift", "t"]`、`["/"]` |

## 唤醒词与门限调优

**唤醒词**

- 想换唤醒词：直接改 `[wake] words`，例如 `["小助手", "电脑", "贾维斯"]`。
  两三个字的常见词识别更稳；避免用“复制”“保存”这类会出现在口令里的词。
- 经常喊了没反应：把 `fuzzy` 调到 `0.75`，或者换个更好发音的唤醒词。
- 经常被电视/别人说话误唤醒：把 `fuzzy` 提到 `0.85~0.9`，或者用更长的唤醒词（如“你好小助手”）。
- 只想说一句话就执行：`require_wake = false`，或者临时加 `--no-wake`。

**门限**

```powershell
uv run vc meter --seconds 15      # 说话时盯着电平看
uv run vc calibrate --seconds 5   # 安静时测底噪
```

- 说话时电平应该明显高于门限（正常说话通常是 0.02~0.15，门限在 0.004~0.02 之间）；
- 一直没反应 → 门限太高或麦克风没收到声音：调小 `audio.noise_threshold`，或换 `audio.device`；
- 老是误触发 → 调大 `noise_threshold` 或 `threshold_factor`，也可以把 `min_speech_ms` 提高到 400；
- 校准期间最好保持“日常环境”（空调、风扇正常开着），但别说话、别敲键盘。

## 换识别引擎（更准的模型）

**Vosk 换大模型**（无需额外依赖，只是模型更大）：

```powershell
uv run vc setup --model vosk-model-cn-0.22     # 约 1.3GB，更准
# 然后改 config.toml： asr.vosk_model = "vosk-model-cn-0.22"
```

**faster-whisper**（明显更准，代价是占用更大、CPU 上有延迟）：

```powershell
uv sync --extra whisper          # 安装可选依赖（ctranslate2 / onnxruntime 等）
# config.toml：
#   [asr] engine = "whisper"
#         whisper_model = "small"          # 或 base / medium
#         whisper_compute_type = "int8"    # CPU 用 int8；NVIDIA 显卡可 device="cuda" + float16
#         hf_endpoint = "https://hf-mirror.com"   # 国内下载慢时
uv run vc setup --engine whisper  # 提示模型会放在哪里
```

英文场景：`asr.vosk_model = "vosk-model-small-en-us-0.15"`、`general.language = "en"`，
唤醒词换成英文（如 `["computer"]`）即可。

## 常见问题

**Q：识别到文本了，但什么都没执行？**
A：按顺序检查——① 是否说了唤醒词（或加 `--no-wake`）；② `vc rules` 里有没有对得上的触发词；
③ 该动作类型是否在 `commands.allow` 白名单里；④ 是不是 400ms 冷却期内重复触发。
用 `vc say "小助手 复制"` 能立刻看出是哪一步没对上。

**Q：一直没反应？**
A：`vc meter` 看说话时电平有没有超过门限；`vc devices` 确认设备对不对；
`uv run vc run --config 你的config.toml` 时留意启动横幅里的“静音门限”和“输入设备”。

**Q：`uv sync` 报 vosk 依赖 `srt` 构建失败？**
A：`srt` 在 PyPI 上只有源码包，`uv`/`pip` 会本地构建它（纯 Python，很快）。
如果因为所在环境的限制构建失败，可以手动 `uv pip install srt` 或 `python -m pip install srt` 后再
`uv sync --no-build-isolation`。

**Q：中文输入没反应 / 只有英文字母？**
A：文字输入走 `SendInput` 的 Unicode 模式，需要有一个可接收键盘输入的前台窗口，
且不能是被管理员权限保护、或独占键盘的程序。个别游戏（DirectInput）不接收这种输入。

**Q：快捷键在某个程序里无效？**
A：如果那个程序以管理员身份运行，Windows 会阻止普通权限进程向它发送按键。
此时只能让 `vc run` 也以管理员身份运行（请同时确认规则里没有危险动作）。

**Q：怎么临时停用一条规则？**
A：给那条 `[[rules]]` 加 `enabled = false`，然后 `vc rules --all` 能看到它被标记为“已停用”。

**Q：模型下载太慢？**
A：Vosk 模型可手动从 <https://alphacephei.com/vosk/models> 下载 zip，解压到 `models/` 目录即可
（目录里应当出现 `am/`、`conf/` 等子目录）。

## 安全说明

- `shell` 动作默认被 `commands.allow_shell = false` 拦住，即使写了规则也不会执行；
- 动作类型还受 `commands.allow` 白名单限制，删掉 `shell` 更保险；
- 调试期间用 `--dry-run`，只显示要做什么，不动系统；
- 不建议让 `vc run` 常驻管理员权限；如果确实需要，请只保留你完全信任的规则；
- 所有识别都在本地完成，音频不会离开你的电脑。

## 项目结构与开发

```
ds_voicecontrol/
├─ pyproject.toml              # 依赖、入口点 vc、pytest 配置
├─ config.toml                 # 你的配置（vc init 生成，已 gitignore）
├─ models/                     # 识别模型（vc setup 下载，已 gitignore）
├─ src/voice_control/
│  ├─ cli.py                   # 命令行子命令
│  ├─ config.py                # TOML 读写 + 校验
│  ├─ config_default.py        # 带注释的默认配置模板
│  ├─ audio.py                 # 麦克风、底噪统计、分段（VAD-lite）
│  ├─ asr/                     # 识别后端：vosk / whisper
│  ├─ models.py                # 模型下载与查找
│  ├─ matching.py              # 归一化、唤醒词、口令匹配
│  ├─ actions.py               # 动作执行（pynput + SendInput）
│  ├─ listener.py              # 主循环 & 状态机
│  └─ ui.py                    # 终端输出
└─ tests/                      # 67 个单元测试
```

开发常用命令：

```powershell
uv run pytest                                   # 跑测试
uv run pytest tests/test_matching.py -q         # 只跑匹配相关
uv run vc say "小助手 音量加" --dry-run           # 不改配置直接验证口令
uv run vc run --wav 某段录音.wav --dry-run       # 用真实音频验证整条链路
```

调试识别问题时，把 `[general] log_level = "debug"`，运行时会打印每段音频的时长、
有效说话时长和峰值电平，方便判断是“没检测到说话”还是“识别错了”。

## 已知限制与后续方向

- 唤醒靠识别文本匹配，不是专用低功耗唤醒引擎；嘈杂环境或电视声可能误唤醒。
  后续可以接 openWakeWord / Porcupine 做专用唤醒，把 CPU 占用再降一个量级。
- 一次只处理一句话，没有多轮对话上下文。
- 目前不会“切到某个已打开的窗口”，`app` 动作只会启动/打开目标，不会激活已有窗口。
- 鼠标动作、自定义正则触发词、图形化配置界面都还没有。
- 非 Windows 平台可以跑（pynput 兜底），但中文文字输入依赖 Windows API。

---

MIT License.
