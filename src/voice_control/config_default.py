"""默认配置模板：`vc init` 会把它写到 config.toml（带中文注释，方便直接改）。"""

from __future__ import annotations

DEFAULT_CONFIG_TOML = '''\
# ============================================================
#  ds_voicecontrol 配置文件
#
#  工作方式：
#    本地只做两件事 —— ① 判断有没有人在说话（音量门限）② 听唤醒词（关键词检测）
#    听到唤醒词后，把接下来的那句话音频直接交给大模型，由模型从下面的
#    命令表（[[rules]]）里挑一条，再在本地执行对应的按键 / 操作。
#
#  改完保存即可，重新运行 `vc run` 生效。
# ============================================================

[general]
# 识别/口令语言：zh（中文）/ en（英文）
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
# 唤醒后单条口令最长录制时长（毫秒），超长会强制截断去识别
max_utterance_ms = 8000
# 停顿超过多久算这句话说完（毫秒）
silence_ms = 700
# 静音门限（RMS，0~1）：`vc calibrate` 自动写入，也可手工微调
noise_threshold = 0.010
# 动态倍数：实际门限 = max(noise_threshold, 实时底噪 * threshold_factor)
threshold_factor = 2.5
# 是否跟随环境底噪自动调整门限（空调/风扇声音变化时有用）
adaptive_noise = true

[wake]
# 是否启用本地唤醒词。false = 不需要唤醒词，检测到有人说话就把音频交给大模型
enabled = true
# 唤醒词列表：中文即可，程序会自动生成关键词文件（需要 pypinyin）
words = ["小助手", "你好小助手"]
# 唤醒词模型目录（相对本配置文件所在目录）
models_dir = "models"
model = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
# 关键词文件：留空 = 自动生成 models/wake-keywords.txt
keywords_file = ""
# 命中阈值 0~1：调大更严格（不容易被误唤醒），调小更容易唤醒
threshold = 0.25
# 关键词权重（一般不用改）
score = 1.0
# 关键词检测用的线程数
num_threads = 2
# 唤醒后等待你开始说话的最长时间（毫秒）
wait_ms = 5000
# 唤醒后等待开始时，最多录制多久的口令（毫秒）
command_max_ms = 8000
# 模型缺失时是否自动下载（首次需要联网，约 31MB）
auto_download = true

[llm]
# 命令理解：音频直接发给多模态大模型，由它返回要执行哪条命令
enabled = true
# OpenAI 兼容接口地址：默认阿里云百炼（DashScope）
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 支持音频输入的模型，例如：
#   qwen-audio-turbo-latest（默认，便宜快）
#   qwen3-omni-flash / qwen-omni-turbo-latest（更新，能力更强）
model = "qwen-audio-turbo-latest"
# API Key：推荐用环境变量（下面写变量名），不要把 key 提交到代码仓库
api_key_env = "DASHSCOPE_API_KEY"
# 也可以直接写在配置里（不建议，且优先级高于环境变量）
api_key = ""
# 单次请求超时（秒）
timeout_s = 30.0
# 采样温度，命令解析建议 0
temperature = 0.0
# 要求接口返回 JSON（DashScope 兼容模式支持）
response_format_json = true
# 发给模型的音频格式：wav（16k 单声道）
audio_format = "wav"
# 额外的提示词：可以在这里补充你自己的说法习惯
extra_prompt = ""
# 调试用：把每次发给模型的音频存到这个目录（留空 = 不保存）
save_audio_dir = ""

[commands]
# 允许执行的动作类型白名单：
#   hotkey / press / text / app / focus / wait / sequence / shell
allow = ["hotkey", "press", "text", "app", "focus", "wait", "sequence"]
# 是否允许执行 shell 命令（默认关闭；开启前请确认规则里没有危险命令）
allow_shell = false
# 大模型没匹配到命令、或调用失败时：log（打印）| ignore（静默）| beep（响一声）
unknown = "log"
# 同一条命令的最小执行间隔（毫秒），防止一次说话重复触发
cooldown_ms = 400
# 本地兜底：模型没给命令、但返回了它听到的原话时，用本地规则再匹配一次
fallback_local = true
# 本地兜底匹配的相似度阈值 0~1
match_threshold = 0.75

[feedback]
# 唤醒成功 / 执行命令时是否发出提示音
beep = true

# ============================================================
#  命令表（允许执行的命令）
#  ------------------------------------------------------------
#  大模型只能从这里挑命令，挑不到就什么都不做，所以这张表 = 你的安全边界。
#
#    id          命令标识（模型返回的就是它）；留空则用 name
#    name        命令名，只用于显示
#    description 给模型看的说明，写清楚“什么时候该用这条命令”
#    phrases     示例说法（也是本地兜底匹配用的触发词）
#    slots       需要模型提取的槽位名，例如 ["query"]；动作里可用 {query}
#    enabled     可选，false = 临时停用（模型也看不到它）
#
#  动作字段（与 type 搭配）：
#    type = "hotkey"    keys = ["ctrl", "c"]                按下组合键
#    type = "press"     keys = ["enter"]                    依次按键，可加 repeat / interval_ms
#    type = "text"      text = "你好{query}"                 输入文字，支持 {槽位} {text} {date} {time}
#    type = "app"       target = "notepad" / "https://..."   打开程序、文件或网址
#    type = "focus"     title = "记事本"                     把指定窗口切到前台
#    type = "wait"      ms = 500                             等待
#    type = "sequence"  steps = [ {...}, {...} ]              组合动作
#    type = "shell"     command = "..."                      执行命令（需 allow_shell = true）
#
#  按键名可用：ctrl alt shift win / enter esc tab space backspace delete insert
#             home end pageup pagedown up down left right f1~f24
#             volumeup volumedown mute playpause nexttrack prevtrack
#             以及单个字母数字，如 ["ctrl", "shift", "t"]
# ============================================================

# ---- 编辑类 ----
[[rules]]
id = "copy"
name = "复制"
description = "复制当前选中的内容到剪贴板"
phrases = ["复制", "拷贝", "复制一下"]
type = "hotkey"
keys = ["ctrl", "c"]

[[rules]]
id = "paste"
name = "粘贴"
description = "把剪贴板里的内容粘贴到光标处"
phrases = ["粘贴", "贴上"]
type = "hotkey"
keys = ["ctrl", "v"]

[[rules]]
id = "cut"
name = "剪切"
description = "剪切当前选中的内容"
phrases = ["剪切"]
type = "hotkey"
keys = ["ctrl", "x"]

[[rules]]
id = "undo"
name = "撤销"
description = "撤销上一步操作"
phrases = ["撤销", "撤回", "返回上一步"]
type = "hotkey"
keys = ["ctrl", "z"]

[[rules]]
id = "save"
name = "保存"
description = "保存当前文档"
phrases = ["保存", "存一下"]
type = "hotkey"
keys = ["ctrl", "s"]

[[rules]]
id = "select_all"
name = "全选"
description = "选中全部内容"
phrases = ["全选"]
type = "hotkey"
keys = ["ctrl", "a"]

[[rules]]
id = "type_text"
name = "输入文字"
description = "把用户说的内容直接打出来。当用户说“输入…”“打上…”“写上…”时使用，把要输入的内容放进 text 槽位"
phrases = ["输入你好世界", "打字", "写上"]
slots = ["text"]
type = "text"
text = "{text}"

# ---- 窗口 / 系统 ----
[[rules]]
id = "enter"
name = "回车确认"
description = "按下回车键（确认、发送、换行）"
phrases = ["确认", "回车", "确定", "发送"]
type = "press"
keys = ["enter"]

[[rules]]
id = "close_window"
name = "关闭窗口"
description = "关闭当前窗口或程序"
phrases = ["关闭窗口", "关掉窗口", "关掉这个"]
type = "hotkey"
keys = ["alt", "f4"]

[[rules]]
id = "switch_window"
name = "切换窗口"
description = "在打开的窗口之间切换"
phrases = ["切换窗口", "切窗口"]
type = "hotkey"
keys = ["alt", "tab"]

[[rules]]
id = "show_desktop"
name = "显示桌面"
description = "最小化所有窗口，显示桌面"
phrases = ["显示桌面"]
type = "hotkey"
keys = ["win", "d"]

[[rules]]
id = "open_browser"
name = "打开浏览器"
description = "打开浏览器（首页）"
phrases = ["打开浏览器", "启动浏览器"]
type = "app"
target = "https://www.bing.com"

[[rules]]
id = "open_notepad"
name = "打开记事本"
description = "打开 Windows 记事本"
phrases = ["打开记事本", "记事本"]
type = "app"
target = "notepad"

# ---- 浏览器 / 应用内 ----
# “搜索 今天天气”：模型会把“今天天气”放进 query 槽位
[[rules]]
id = "search"
name = "搜索"
description = "在浏览器里搜索用户说的内容。用户说“搜索…”“查一下…”“百度一下…”时使用，搜索词放进 query 槽位"
phrases = ["搜索今天天气", "查一下", "百度一下"]
slots = ["query"]
type = "sequence"
steps = [
    { type = "hotkey", keys = ["ctrl", "t"] },
    { type = "text", text = "{query}" },
    { type = "press", keys = ["enter"] },
]

[[rules]]
id = "new_tab"
name = "新标签页"
description = "在浏览器里新建一个标签页"
phrases = ["新标签页", "新建标签"]
type = "hotkey"
keys = ["ctrl", "t"]

[[rules]]
id = "close_tab"
name = "关闭标签页"
description = "关闭浏览器当前标签页"
phrases = ["关闭标签页"]
type = "hotkey"
keys = ["ctrl", "w"]

[[rules]]
id = "page_down"
name = "下一页"
description = "向下翻一页"
phrases = ["下一页", "往下翻"]
type = "press"
keys = ["pagedown"]

[[rules]]
id = "page_up"
name = "上一页"
description = "向上翻一页"
phrases = ["上一页", "往上翻"]
type = "press"
keys = ["pageup"]

# ---- 音量 / 媒体 ----
[[rules]]
id = "volume_up"
name = "音量加"
description = "把系统音量调大"
phrases = ["音量加", "声音大一点", "大声点"]
type = "press"
keys = ["volumeup"]
repeat = 3

[[rules]]
id = "volume_down"
name = "音量减"
description = "把系统音量调小"
phrases = ["音量减", "声音小一点", "小声点"]
type = "press"
keys = ["volumedown"]
repeat = 3

[[rules]]
id = "mute"
name = "静音切换"
description = "切换系统静音"
phrases = ["静音", "安静"]
type = "press"
keys = ["mute"]

[[rules]]
id = "play_pause"
name = "播放暂停"
description = "播放或暂停当前媒体"
phrases = ["播放", "暂停"]
type = "press"
keys = ["playpause"]

# ---- 窗口激活示例：先切到记事本，再保存 ----
# 如果你的按键总是发给了错误的窗口，可以加这样一条“先激活再操作”的命令
# [[rules]]
# id = "focus_notepad"
# name = "切到记事本"
# description = "把记事本窗口切到最前面"
# phrases = ["切到记事本"]
# type = "focus"
# title = "记事本"

# ---- 可选的“执行命令”示例（默认被 allow_shell = false 拦住）----
# [[rules]]
# id = "lock_screen"
# name = "锁屏"
# description = "锁定电脑"
# phrases = ["锁屏"]
# type = "shell"
# command = "rundll32.exe user32.dll,LockWorkStation"
'''
