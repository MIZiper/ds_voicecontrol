"""默认配置模板：`vc init` 会把它写到 config.toml（带中文注释，方便直接改）。"""

from __future__ import annotations

DEFAULT_CONFIG_TOML = '''\
# ============================================================
#  ds_voicecontrol 配置文件
#  改完保存即可，重新运行 `vc run` 生效。
#  不确定的项保持默认；标了“自动写入”的项由 `vc calibrate` 负责。
# ============================================================

[general]
# 识别语言：zh（中文）/ en（英文）
language = "zh"
# 日志级别：debug / info / warning
log_level = "info"

[audio]
# 输入设备：留空 = 系统默认麦克风；
# 也可以填设备序号（见 `vc devices`）或名称片段，例如 "Headset"
device = ""
sample_rate = 16000
# 每帧时长（毫秒），越小越灵敏，CPU 占用略高
frame_ms = 30
# 说话前多保留的音频（毫秒），避免第一个字被切掉
pre_roll_ms = 300
# 判定为“有效说话”的最短时长（毫秒），用于过滤咳嗽、敲键盘等短噪声
min_speech_ms = 250
# 单句话最长录制时长（毫秒），超长会强制截断去识别
max_utterance_ms = 8000
# 停顿超过多久算这句话说完（毫秒）
silence_ms = 700
# 静音门限（RMS，0~1）：`vc calibrate` 自动写入，也可手工微调
noise_threshold = 0.010
# 动态倍数：实际门限 = max(noise_threshold, 实时底噪 * threshold_factor)
threshold_factor = 2.5
# 是否跟随环境底噪自动调整门限（空调/风扇声音变化时有用）
adaptive_noise = true

[asr]
# 识别引擎：vosk（离线、轻量，默认）| whisper（faster-whisper，更准但更慢）
engine = "vosk"
# 模型目录（相对本配置文件所在目录）
models_dir = "models"
# Vosk 模型：
#   vosk-model-small-cn-0.22   中文小模型，约 42MB，速度快（推荐先用它）
#   vosk-model-cn-0.22         中文大模型，约 1.3GB，更准
#   vosk-model-small-en-us-0.15 / vosk-model-en-us-0.22   英文模型
vosk_model = "vosk-model-small-cn-0.22"
# faster-whisper 模型：tiny / base / small / medium / large-v3
whisper_model = "small"
whisper_device = "cpu"
whisper_compute_type = "int8"
# 中国大陆下载 HuggingFace 模型慢时可填镜像：https://hf-mirror.com
hf_endpoint = ""
# 模型缺失时是否自动下载（首次需要联网）
auto_download = true

[wake]
# 是否启用唤醒词
enabled = true
# true  = 必须先用唤醒词唤醒；false = 不需要唤醒词，听到口令就执行
require_wake = true
# 唤醒词列表：识别文本里出现任意一个就算唤醒
words = ["小助手", "你好小助手"]
# 模糊匹配相似度 0~1，越大越严格；识别不准时可下调到 0.75
fuzzy = 0.82
# 只说了唤醒词时，接下来多少秒内的一句话按口令处理
window_s = 5.0

[commands]
# 允许执行的动作类型白名单：
#   hotkey / press / text / app / wait / sequence / shell
allow = ["hotkey", "press", "text", "app", "wait", "sequence"]
# 是否允许执行 shell 命令（默认关闭；开启前请确认规则里没有危险命令）
allow_shell = false
# 没匹配到任何口令时：log（打印出来）| ignore（静默）| beep（响一声）
unknown = "log"
# 口令模糊匹配阈值 0~1
match_threshold = 0.75
# 同一条口令的最小执行间隔（毫秒），避免一次说话重复触发
cooldown_ms = 400

[feedback]
# 唤醒成功/命中口令时是否发出提示音
beep = true

# ============================================================
#  口令规则
#  ------------------------------------------------------------
#  [[rules]] 可以写很多条；一条话说完后，取“匹配得分最高”的规则执行。
#    name     规则名，只用于显示
#    phrases  触发词列表；识别文本 等于 / 以它开头 / 包含它 都算命中。
#             触发词后面继续说出的内容会作为 {text} 传给动作，
#             例如“搜索 今天天气” -> {text} = "今天天气"
#    enabled  可选，false = 临时停用这条规则（默认 true）
#    动作字段（与 type 搭配）：
#      type = "hotkey"    keys = ["ctrl", "c"]              按下组合键
#      type = "press"     keys = ["enter"]                  依次按键，可加 repeat / interval_ms
#      type = "text"      text = "你好{text}"                直接输入文字，支持 {text} {date} {time}
#      type = "app"       target = "notepad" / "https://..." 打开程序、文件或网址
#      type = "wait"      ms = 500                           等待
#      type = "sequence"  steps = [ {type="...", ...}, ... ]  组合动作
#      type = "shell"     command = "..."                    执行命令（需 allow_shell = true）
#  按键名可用：ctrl alt shift win / enter esc tab space backspace delete insert
#             home end pageup pagedown up down left right f1~f20
#             volumeup volumedown mute playpause nexttrack prevtrack
#             以及单个字母数字，如 ["ctrl", "shift", "t"]
# ============================================================

# ---- 编辑类 ----
[[rules]]
name = "复制"
phrases = ["复制", "拷贝"]
type = "hotkey"
keys = ["ctrl", "c"]

[[rules]]
name = "粘贴"
phrases = ["粘贴"]
type = "hotkey"
keys = ["ctrl", "v"]

[[rules]]
name = "剪切"
phrases = ["剪切"]
type = "hotkey"
keys = ["ctrl", "x"]

[[rules]]
name = "撤销"
phrases = ["撤销", "撤回"]
type = "hotkey"
keys = ["ctrl", "z"]

[[rules]]
name = "保存"
phrases = ["保存"]
type = "hotkey"
keys = ["ctrl", "s"]

[[rules]]
name = "全选"
phrases = ["全选"]
type = "hotkey"
keys = ["ctrl", "a"]

[[rules]]
name = "输入文字"
phrases = ["输入", "打字"]
type = "text"
text = "{text}"

# ---- 窗口 / 系统 ----
[[rules]]
name = "回车确认"
phrases = ["确认", "回车", "确定"]
type = "press"
keys = ["enter"]

[[rules]]
name = "关闭窗口"
phrases = ["关闭窗口", "关掉窗口", "关掉这个"]
type = "hotkey"
keys = ["alt", "f4"]

[[rules]]
name = "切换窗口"
phrases = ["切换窗口", "切窗口"]
type = "hotkey"
keys = ["alt", "tab"]

[[rules]]
name = "显示桌面"
phrases = ["显示桌面"]
type = "hotkey"
keys = ["win", "d"]

[[rules]]
name = "打开浏览器"
phrases = ["打开浏览器", "启动浏览器"]
type = "app"
target = "https://www.bing.com"

[[rules]]
name = "打开记事本"
phrases = ["打开记事本", "记事本"]
type = "app"
target = "notepad"

# ---- 浏览器 / 应用内 ----
# “搜索 关键词”：新标签页输入关键词并回车（适合浏览器里用）
[[rules]]
name = "搜索"
phrases = ["搜索", "百度", "谷歌"]
type = "sequence"
steps = [
    { type = "hotkey", keys = ["ctrl", "t"] },
    { type = "text", text = "{text}" },
    { type = "press", keys = ["enter"] },
]

[[rules]]
name = "新标签页"
phrases = ["新标签页", "新建标签"]
type = "hotkey"
keys = ["ctrl", "t"]

[[rules]]
name = "关闭标签页"
phrases = ["关闭标签页"]
type = "hotkey"
keys = ["ctrl", "w"]

[[rules]]
name = "下一页"
phrases = ["下一页"]
type = "press"
keys = ["pagedown"]

[[rules]]
name = "上一页"
phrases = ["上一页"]
type = "press"
keys = ["pageup"]

# ---- 音量 / 媒体 ----
[[rules]]
name = "音量加"
phrases = ["音量加", "声音大一点"]
type = "press"
keys = ["volumeup"]
repeat = 3

[[rules]]
name = "音量减"
phrases = ["音量减", "声音小一点"]
type = "press"
keys = ["volumedown"]
repeat = 3

[[rules]]
name = "静音切换"
phrases = ["静音", "安静"]
type = "press"
keys = ["mute"]

[[rules]]
name = "播放暂停"
phrases = ["播放", "暂停"]
type = "press"
keys = ["playpause"]

# ---- 可选的“执行命令”示例（默认被 allow_shell = false 拦住）----
# [[rules]]
# name = "锁屏"
# phrases = ["锁屏"]
# type = "shell"
# command = "rundll32.exe user32.dll,LockWorkStation"
'''
