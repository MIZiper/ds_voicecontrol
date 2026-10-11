"""文本归一化与口令/唤醒词匹配。

归一化做的事情：全角转半角、去掉空白与中英文标点、小写化，并去掉口语填充词
（“嗯 / 那个 / 帮我 / …一下”），让“帮我复制一下”也能命中“复制”这条口令。
归一化时保留“归一化字符 -> 原文字符下标”的映射，因此可以准确地把触发词后面
的剩余内容取出来当作 {text} 槽位。
"""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# 归一化时需要丢掉的字符（空白在中英文里都算）
_PUNCT = set(
    " \t\r\n\u3000"
    "，。！？、；：,.!?;:"
    "\"'“”‘’`´"
    "()（）[]【】{}<>《》〈〉"
    "—-–—_~·|/\\+*=&%$#@^"
)

# 句首常见的口语/礼貌词，匹配前统一去掉（查询与口令两侧都会去掉，保证对称）
_PREFIX_FILLERS = (
    "你好",
    "那个",
    "就是",
    "然后",
    "麻烦",
    "请",
    "帮我",
    "帮忙",
    "给我",
    "把",
    "嗯",
    "呃",
    "啊",
)

# 句尾语气词（仅当去掉后还剩下内容时才去）
_TRAILING_FILLERS = ("一下", "一遍", "吧", "呀", "吗", "呢", "啊", "哦", "谢谢", "好吗")


@dataclass(frozen=True)
class TextView:
    """一段文本的归一化视图。"""

    original: str
    norm: str
    index_map: list[int]

    def __bool__(self) -> bool:
        return bool(self.norm)


def _halfwidth(ch: str) -> str:
    code = ord(ch)
    if code == 0x3000:
        return " "
    if 0xFF01 <= code <= 0xFF5E:
        return chr(code - 0xFEE0)
    return ch


def view(text: str) -> TextView:
    """把原始文本转成归一化视图（小写、去标点空白、去口头禅）。"""
    if not text:
        return TextView("", "", [])
    chars: list[str] = []
    index_map: list[int] = []
    for i, raw in enumerate(text):
        ch = _halfwidth(raw)
        if not ch or ch.isspace() or ch in _PUNCT:
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Mn", "Sk", "So", "Po", "Ps", "Pe", "Pd", "Pc"):
            continue
        chars.append(ch)
        index_map.append(i)

    def drop_prefix(count: int) -> None:
        del chars[:count]
        del index_map[:count]

    changed = True
    while changed and chars:
        changed = False
        current = "".join(chars).lower()
        for filler in _PREFIX_FILLERS:
            if current.startswith(filler):
                drop_prefix(len(filler))
                changed = True
                break
    while chars:
        current = "".join(chars).lower()
        for tail in _TRAILING_FILLERS:
            if current.endswith(tail) and len(chars) > len(tail):
                del chars[-len(tail):]
                del index_map[-len(tail):]
                break
        else:
            break

    return TextView(text, "".join(chars).lower(), index_map)


def remainder_after(text_view: TextView, norm_end: int) -> str:
    """取归一化文本 [norm_end:] 对应的原文内容（去掉首尾标点空白）。"""
    if norm_end >= len(text_view.index_map):
        return ""
    start = text_view.index_map[norm_end]
    return text_view.original[start:].strip(" \t，。！？、；：,.!?;:\u3000")


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class RuleMatch:
    rule: Any
    phrase: str
    score: float
    remainder: str

    @property
    def name(self) -> str:
        return getattr(self.rule, "name", "?")


def score_phrase(query: str, phrase: str, threshold: float) -> tuple[float, int]:
    """给“查询文本 vs 触发词”打分，返回 (得分, 归一化文本中的匹配结束下标)。

    结束下标用于取出 {text} 槽位；模糊匹配时无法定位，返回 0。
    """
    if not query or not phrase:
        return 0.0, 0
    if query == phrase:
        return 1.0, len(phrase)
    if query.startswith(phrase):
        return 0.98, len(phrase)
    pos = query.find(phrase)
    if pos >= 0:
        # 出现在句中：越靠前得分越高
        return max(0.80, 0.95 - 0.02 * pos), pos + len(phrase)
    if len(phrase) >= 3 and len(query) >= 3:
        ratio = similarity(query, phrase)
        if ratio >= threshold:
            return ratio * 0.8, 0
    return 0.0, 0


def match_rules(text: str, rules: Iterable[Any], threshold: float = 0.75) -> RuleMatch | None:
    """在规则列表里找最匹配的一条。"""
    text_view = view(text)
    if not text_view:
        return None
    best: RuleMatch | None = None
    best_end = 0
    for rule in rules:
        if not getattr(rule, "enabled", True):
            continue
        for phrase in getattr(rule, "phrases", ()) or ():
            phrase_view = view(phrase)
            if not phrase_view:
                continue
            score, end = score_phrase(text_view.norm, phrase_view.norm, threshold)
            if score <= 0:
                continue
            if best is None or score > best.score + 1e-9 or (
                abs(score - best.score) <= 1e-9 and end > best_end
            ):
                best = RuleMatch(rule, phrase, score, remainder_after(text_view, end))
                best_end = end
    return best


def find_wake_word(text: str, words: Sequence[str], fuzzy: float = 0.82) -> tuple[str | None, str]:
    """在识别文本里找唤醒词。

    返回 (命中的唤醒词, 唤醒词之后剩下的内容)。没找到时返回 (None, "")。
    """
    text_view = view(text)
    if not text_view:
        return None, ""
    for word in words or ():
        word_view = view(word)
        if not word_view:
            continue
        norm_word = word_view.norm
        if text_view.norm == norm_word:
            return word, ""
        if text_view.norm.startswith(norm_word):
            return word, remainder_after(text_view, len(norm_word))
        pos = text_view.norm.find(norm_word)
        if pos >= 0:
            return word, remainder_after(text_view, pos + len(norm_word))
    # 整句模糊匹配（识别把唤醒词听错一两个字时仍能唤醒）
    for word in words or ():
        word_view = view(word)
        if len(word_view.norm) < 3:
            continue
        ratio = similarity(text_view.norm, word_view.norm)
        if ratio >= fuzzy:
            return word, ""
    return None, ""
