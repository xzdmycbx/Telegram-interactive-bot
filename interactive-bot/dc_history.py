"""把 TG 后台群里的历史话题迁移到 Discord 帖子（借助 MTProto 按 ID 读取历史消息）。

- 只迁移 ID ≤ history_cutoff（启用 Discord 同步那一刻）的消息，之后的消息由实时同步负责。
- 每迁移一组就记录进度；已经同步过的消息（有映射）会跳过，重复执行不会重复发送。
- 也用于补发重启前没来得及同步的 TG 消息（outbox）。
"""

import asyncio
import re
import time
from collections import defaultdict

from telethon import utils
from telethon.tl.types import (
    MessageActionTopicCreate,
    MessageActionTopicEdit,
    MessageMediaGiveaway,
    MessageMediaStory,
)

from db.model import DiscordMessageMap, DiscordMirrorOrigin, MessageMap, User

from . import general_topic_id
from .dc_avatar import DEFAULT_DISCORD
from .dc_common import OutFile, OutMessage, logger, tg_message_link
from .dc_format import (
    ents_from_telethon,
    escape_md,
    render_contact,
    render_dice,
    render_game,
    render_giveaway,
    render_invoice,
    render_location,
    render_paid_media,
    render_poll,
    render_story,
    render_venue,
    sanitize_webhook_name,
    tg_to_discord,
    utf16_len,
)

BATCH = 100
# 同一发送者连续发言超过这个间隔才再次显示时间
TIME_GAP = 5 * 60
# Discord 管理员的回复镜像到 TG 后台时，首行是「💬 名字 · Discord」（只用于去掉标题，身份以来源表为准）
_DC_HEADER = re.compile(r"^💬 (.+) · Discord$")


def _topic_of(m):
    if isinstance(m.action, MessageActionTopicCreate):
        return m.id
    r = m.reply_to
    if r is None or not getattr(r, "forum_topic", False):
        return None  # General
    return r.reply_to_top_id or r.reply_to_msg_id


def _text(value):
    """新版 TL 中投票的问题/选项是 TextWithEntities"""
    return getattr(value, "text", value) or ""


def _strip_first_line(text, ents):
    """去掉首行，并把实体的 UTF-16 偏移相应前移"""
    first, sep, rest = text.partition("\n")
    cut = utf16_len(first) + (1 if sep else 0)
    kept = []
    for e in ents:
        end = e.offset + e.length
        if end <= cut:
            continue
        start = max(e.offset, cut)
        kept.append(e._replace(offset=start - cut, length=end - start))
    return rest, kept


class TgHistory:
    def __init__(self, bridge, mtproto):
        self.bridge = bridge
        self.db = bridge.db
        self.mtproto = mtproto
        self._index_lock = asyncio.Lock()
        self._index = None  # user_id -> [群消息ID]
        self._group_to_user_msg = {}  # 群消息ID -> 用户侧消息ID
        self._dc_origin = {}  # 群消息ID -> Discord 管理员名字（可信来源记录）

    def reset_index(self):
        """/dc_reset 改变了历史边界，下次需要重新扫描"""
        self._index = None
        self._group_to_user_msg = {}
        self._dc_origin = {}

    async def _get_index(self, progress=None):
        """扫描后台群 1..cutoff 的全部消息，建立 用户 → 历史消息ID 的索引（每次进程只扫一次）"""
        async with self._index_lock:
            if self._index is not None:
                return self._index
            cutoff = self.bridge.history_cutoff
            topics = defaultdict(list)
            titles = {}
            for start in range(1, cutoff + 1, BATCH):
                ids = range(start, min(start + BATCH, cutoff + 1))
                for m in await self.mtproto.get_messages(ids):
                    if m is None:
                        continue
                    topic = _topic_of(m)
                    if not topic or topic == general_topic_id:
                        continue
                    topics[topic].append(m.id)
                    if isinstance(m.action, MessageActionTopicCreate):
                        titles[topic] = m.action.title
                if progress:
                    await progress(min(start + BATCH - 1, cutoff), cutoff)

            user_by_topic = {
                u.message_thread_id: u.user_id
                for u in self.db.query(User).filter(User.message_thread_id != 0).all()
            }
            rows = self.db.query(MessageMap).filter(MessageMap.group_chat_message_id <= cutoff).all()
            user_by_group_msg = {r.group_chat_message_id: r.user_id for r in rows}
            self._group_to_user_msg = {r.group_chat_message_id: r.user_chat_message_id for r in rows}
            self._dc_origin = {
                r.group_chat_message_id: r.author
                for r in self.db.query(DiscordMirrorOrigin).filter(DiscordMirrorOrigin.group_chat_message_id <= cutoff)
            }

            index = defaultdict(list)
            orphan = 0
            for topic, ids in topics.items():
                uid = user_by_topic.get(topic)
                if not uid and (m := re.search(r"\|(\d+)$", titles.get(topic, ""))):
                    uid = int(m.group(1))
                if not uid:
                    votes = [user_by_group_msg[i] for i in ids if i in user_by_group_msg]
                    uid = max(set(votes), key=votes.count) if votes else None
                if uid:
                    index[uid].extend(ids)
                else:
                    orphan += len(ids)
            for ids in index.values():
                ids.sort()
            if orphan:
                logger.info(f"有 {orphan} 条历史消息无法确定所属用户，已跳过")
            self._index = dict(index)
            logger.info(f"历史索引完成：{len(self._index)} 个用户，{sum(map(len, self._index.values()))} 条消息")
            return self._index

    # ---------- Telethon 消息 → OutMessage ----------

    def _tl_file(self, m, kind, filename, label, source, spoiler=False, thumb=None):
        mtproto = self.mtproto

        async def fetch_to(path):
            await mtproto.download(m, path, thumb=thumb)

        f = m.file
        return OutFile(
            kind=kind,
            filename=filename,
            label=label,
            size=None if thumb is not None else f.size,
            fetch_to=fetch_to,
            spoiler=spoiler,
            duration=getattr(f, "duration", None),
            source=source,
            link=tg_message_link(m.id),
        )

    def out_from_telethon(self, unit):
        out = OutMessage()
        for source, m in enumerate(unit):
            if m.message:
                text, ents = m.message, ents_from_telethon(m.entities)
                if self._dc_author(m) is not None and _DC_HEADER.match(text.split("\n", 1)[0]):
                    text, ents = _strip_first_line(text, ents)
                if text:
                    out.bodies[source] = tg_to_discord(text, ents)
            spoiler = bool(getattr(m.media, "spoiler", False))
            f = m.file

            def file(kind, filename, label, spoiler=False, thumb=None):
                return self._tl_file(m, kind, filename, label, source, spoiler, thumb)

            if m.photo:
                out.files.append(file("photo", "photo.jpg", "图片", spoiler))
            elif m.sticker:
                mime = f.mime_type or ""
                if mime == "application/x-tgsticker":
                    sticker = file("sticker_tgs", "sticker.tgs", "贴纸")
                    sticker.fallback = file("sticker", "sticker.webp", "贴纸", thumb=-1)
                    out.files.append(sticker)
                elif mime == "video/webm":
                    out.files.append(file("sticker_video", "sticker.webm", "贴纸"))
                else:
                    out.files.append(file("sticker", "sticker.webp", "贴纸"))
                if f.emoji:
                    out.extras.append(f"-# 贴纸 {f.emoji}")
            elif m.gif:
                out.files.append(file("animation", f.name or "animation.mp4", "GIF", spoiler))
            elif m.video_note:
                out.files.append(file("video_note", "video_note.mp4", "视频消息"))
            elif m.video:
                out.files.append(file("video", f.name or "video.mp4", "视频", spoiler))
            elif m.voice:
                out.files.append(file("voice", "voice.ogg", "语音"))
            elif m.audio:
                out.files.append(file("audio", f.name or "audio.mp3", "音频"))
                title = " - ".join(x for x in (f.performer, f.title) if x)
                if title:
                    out.extras.append(f"🎵 {escape_md(title)}")
            elif m.document:
                out.files.append(file("document", f.name or f"file{f.ext or ''}", "文件"))
            elif m.venue:
                v = m.venue
                out.extras.append(render_venue(v.title, v.address, v.geo.lat, v.geo.long))
            elif m.geo:
                out.extras.append(render_location(m.geo.lat, m.geo.long))
            elif m.contact:
                c = m.contact
                out.extras.append(render_contact(c.first_name, c.last_name, c.phone_number))
            elif m.poll:
                poll = m.poll.poll
                out.extras.append(
                    render_poll(_text(poll.question), [_text(a.text) for a in poll.answers], bool(poll.quiz))
                )
            elif m.dice:
                out.extras.append(render_dice(m.dice.emoticon, m.dice.value))
            elif m.game:
                out.extras.append(render_game(m.game.title, m.game.description))
            elif m.invoice:
                i = m.invoice
                out.extras.append(render_invoice(i.title, i.description, i.total_amount, i.currency))
            elif isinstance(m.media, MessageMediaStory):
                out.extras.append(render_story(None, None, m.media.id))
            elif isinstance(m.media, MessageMediaGiveaway):
                g = m.media
                out.extras.append(render_giveaway(g.quantity, g.until_date.timestamp(), g.prize_description))
            elif type(m.media).__name__ == "MessageMediaPaidMedia":
                out.extras.append(render_paid_media(len(m.media.extended_media), m.media.stars_amount))
            elif m.media and not m.web_preview and not m.message:
                out.extras.append(f"-# [不支持同步的消息类型：{type(m.media).__name__}]")

        fwd = unit[0].forward
        if fwd is not None:
            entity = fwd.sender or fwd.chat
            out.forward_from = (utils.get_display_name(entity) if entity else None) or fwd.from_name or "隐藏用户"
        return out

    def _dc_author(self, m):
        """后台群里由 Discord 管理员回复镜像过来的消息：返回该管理员的名字。
        只认来源表里的记录，不看正文（用户发来的消息同样由机器人转到后台，正文可以伪造）"""
        if m.sender_id != self.mtproto.bot_id:
            return None
        return self._dc_origin.get(m.id)

    def _label(self, m, user_name):
        """返回 (webhook 昵称, 发送者类型 user / dc_admin / system / tg_admin)"""
        if m.sender_id == self.mtproto.bot_id:
            if (name := self._dc_author(m)) is not None:
                # webhook 昵称不能包含 "discord" 字样
                return sanitize_webhook_name(f"💬 {name}"), "dc_admin"
            if m.id in self._group_to_user_msg:
                return sanitize_webhook_name(user_name), "user"
            return "🤖 机器人", "system"
        if m.post or m.sender_id == self.mtproto.group_id:
            name = m.post_author or "匿名管理员"
        else:
            name = (utils.get_display_name(m.sender) if m.sender else "") or "管理员"
        return sanitize_webhook_name(f"🛡️ {name} · TG"), "tg_admin"

    async def _avatar_for(self, m, kind, user_id):
        if kind == "user":
            return await self.bridge._avatar(user_id, self.bridge._db_username(user_id))
        if kind == "system":
            return self.bridge._bot_avatar()
        if kind == "dc_admin":
            return DEFAULT_DISCORD
        if m.post or m.sender_id == self.mtproto.group_id or not m.sender:
            return await self.bridge._avatar(None, admin=True)
        return await self.bridge._avatar(m.sender_id, getattr(m.sender, "username", None), admin=True)

    # ---------- 单用户迁移 ----------

    async def migrate_user(self, user_id):
        """迁移单个用户的历史；必须在该用户的串行队列里执行。返回迁移的消息条数"""
        index = await self._get_index()
        row = self.bridge._thread_row(user_id)
        done_upto = row.history_migrated_upto or 0
        todo = [i for i in index.get(user_id, []) if i > done_upto]
        if not todo:
            row.history_done = True
            self.bridge._commit()
            return 0

        user_name = self.bridge._user_name(user_id)
        state = {"label": None, "ts": 0, "count": 0}

        async def flush(unit):
            if unit:
                await self._post_unit(user_id, user_name, unit, state)
                row = self.bridge._thread_row(user_id)
                row.history_migrated_upto = max(m.id for m in unit)
                self.bridge._commit()

        async with self.bridge.migration_window(user_id):
            if not done_upto:
                await self.bridge._system_post(user_id, f"📜 以下是迁移自 Telegram 的历史记录（共 {len(todo)} 条）")
            album = []
            for start in range(0, len(todo), BATCH):
                for m in await self.mtproto.get_messages(todo[start : start + BATCH]):
                    if m is None:
                        continue
                    if album and m.grouped_id != album[0].grouped_id:
                        await flush(album)
                        album = []
                    if m.grouped_id:
                        album.append(m)
                    else:
                        await flush([m])
            await flush(album)
            await self.bridge._system_post(user_id, "📜 历史记录迁移完成，以下为实时消息")

        row = self.bridge._thread_row(user_id)
        row.history_done = True
        self.bridge._commit()
        return state["count"]

    async def _post_unit(self, user_id, user_name, unit, state, *, origin=None, pairs=None):
        """发送一组消息（单条或一个相册）"""
        first = unit[0]
        ts = int(first.date.timestamp())
        if first.action is not None:
            if isinstance(first.action, MessageActionTopicEdit) and first.action.closed is not None:
                text = "🔒 对话被关闭" if first.action.closed else "🔓 对话被重新打开"
                await self.bridge._system_post(user_id, f"-# {text} · <t:{ts}:f>")
                state["label"] = None
            return
        ids = [m.id for m in unit]
        sync = self.bridge._tg_sync_state(ids)
        if sync == "done":
            return
        if sync == "partial":
            await self.bridge._discard_partial_tg(ids)
        label, kind = self._label(first, user_name)
        prefix = []
        if label != state["label"] or ts - state["ts"] > TIME_GAP:
            prefix.append(f"-# 🕒 <t:{ts}:f>")
        r = first.reply_to
        if r is not None and r.reply_to_top_id and r.reply_to_msg_id:
            target = self.bridge._dmm_first(DiscordMessageMap.group_chat_message_id == r.reply_to_msg_id)
            if target:
                prefix.append(await self.bridge._reply_line(target.discord_thread_id, target.discord_message_id))
        out = self.out_from_telethon(unit)
        if kind == "dc_admin" and not (out.bodies or out.files or out.extras):
            return  # 只有标题行的消息（补发后台镜像时单独发的标题），没有内容
        if pairs is None:
            pairs = [(self._group_to_user_msg.get(m.id), m.id) for m in unit]
        await self.bridge._post_recover(
            user_id,
            out,
            username=label,
            prefix=prefix,
            origin=origin or "history",
            pairs=pairs,
            avatar_url=await self._avatar_for(first, kind, user_id),
        )
        state.update(label=label, ts=ts, count=state["count"] + len(unit))

    async def replay(self, user_id, origin, pairs):
        """补发重启前没同步完的实时消息"""
        group_ids = [g for _, g in pairs if g]
        if not group_ids or self.bridge._tg_sync_state(group_ids) == "done":
            return
        unit = [m for m in await self.mtproto.get_messages(group_ids) if m is not None]
        if not unit:
            return
        user_name = self.bridge._user_name(user_id)
        self._group_to_user_msg.update({g: u for u, g in pairs})
        await self.bridge._ensure_thread(user_id)
        state = {"label": None, "ts": 0, "count": 0}
        await self._post_unit(user_id, user_name, unit, state, origin=origin, pairs=pairs)

    async def replay_admin_edit(self, user_id, group_msg_id):
        """补发重启前没同步完的 TG 管理员编辑：从后台群读取最新内容"""
        (m,) = await self.mtproto.get_messages([group_msg_id])
        if m is None:
            return
        avatar = await self._avatar_for(m, "tg_admin", user_id)
        await self.bridge._apply_admin_edit(
            group_msg_id, tg_to_discord(m.message or "", ents_from_telethon(m.entities)), avatar
        )

    # ---------- /dc_migrate ----------

    async def run(self, status_chat_id, status_message_id, only_user_id=None):
        bot = self.bridge.bot
        last_edit = [0.0]

        async def report(text, force=False):
            now = time.time()
            if not force and now - last_edit[0] < 5:
                return
            last_edit[0] = now
            try:
                await bot.edit_message_text(text, chat_id=status_chat_id, message_id=status_message_id)
            except Exception:
                pass

        await self.bridge.ready.wait()
        if self.bridge.failed:
            await report("❌ Discord 同步未能启动，无法迁移，请检查日志。", force=True)
            return
        try:
            await report("⏳ 正在连接 Telegram 并扫描历史消息…", force=True)
            index = await self._get_index(progress=lambda done, total: report(f"⏳ 正在扫描历史消息 {done}/{total}"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("扫描历史消息失败")
            await report(f"❌ 扫描历史消息失败：{e}", force=True)
            return

        if only_user_id:
            users = [only_user_id] if only_user_id in index else []
        else:
            users = sorted(index, key=lambda uid: index[uid][0])
        total_msgs, skipped, failed = 0, 0, []
        for n, uid in enumerate(users, 1):
            row = self.bridge._thread_row(uid)
            if row and row.history_done:
                skipped += 1
                continue

            async def job(uid=uid):
                await self.bridge._ensure_thread(uid, history=False)
                return await self.migrate_user(uid)

            try:
                count = await self.bridge._submit(uid, job, "history")
                if count is None:
                    raise RuntimeError("任务未执行（Discord 同步不可用或会话已被清除）")
                total_msgs += count
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failed.append((uid, str(e)))
            await report(
                f"⏳ 迁移中：用户 {n}/{len(users)}，已迁移 {total_msgs} 条消息" + (f"，失败 {len(failed)} 个" if failed else "")
            )

        lines = [
            ("⚠️ 历史迁移结束（部分失败）" if failed else "✅ 历史迁移完成")
            + f"：共 {len(users)} 个用户，本次迁移 {total_msgs} 条消息"
        ]
        if skipped:
            lines.append(f"{skipped} 个用户此前已迁移过")
        if failed:
            lines.append(f"{len(failed)} 个用户失败（进度已保存，再次发送 /dc_migrate 会从断点继续）：")
            lines += [f"• {self.bridge._user_name(uid)}（{uid}）：{err[:100]}" for uid, err in failed[:10]]
        await report("\n".join(lines), force=True)
