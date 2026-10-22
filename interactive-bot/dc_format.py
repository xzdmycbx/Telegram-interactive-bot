"""Telegram ⇄ Discord 消息格式转换（纯函数，不做任何网络请求）"""

import html
import re
from collections import namedtuple
from datetime import datetime, timezone

from discord.utils import escape_markdown

# Discord 单条消息上限 2000，按 UTF-16 计算；留出余量给锁帖时机器人代发加的发送者行
DISCORD_LIMIT = 1900
# Telegram 媒体说明文字上限
TG_CAPTION_LIMIT = 1024
TG_TEXT_LIMIT = 4096

Ent = namedtuple("Ent", "type offset length url language")

_HANDLED = {
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "spoiler",
    "code",
    "pre",
    "text_link",
    "url",
    "text_mention",
    "blockquote",
    "expandable_blockquote",
    "custom_emoji",
}
_MARKERS = {
    "bold": "**",
    "italic": "*",
    "underline": "__",
    "strikethrough": "~~",
    "spoiler": "||",
}
_TELETHON_TYPES = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough",
    "MessageEntitySpoiler": "spoiler",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityTextUrl": "text_link",
    "MessageEntityUrl": "url",
    "MessageEntityMentionName": "text_mention",
    "InputMessageEntityMentionName": "text_mention",
    "MessageEntityBlockquote": "blockquote",
    "MessageEntityCustomEmoji": "custom_emoji",
}


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def ents_from_ptb(entities):
    return [
        Ent(str(e.type), e.offset, e.length, e.url, e.language) for e in entities or ()
    ]


def ents_from_telethon(entities):
    result = []
    for e in entities or ():
        t = _TELETHON_TYPES.get(type(e).__name__)
        if t:
            result.append(
                Ent(t, e.offset, e.length, getattr(e, "url", None), getattr(e, "language", None))
            )
    return result


# ---------- Telegram → Discord ----------


def escape_md(text):
    if not text:
        return text or ""
    text = escape_markdown(text)
    # 有序列表 "1. "
    text = re.sub(r"(?m)^(\s*\d+)\.(?=\s)", r"\1\\.", text)
    # <@id> <#id> <:emoji:id> <t:ts> </cmd:id> 等会被 Discord 渲染成提及/时间戳
    text = re.sub(r"<(?=[@#/]|a?:\w+:\d|t:\d)", r"\\<", text)
    return text


def _slice(buf, start, end):
    return buf[start * 2 : end * 2].decode("utf-16-le", errors="replace")


def _surround(inner, marker):
    core = inner.strip()
    if not core:
        return inner
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()) :]
    return f"{lead}{marker}{core}{marker}{trail}"


def _inline_code(raw):
    if not raw:
        return ""
    if "`" not in raw:
        return f"`{raw}`"
    return "`` " + raw.replace("``", "`\u200b`") + " ``"


def _safe_url(url):
    return url.replace(" ", "%20").replace(">", "%3E").replace(")", "%29")


def _render(buf, ents, start, end):
    out = []
    pos = start
    i = 0
    while i < len(ents):
        e = ents[i]
        e_start = max(e.offset, pos)
        e_end = min(e.offset + e.length, end)
        # 嵌套在当前实体内的实体
        j = i + 1
        while j < len(ents) and ents[j].offset < e_end:
            j += 1
        nested = ents[i + 1 : j]
        i = j
        if e_start >= e_end:
            continue

        out.append(escape_md(_slice(buf, pos, e_start)))
        raw = _slice(buf, e_start, e_end)
        at_line_start = e_start == 0 or _slice(buf, e_start - 1, e_start) == "\n"
        followed_by_nl = e_end * 2 >= len(buf) or _slice(buf, e_end, e_end + 1) == "\n"
        t = e.type

        if t == "code":
            insert = _inline_code(raw)
        elif t == "pre":
            lang = re.sub(r"[^\w+#-]", "", e.language or "")
            body = raw.rstrip("\n").replace("```", "`\u200b``")
            insert = f"```{lang}\n{body}\n```"
            insert = ("" if at_line_start else "\n") + insert + ("" if followed_by_nl else "\n")
        elif t == "url":
            insert = raw if re.match(r"(?i)https?://", raw) else escape_md(raw)
        else:
            inner = _render(buf, nested, e_start, e_end)
            if t in ("blockquote", "expandable_blockquote"):
                insert = "\n".join("> " + line for line in inner.split("\n"))
                insert = ("" if at_line_start else "\n") + insert + ("" if followed_by_nl else "\n")
            elif t == "text_link":
                url = e.url or ""
                if re.match(r"(?i)https?://", url):
                    text = inner.replace("[", "\\[").replace("]", "\\]").strip() or escape_md(url)
                    insert = f"[{text}](<{_safe_url(url)}>)"
                else:
                    insert = inner
            elif t in _MARKERS:
                insert = _surround(inner, _MARKERS[t])
            else:
                # text_mention / custom_emoji：保留文字（自定义表情的文字本身就是对应的普通 emoji）
                insert = inner
        out.append(insert)
        pos = e_end
    out.append(escape_md(_slice(buf, pos, end)))
    return "".join(out)


def tg_to_discord(text, entities=()):
    """Telegram 文本 + 实体 → Discord Markdown"""
    if not text:
        return ""
    buf = text.encode("utf-16-le")
    ents = sorted(
        (e for e in entities if e.type in _HANDLED and e.length > 0),
        key=lambda e: (e.offset, -e.length),
    )
    return _render(buf, ents, 0, len(buf) // 2)


def _cut(line, limit):
    """按 UTF-16 长度切出不超过 limit 的前缀，返回 (前缀, 剩余)"""
    total = 0
    for i, ch in enumerate(line):
        total += utf16_len(ch)
        if total > limit:
            return line[:i], line[i:]
    return line, ""


def split_discord(text, limit=DISCORD_LIMIT):
    """按行拆分超长消息；代码块被切断时自动补全/重开围栏"""
    if not text:
        return []
    if utf16_len(text) <= limit:
        return [text]
    parts = []
    cur, cur_len = [], 0
    fence = None  # 当前所在代码块的语言，None 表示不在代码块内

    def flush():
        nonlocal cur, cur_len
        body = "\n".join(cur)
        if fence is not None:
            body += "\n```"
        parts.append(body)
        cur = [f"```{fence}"] if fence is not None else []
        cur_len = utf16_len(cur[0]) if cur else 0

    for line in text.split("\n"):
        rest = line
        while True:
            sep = 1 if cur else 0
            avail = limit - cur_len - sep - (4 if fence is not None else 0)
            if utf16_len(rest) <= avail:
                cur.append(rest)
                cur_len += sep + utf16_len(rest)
                break
            if cur and avail < 200:
                flush()
                continue
            # 超长的行先填满当前段，剩余部分进入下一段
            head, rest = _cut(rest, max(avail, 1))
            cur.append(head)
            cur_len += sep + utf16_len(head)
            flush()
            if not rest:
                break
        for m in re.finditer(r"```([\w+#-]*)", line):
            fence = None if fence is not None else m.group(1)
    if cur:
        fence = None
        parts.append("\n".join(cur))
    return [p for p in parts if p.strip()]


def plain_snippet(text, limit=60):
    """去掉 Markdown 标记，压成一行的短摘要"""
    text = re.sub(r"(?m)^-# .*$", "", text or "")
    text = re.sub(r"\[([^\]]*)\]\(<?[^)]*>?\)", r"\1", text)
    text = re.sub(r"[*_~|`>#\\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def sanitize_webhook_name(name, fallback="用户"):
    """Discord webhook 昵称：1–80 字符，不能包含 discord/clyde/@/#/:/```，不能是 everyone/here"""
    name = (name or "").replace("\n", " ").strip()
    name = re.sub(r"(?i)discord", "Disc0rd", name)
    name = re.sub(r"(?i)clyde", "Cly de", name)
    name = name.replace("```", "").replace("@", "＠").replace("#", "＃").replace(":", "：")
    if not name or name.lower() in ("everyone", "here"):
        name = fallback
    return name[:80]


def human_size(size):
    if not size:
        return "未知大小"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def map_link(lat, lon):
    return f"<https://maps.google.com/?q={lat:.6f},{lon:.6f}>"


def render_location(lat, lon):
    return f"📍 位置：[{lat:.6f}, {lon:.6f}]({map_link(lat, lon)})"


def render_venue(title, address, lat, lon):
    return f"📍 **{escape_md(title)}**\n{escape_md(address)}\n[查看地图]({map_link(lat, lon)})"


def render_contact(first_name, last_name, phone):
    name = " ".join(x for x in (first_name, last_name) if x)
    return f"👤 联系人：{escape_md(name)}  `{phone or '无号码'}`"


def render_poll(question, options, is_quiz=False):
    lines = [f"📊 {'测验' if is_quiz else '投票'}：**{escape_md(question)}**"]
    lines += [f"▫️ {escape_md(o)}" for o in options]
    return "\n".join(lines)


def render_dice(emoji, value):
    return f"{emoji} 结果：**{value}**"


def render_story(chat_name, chat_username, story_id):
    link = f" [打开](<https://t.me/{chat_username}/s/{story_id}>)" if chat_username else ""
    return f"📖 分享了 {escape_md(chat_name or '某人')} 的 Story{link}"


def render_game(title, description):
    return "\n".join(x for x in (f"🎮 游戏：**{escape_md(title)}**", escape_md(description or "")) if x)


def render_invoice(title, description, amount, currency):
    value = f"{amount / 100:.2f} {currency}" if currency != "XTR" else f"{amount} ⭐"
    lines = [f"🧾 账单：**{escape_md(title)}**（{value}）", escape_md(description or "")]
    return "\n".join(x for x in lines if x)


def render_giveaway(winner_count, until_ts, prize=None):
    lines = [f"🎁 抽奖：{winner_count} 名中奖者，<t:{int(until_ts)}:f> 开奖"]
    if prize:
        lines.append(f"奖品：{escape_md(prize)}")
    return "\n".join(lines)


def render_paid_media(count, star_count):
    return f"💎 付费媒体：{count} 个文件，{star_count} ⭐（机器人无法获取内容）"


def render_embed_plain(title=None, url=None, description=None, fields=(), footer=None, image=None, author=None):
    """Discord embed → 纯文本（发往 Telegram 前再转义）"""
    lines = []
    if author:
        lines.append(author)
    if title:
        lines.append(f"{title}{f' ({url})' if url else ''}")
    elif url:
        lines.append(url)
    if description:
        lines.append(description)
    for name, value in fields:
        lines.append(f"{name}：{value}")
    if image:
        lines.append(image)
    if footer:
        lines.append(footer)
    return "\n".join(x for x in lines if x)


# ---------- Discord → Telegram ----------


class MentionResolver:
    """把 Discord 的 ID 解析成可读名字，由调用方实现"""

    def user(self, user_id):
        return None

    def role(self, role_id):
        return None

    def channel(self, channel_id):
        return None


def _format_ts(ts, style):
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return f"<t:{ts}>"
    if style in ("t",):
        return dt.strftime("%H:%M")
    if style in ("T",):
        return dt.strftime("%H:%M:%S")
    if style in ("d", "D"):
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M %Z").strip()


def _resolve_tokens(text, resolver, emit):
    """把提及/表情/时间戳替换成 emit(可读文本) 的结果"""
    resolver = resolver or MentionResolver()
    text = re.sub(
        r"<@!?(\d+)>",
        lambda m: emit("@" + (resolver.user(int(m.group(1))) or "未知用户")),
        text,
    )
    text = re.sub(
        r"<@&(\d+)>",
        lambda m: emit("@" + (resolver.role(int(m.group(1))) or "未知角色")),
        text,
    )
    text = re.sub(
        r"<#(\d+)>",
        lambda m: emit("#" + (resolver.channel(int(m.group(1))) or "未知频道")),
        text,
    )
    text = re.sub(r"<a?:(\w+):\d+>", lambda m: emit(f":{m.group(1)}:"), text)
    text = re.sub(
        r"<t:(-?\d{1,12})(?::([tTdDfFR]))?>",
        lambda m: emit(_format_ts(m.group(1), m.group(2) or "f")),
        text,
    )
    text = re.sub(r"</([^:>]+):\d+>", lambda m: emit("/" + m.group(1)), text)
    return text


_INLINE_RULES = [
    (re.compile(r"\*\*([\s\S]+?)\*\*(?!\*)"), r"<b>\1</b>"),
    (re.compile(r"__([\s\S]+?)__(?!_)"), r"<u>\1</u>"),
    (re.compile(r"(?<![*\w])\*(?=\S)([\s\S]*?\S)\*(?![*\w])"), r"<i>\1</i>"),
    (re.compile(r"(?<![\w_])_(?=\S)([\s\S]*?\S)_(?![\w_])"), r"<i>\1</i>"),
    (re.compile(r"~~([\s\S]+?)~~"), r"<s>\1</s>"),
    (re.compile(r"\|\|([\s\S]+?)\|\|"), r"<tg-spoiler>\1</tg-spoiler>"),
]


def _convert_line(line):
    if m := re.match(r"^#{1,3} (.+)$", line):
        return f"<b>{m.group(1)}</b>"
    if m := re.match(r"^-# (.+)$", line):
        return f"<i>{m.group(1)}</i>"
    if m := re.match(r"^(\s*)[-*] (.+)$", line):
        return f"{m.group(1)}• {m.group(2)}"
    return line


def discord_to_tg_html(content, resolver=None):
    """Discord Markdown → Telegram HTML。格式错乱时调用方应退回 discord_to_tg_plain"""
    if not content:
        return ""
    holders = []

    def hold(fragment):
        holders.append(fragment)
        return f"\x00{len(holders) - 1}\x00"

    text = content.replace("\x00", "")

    # 代码块 / 行内代码：内部不再做任何转换
    def code_block(m):
        lang = m.group(1) or ""
        body = html.escape(m.group(2).strip("\n"), quote=False)
        if lang:
            return hold(f'<pre><code class="language-{lang}">{body}</code></pre>')
        return hold(f"<pre>{body}</pre>")

    text = re.sub(r"```(?:([\w+#.-]+)?\n)?([\s\S]*?)```", code_block, text)
    text = re.sub(
        r"``([\s\S]+?)``|`([^`]+)`",
        lambda m: hold(f"<code>{html.escape((m.group(1) or m.group(2)).strip(), quote=False)}</code>"),
        text,
    )
    # 反斜杠转义
    text = re.sub(r"\\([^\w\s])", lambda m: hold(html.escape(m.group(1), quote=False)), text)
    # 提及、表情、时间戳
    text = _resolve_tokens(text, resolver, lambda s: hold(html.escape(s, quote=False)))
    # 带文字的链接 [text](url)
    text = re.sub(
        r"\[([^\[\]\n]+)\]\(<?(https?://[^\s<>()]+(?:\([^\s<>()]*\)[^\s<>()]*)*)>?\)",
        lambda m: hold(f'<a href="{html.escape(m.group(2), quote=True)}">')
        + m.group(1)
        + hold("</a>"),
        text,
    )
    # 裸链接（含 <url> 形式）
    text = re.sub(
        r"<?(https?://[^\s<>]*[^\s<>.,:;\"')\]])>?",
        lambda m: hold(html.escape(m.group(1), quote=False)),
        text,
    )

    text = html.escape(text, quote=False)

    # 行级：标题、小字、列表、引用
    out_lines = []
    quote = None
    rest_is_quote = False
    for line in text.split("\n"):
        if rest_is_quote:
            quote.append(_convert_line(line))
            continue
        if m := re.match(r"^&gt;&gt;&gt; (.*)$", line):
            quote = (quote or []) + [_convert_line(m.group(1))]
            rest_is_quote = True
            continue
        if m := re.match(r"^&gt; (.*)$", line):
            quote = (quote or []) + [_convert_line(m.group(1))]
            continue
        if quote is not None:
            out_lines.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote = None
        out_lines.append(_convert_line(line))
    if quote is not None:
        out_lines.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
    text = "\n".join(out_lines)

    for pattern, repl in _INLINE_RULES:
        text = pattern.sub(repl, text)

    # 占位符里可能还套着占位符（链接文字中的代码等），循环还原
    for _ in range(3):
        if "\x00" not in text:
            break
        text = re.sub(r"\x00(\d+)\x00", lambda m: holders[int(m.group(1))], text)
    return text.strip()


def discord_to_tg_plain(content, resolver=None):
    """HTML 解析失败时的纯文本兜底：只解析提及和转义，保留其它字符"""
    if not content:
        return ""
    text = _resolve_tokens(content, resolver, lambda s: s)
    text = re.sub(r"\\([^\w\s])", r"\1", text)
    return text.strip()


def tg_html_visible_len(fragment):
    """Telegram 计算长度时只算解析后的可见文字"""
    if not fragment:
        return 0
    return utf16_len(html.unescape(re.sub(r"<[^>]+>", "", fragment)))
