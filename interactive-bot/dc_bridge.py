"""Telegram ⇄ Discord 同步（核心）。

- 每个 TG 用户对应 Discord 论坛频道里的一个帖子（相当于 TG 后台群的一个话题）。
- TG → Discord 见 dc_tg2dc.py；Discord → TG 见 dc_dc2tg.py；历史迁移见 dc_history.py。
- 同一用户的所有同步任务串行执行，保证顺序；Discord 侧失败不影响 TG 侧原有流程。
- 入队的同步任务先落库（DiscordOutbox）。暂时性失败在原队列里退避重试（保持顺序），
  仍失败则保留记录，由定时任务和下次启动继续补发；永久失败才放弃。
- 每次同步用 DiscordPost.stage 记录进度，发了一半的会在重试前先撤回再整条重发。
- 关闭/锁定状态用「三方对账」：记录上次两边一致的状态，哪边变了就以哪边为准同步另一边；
  事件只是触发对账的信号，执行时读取两边的当前状态。启动时会主动对账一次，失败会退避重试。
- /clear 会让该用户的会话「代次」加一，之前入队、还没执行完的任务全部作废。
- webhook 发送经过全局令牌桶匀速发出，尽量不触发 Discord 的限流。
"""

import asyncio
import contextlib
import contextvars
import io
import json
from collections import OrderedDict, defaultdict
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from telegram.error import BadRequest

from db.model import BridgeState, DiscordMessageMap, DiscordOutbox, DiscordThread, MessageMap, User

from . import admin_group_id, general_topic_id
from .dc_avatar import AvatarProvider
from .dc_common import (
    NO_MENTIONS,
    WEBHOOK_NAME,
    ConversationCleared,
    RatePacer,
    ThreadGone,
    is_transient,
    logger,
)
from .dc_dc2tg import DiscordToTgMixin
from .dc_format import escape_md
from .dc_tg2dc import TgToDiscordMixin
from .utils import get_topic_status, set_topic_status

# 当前正在执行的同步任务属于哪个用户的哪一代会话
_job_ctx = contextvars.ContextVar("discord_bridge_job", default=None)

INLINE_RETRY_DELAYS = (5, 15, 45)  # 暂时性失败在队列里的重试间隔（秒）
MAX_OUTBOX_ATTEMPTS = 20  # 超过后放弃
OUTBOX_RETRY_INTERVAL = 600  # 定时补发间隔（秒）
MEDIA_CONCURRENCY = 2  # 同时进行的下载/转码/上传数
# webhook 发送节奏：平均每分钟约 28 条，允许 5 条突发（Discord 同频道约 30 条/分钟）
WEBHOOK_RATE = 28 / 60
WEBHOOK_BURST = 5


class _SerialRunner:
    """按 key 串行执行任务；不同 key 之间并发。任务异常会记录日志并传给等待者，但不影响后续任务"""

    def __init__(self, on_error=None):
        self._tails = {}
        self._tasks = set()
        self.on_error = on_error

    def submit(self, key, factory, what=""):
        prev = self._tails.get(key)

        async def run():
            if prev is not None:
                await asyncio.wait([prev])
            try:
                return await factory()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"Discord 同步任务失败 [{what}] user={key}")
                if self.on_error:
                    self.on_error()
                raise

        task = asyncio.get_running_loop().create_task(run())
        self._tails[key] = task
        self._tasks.add(task)

        def done(t):
            self._tasks.discard(t)
            if self._tails.get(key) is t:
                del self._tails[key]
            if not t.cancelled():
                t.exception()  # 已记录日志，这里只是标记为已取回，避免 asyncio 警告

        task.add_done_callback(done)
        if len(self._tasks) > 1000 and len(self._tasks) % 500 == 1:
            logger.warning(f"Discord 同步积压任务 {len(self._tasks)} 个")
        return task

    async def drain(self, timeout):
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    async def close(self):
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class _Client(discord.Client):
    def __init__(self, bridge, **kwargs):
        super().__init__(**kwargs)
        self.bridge = bridge

    async def on_ready(self):
        await self.bridge._on_ready()

    async def on_message(self, message):
        await self.bridge._on_message(message)

    async def on_raw_message_edit(self, payload):
        await self.bridge._on_raw_message_edit(payload)

    async def on_raw_message_delete(self, payload):
        await self.bridge._on_raw_message_delete(payload)

    async def on_raw_bulk_message_delete(self, payload):
        await self.bridge._on_raw_bulk_message_delete(payload)

    async def on_raw_reaction_add(self, payload):
        await self.bridge._on_raw_reaction(payload, add=True)

    async def on_raw_reaction_remove(self, payload):
        await self.bridge._on_raw_reaction(payload, add=False)

    async def on_raw_thread_update(self, payload):
        await self.bridge._on_raw_thread_update(payload)

    async def on_raw_thread_delete(self, payload):
        await self.bridge._on_raw_thread_delete(payload)


class DiscordBridge(TgToDiscordMixin, DiscordToTgMixin):
    def __init__(
        self,
        db,
        *,
        token,
        forum_channel_id,
        notify_channel_id=0,
        admin_role_ids=(),
        proxy=None,
        bot_token="",
        avatar_public_url=None,
        avatar_port=8080,
        avatar_dir="./assets/avatars",
    ):
        self.db = db
        self.token = token
        self.forum_channel_id = forum_channel_id
        self.notify_channel_id = notify_channel_id
        self.admin_role_ids = set(admin_role_ids)
        self.proxy = proxy
        self.bot = None  # telegram.Bot，start() 时注入
        self.mtproto = None  # MtprotoClient，可选
        self.history = None  # TgHistory，可选
        self.on_clear = None  # async (user_id)，由主程序提供，Discord 的 /clear 使用
        self.history_cutoff = 0
        self.avatars = None  # AvatarProvider，start() 时创建
        self._avatar_conf = dict(
            bot_token=bot_token, public_url=avatar_public_url, port=avatar_port, directory=avatar_dir
        )

        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.guild_reactions = True
        intents.message_content = True
        self.client = _Client(self, intents=intents, allowed_mentions=NO_MENTIONS, proxy=proxy)
        self.tree = app_commands.CommandTree(self.client)

        @app_commands.command(name="clear", description="删除该用户的会话（Telegram 话题与本帖）")
        async def clear_command(interaction: discord.Interaction):
            await self._on_clear_command(interaction)

        self.tree.add_command(clear_command)

        self.forum: Optional[discord.ForumChannel] = None
        self.webhook: Optional[discord.Webhook] = None
        self.http: Optional[aiohttp.ClientSession] = None
        self.ready = asyncio.Event()
        self.failed = False
        self.runner = _SerialRunner(on_error=self._rollback)
        self.media_sem = asyncio.Semaphore(MEDIA_CONCURRENCY)
        self.webhook_pacer = RatePacer(WEBHOOK_RATE, WEBHOOK_BURST)
        self._client_task = None
        self._migration_task = None
        self._background = set()
        self._albums = {}  # (chat_id, media_group_id) -> (time, [Message])
        self._snippets = OrderedDict()  # discord_message_id -> (作者, 摘要)
        self._edit_seen = OrderedDict()  # discord_message_id -> edited_timestamp
        self._reactors = OrderedDict()  # (discord_message_id, emoji) -> 添加该表情的管理员
        self._reconcile_pending = set()
        self._reconcile_failures = defaultdict(int)
        self._generation = defaultdict(int)  # user_id -> 会话代次
        self._outbox_inflight = set()

    # ---------- 生命周期 ----------

    async def start(self, bot):
        self.bot = bot
        self.http = aiohttp.ClientSession()
        self.avatars = AvatarProvider(bot=bot, http=self.http, proxy=self.proxy, **self._avatar_conf)
        if self.avatars.public_url:
            try:
                await self.avatars.start_server()
            except OSError as e:
                logger.error(f"头像服务启动失败（端口 {self.avatars.port}）：{e}，改用公开头像链接")
                self.avatars.public_url = None
        await self._ensure_history_cutoff()
        # 先于任何实时任务入队，保证同一用户的补发在新消息之前
        self._replay_outbox()
        self._client_task = asyncio.get_running_loop().create_task(self._run_client())

    async def close(self):
        # 先尽量把队列里的任务做完；做不完的保留在 outbox，下次启动补发
        await self.runner.drain(timeout=20)
        for task in [self._migration_task, *self._background]:
            if task:
                task.cancel()
        await self.runner.close()
        if not self.client.is_closed():
            await self.client.close()
        if self.avatars:
            await self.avatars.close()
        if self.http:
            await self.http.close()
        if self.mtproto:
            await self.mtproto.close()

    def _rollback(self):
        try:
            self.db.rollback()
        except Exception:
            logger.exception("数据库回滚失败")

    def _commit(self):
        """提交；失败时回滚，避免共享会话卡在失败状态影响 TG 主流程"""
        try:
            self.db.commit()
        except Exception:
            self._rollback()
            raise

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _later(self, delay, func, *args):
        asyncio.get_running_loop().call_later(delay, func, *args)

    def _fail(self, reason):
        logger.error(f"{reason}，Discord 同步已停用")
        self.failed = True
        self.ready.set()

    async def _run_client(self):
        try:
            await self.client.start(self.token)
        except discord.LoginFailure:
            self._fail("Discord 登录失败，请检查 DISCORD_BOT_TOKEN")
        except discord.PrivilegedIntentsRequired:
            self._fail("Discord 机器人缺少 Message Content Intent，请在开发者后台 Bot 页面开启")
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Discord 客户端异常退出")
            if not self.ready.is_set():
                self._fail("Discord 客户端无法启动")

    async def _on_ready(self):
        if self.ready.is_set():
            return  # 断线重连
        for attempt in range(1, 6):
            try:
                channel = self.client.get_channel(self.forum_channel_id) or await self.client.fetch_channel(
                    self.forum_channel_id
                )
                if not isinstance(channel, discord.ForumChannel):
                    return self._fail("DISCORD_FORUM_CHANNEL_ID 必须是一个论坛（Forum）频道")
                self.forum = channel
                await self._ensure_webhook()
                break
            except discord.Forbidden as e:
                return self._fail(f"Discord 机器人权限不足（需要查看频道、管理 Webhook 等权限）：{e}")
            except discord.NotFound:
                return self._fail(f"找不到 Discord 论坛频道 {self.forum_channel_id}")
            except Exception as e:
                logger.warning(f"Discord 初始化失败（第 {attempt} 次），稍后重试：{e}")
                await asyncio.sleep(5 * attempt)
        else:
            return self._fail("Discord 初始化多次失败")
        await self._setup_commands()
        logger.info(f"Discord 已连接：{self.client.user}，论坛频道：#{self.forum.name}")
        self.ready.set()
        self._spawn(self._startup_reconcile())
        self._spawn(self._outbox_retry_loop())

    async def _setup_commands(self):
        guild = discord.Object(id=self.forum.guild.id)
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except discord.HTTPException as e:
            logger.warning(f"注册 Discord 斜杠命令失败（邀请机器人时需要勾选 applications.commands）：{e}")

    @property
    def active(self):
        return not (self.failed and self.ready.is_set())

    @staticmethod
    def _now():
        return asyncio.get_running_loop().time()

    # ---------- 任务 ----------

    def _check_alive(self):
        """当前任务所属的会话已被 /clear 时中止，避免旧任务继续发消息或重建帖子"""
        ctx = _job_ctx.get()
        if ctx and self._generation[ctx[0]] != ctx[1]:
            raise ConversationCleared()

    def _submit(self, user_id, factory, what=""):
        generation = self._generation[user_id]

        async def job():
            await self.ready.wait()
            if self.failed or self._generation[user_id] != generation:
                return None
            _job_ctx.set((user_id, generation))
            try:
                return await factory()
            except ConversationCleared:
                logger.info(f"会话已清除，放弃任务 [{what}] user={user_id}")
                return None

        return self.runner.submit(user_id, job, what)

    # ---------- 头像 ----------

    async def _avatar(self, user_id, username=None, *, admin=False):
        if not self.avatars:
            return None
        return await self.avatars.url_for(user_id, username, admin=admin)

    def _bot_avatar(self):
        avatar = getattr(self.client.user, "display_avatar", None)
        return str(avatar.url) if avatar else None

    def _db_username(self, user_id):
        u = self.db.query(User).filter(User.user_id == user_id).first()
        return u.username if u else None

    # ---------- outbox：持久化待办 ----------

    def _outbox_add(self, user_id, kind, payload):
        try:
            row = DiscordOutbox(user_id=user_id, kind=kind, payload=json.dumps(payload), attempts=0)
            self.db.add(row)
            self._commit()
            return row.id
        except Exception:
            logger.exception("写入 Discord 待办失败")
            return None

    def _outbox_done(self, outbox_id):
        if outbox_id is None:
            return
        self.db.query(DiscordOutbox).filter(DiscordOutbox.id == outbox_id).delete(synchronize_session="fetch")
        self._commit()

    def _outbox_attempt(self, outbox_id):
        row = self.db.get(DiscordOutbox, outbox_id) if outbox_id is not None else None
        if row is None:
            return 0
        row.attempts = (row.attempts or 0) + 1
        self._commit()
        return row.attempts

    def _submit_outbox(self, user_id, outbox_id, factory, what):
        """执行持久化的任务：成功或永久失败才删除记录；暂时性失败先在队列里重试，仍不行就留给定时补发"""
        generation = self._generation[user_id]
        if outbox_id is not None:
            self._outbox_inflight.add(outbox_id)

        async def job():
            try:
                await self.ready.wait()
                if self.failed:
                    # Discord 同步不可用（配置错误等），不再保留，避免下次启动集中补发
                    self._outbox_done(outbox_id)
                    return None
                _job_ctx.set((user_id, generation))
                for attempt in range(len(INLINE_RETRY_DELAYS) + 1):
                    if self._generation[user_id] != generation:
                        self._outbox_done(outbox_id)
                        return None
                    try:
                        result = await factory()
                    except ConversationCleared:
                        self._outbox_done(outbox_id)
                        return None
                    except asyncio.CancelledError:
                        raise  # 关闭程序：保留记录，下次启动补发
                    except Exception as e:
                        self._rollback()
                        if not is_transient(e):
                            self._outbox_done(outbox_id)
                            raise
                        attempts = self._outbox_attempt(outbox_id)
                        if attempts >= MAX_OUTBOX_ATTEMPTS:
                            logger.error(f"同步任务多次失败，放弃 [{what}] user={user_id}：{e}")
                            self._outbox_done(outbox_id)
                            return None
                        if attempt == len(INLINE_RETRY_DELAYS):
                            logger.warning(f"同步任务暂时失败，稍后补发 [{what}] user={user_id}：{e}")
                            return None
                        logger.warning(f"同步任务暂时失败，{INLINE_RETRY_DELAYS[attempt]}s 后重试 [{what}]：{e}")
                        await asyncio.sleep(INLINE_RETRY_DELAYS[attempt])
                        continue
                    self._outbox_done(outbox_id)
                    return result
            finally:
                self._outbox_inflight.discard(outbox_id)

        return self.runner.submit(user_id, job, what)

    def _replay_outbox(self):
        rows = (
            self.db.query(DiscordOutbox)
            .filter(DiscordOutbox.id.notin_(self._outbox_inflight or [0]))
            .order_by(DiscordOutbox.id)
            .all()
        )
        if rows:
            logger.info(f"补发未完成的 Discord 同步任务 {len(rows)} 个")
        for row in rows:
            payload = json.loads(row.payload)
            user_id, kind = row.user_id, row.kind
            self._submit_outbox(
                user_id, row.id, lambda u=user_id, k=kind, p=payload: self._replay_job(u, k, p), f"replay {kind}"
            )

    async def _outbox_retry_loop(self):
        while True:
            await asyncio.sleep(OUTBOX_RETRY_INTERVAL)
            try:
                self._replay_outbox()
            except Exception:
                logger.exception("定时补发失败")

    async def _author_allowed(self, message):
        """补发时重新确认发送者当前仍有权限"""
        author = message.author
        if getattr(author, "bot", False) or getattr(message, "webhook_id", None):
            return False
        if not self.admin_role_ids:
            return True
        if isinstance(author, discord.Member):
            return self._is_admin(author)
        guild = self.forum.guild
        try:
            member = guild.get_member(author.id) or await guild.fetch_member(author.id)
        except discord.NotFound:
            return False
        return self._is_admin(member)

    async def _fetch_dc_message(self, thread_id, message_id):
        """返回消息；消息或帖子已不存在返回 None；其它错误抛出（暂时性的会重试）"""
        try:
            return await self.client.get_partial_messageable(thread_id).fetch_message(message_id)
        except discord.NotFound:
            return None

    async def _replay_job(self, user_id, kind, payload):
        if kind == "tg":
            if not self.history:
                logger.warning("有未同步到 Discord 的 TG 消息，但未配置 TG_API_ID / TG_API_HASH，无法补发")
                return
            await self.history.replay(user_id, payload["origin"], [tuple(p) for p in payload["pairs"]])
        elif kind == "tg_edit":
            if self.history:
                await self.history.replay_admin_edit(user_id, payload["group_msg_id"])
        elif kind in ("dc", "dc_edit"):
            message = await self._fetch_dc_message(payload["thread_id"], payload["message_id"])
            if message is None:
                logger.info("补发时 Discord 消息已不存在，跳过")
                return
            if not await self._author_allowed(message):
                logger.warning(f"补发时发送者已无权限，跳过 Discord 消息 {message.id}")
                return
            if kind == "dc":
                await self._dc_to_tg(user_id, message)
            else:
                await self._dc_edit_job(user_id, message, str(message.edited_at))
        elif kind == "dc_delete":
            await self._dc_delete_job(user_id, payload["message_ids"])

    async def _ensure_history_cutoff(self):
        """记录启用 Discord 同步时 TG 后台群的最新消息 ID：此前为历史（迁移），此后为实时同步"""
        row = self.db.get(BridgeState, "history_cutoff")
        if row:
            self.history_cutoff = int(row.value)
            return
        try:
            msg = await self.bot.send_message(
                admin_group_id,
                "🔗 已启用 Discord 同步。\n"
                "此前的历史消息会在用户的 Discord 帖子首次创建时自动迁移（需要配置 TG_API_ID / TG_API_HASH），"
                "也可以发送 /dc_migrate 一次性迁移全部历史。",
                message_thread_id=general_topic_id or None,
            )
            cutoff = msg.message_id
        except Exception as e:
            logger.warning(f"发送 Discord 启用提示失败，改用消息映射估算历史边界：{e}")
            last = self.db.query(MessageMap).order_by(MessageMap.group_chat_message_id.desc()).first()
            cutoff = (last.group_chat_message_id or 0) if last else 0
        self.history_cutoff = cutoff
        self.db.add(BridgeState(key="history_cutoff", value=str(cutoff)))
        self._commit()

    # ---------- 帖子 / webhook ----------

    async def _ensure_webhook(self, force=False):
        if self.webhook and not force:
            return self.webhook
        for hook in await self.forum.webhooks():
            if hook.user and hook.user.id == self.client.user.id and hook.name == WEBHOOK_NAME and hook.token:
                self.webhook = hook
                return hook
        self.webhook = await self.forum.create_webhook(name=WEBHOOK_NAME)
        return self.webhook

    def _thread_row(self, user_id):
        return self.db.query(DiscordThread).filter(DiscordThread.user_id == user_id).first()

    def _user_by_thread(self, thread_id):
        if not thread_id:
            return None
        row = self.db.query(DiscordThread).filter(DiscordThread.thread_id == thread_id).first()
        return row.user_id if row else None

    def _user_name(self, user_id, tg_user=None):
        if tg_user:
            return tg_user.full_name
        u = self.db.query(User).filter(User.user_id == user_id).first()
        if u:
            name = " ".join(x for x in (u.first_name, u.last_name) if x)
            if name:
                return name
        return str(user_id)

    async def _tg_avatar(self, user_id):
        try:
            photos = await self.bot.get_user_profile_photos(user_id, limit=1)
            if not photos.total_count:
                return None
            f = await self.bot.get_file(photos.photos[0][-1].file_id)
            return bytes(await f.download_as_bytearray())
        except Exception as e:
            logger.debug(f"获取用户头像失败 {user_id}: {e}")
            return None

    async def _create_thread(self, user_id, tg_user=None):
        name = self._user_name(user_id, tg_user)
        u = self.db.query(User).filter(User.user_id == user_id).first()
        username = tg_user.username if tg_user else (u.username if u else None)

        embed = discord.Embed(title=name[:256], color=0x229ED9)
        embed.add_field(name="用户 ID", value=f"`{user_id}`")
        embed.add_field(
            name="用户名",
            value=f"[@{escape_md(username)}](https://t.me/{username})" if username else "无",
        )
        if tg_user is not None:
            embed.add_field(name="会员", value="🏆 Premium" if tg_user.is_premium else "✈️ 普通会员")
        embed.set_footer(text="Telegram 私聊 · 在本帖内发言即可回复用户；锁定帖子 = 关闭对话；/clear 删除会话")
        kwargs = {}
        avatar = await self._tg_avatar(user_id)
        if avatar:
            kwargs["file"] = discord.File(io.BytesIO(avatar), filename="avatar.jpg")
            embed.set_thumbnail(url="attachment://avatar.jpg")
        created = await self.forum.create_thread(
            name=f"{name}|{user_id}"[:100],
            embed=embed,
            allowed_mentions=NO_MENTIONS,
            **kwargs,
        )
        return created.thread.id

    def _user_has_history(self, user_id):
        if not self.history_cutoff:
            return False
        u = self.db.query(User).filter(User.user_id == user_id).first()
        if u and u.message_thread_id and u.message_thread_id <= self.history_cutoff:
            return True
        return (
            self.db.query(MessageMap)
            .filter(
                MessageMap.user_id == user_id,
                MessageMap.group_chat_message_id <= self.history_cutoff,
            )
            .first()
            is not None
        )

    async def _ensure_thread(self, user_id, *, tg_user=None, announce=False, history=True):
        """返回用户对应的帖子 ID，不存在则创建；首次创建时迁移该用户的历史消息"""
        row = self._thread_row(user_id)
        if row and row.thread_id:
            return row.thread_id
        self._check_alive()
        thread_id = await self._create_thread(user_id, tg_user)
        if row is None:
            row = DiscordThread(user_id=user_id, thread_id=thread_id, history_migrated_upto=0, history_done=False)
        else:
            row.thread_id = thread_id
        row.lock_state = False
        row.migration_unlocked = False
        self.db.add(row)
        self._commit()

        if announce:
            await self._announce(user_id, thread_id, tg_user)
        if history and not row.history_done:
            if not self._user_has_history(user_id):
                row.history_done = True
                self._commit()
            elif self.history:
                try:
                    await self.history.migrate_user(user_id)
                except ConversationCleared:
                    raise
                except Exception:
                    self._rollback()
                    logger.exception(f"迁移用户 {user_id} 的历史消息失败，可稍后使用 /dc_migrate 重试")
            else:
                logger.info(f"用户 {user_id} 有历史消息，但未配置 TG_API_ID / TG_API_HASH，跳过迁移")
        # TG 侧对话是关闭状态时，新帖子也保持锁定
        if self._tg_closed(user_id):
            try:
                thread = await self._fetch_thread(thread_id)
                await self._apply_lock_to_discord(thread, True, note=False)
                row = self._thread_row(user_id)
                row.lock_state = True
                self._commit()
            except (discord.HTTPException, ThreadGone) as e:
                logger.warning(f"锁定新帖子失败：{e}")
                self._schedule_reconcile(user_id)
        return thread_id

    async def _announce(self, user_id, thread_id, tg_user=None):
        if not self.notify_channel_id:
            return
        channel = self.client.get_channel(self.notify_channel_id)
        if channel is None:
            return
        name = escape_md(self._user_name(user_id, tg_user))
        username = tg_user.username if tg_user else None
        extra = f"（[@{escape_md(username)}](<https://t.me/{username}>)）" if username else ""
        try:
            await channel.send(f"📨 新的用户 **{name}**{extra} 发起了私聊：<#{thread_id}>", allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            logger.warning(f"Discord 新用户通知发送失败：{e}")

    def _mark_thread_gone(self, thread_id):
        """帖子被删除：保留用户记录（迁移进度等），下次有消息时重建帖子"""
        row = self.db.query(DiscordThread).filter(DiscordThread.thread_id == thread_id).first()
        if row:
            row.thread_id = 0
            row.lock_state = None
            row.migration_unlocked = False
        self.db.query(DiscordMessageMap).filter(DiscordMessageMap.discord_thread_id == thread_id).delete(
            synchronize_session="fetch"
        )
        self._commit()

    async def _fetch_thread(self, thread_id, fresh=False) -> discord.Thread:
        if not fresh:
            thread = self.client.get_channel(thread_id)
            if isinstance(thread, discord.Thread):
                return thread
        try:
            return await self.client.fetch_channel(thread_id)
        except discord.NotFound:
            raise ThreadGone(thread_id)

    def _tg_closed(self, user_id):
        u = self.db.query(User).filter(User.user_id == user_id).first()
        return bool(u and u.message_thread_id and get_topic_status(self.db, u.message_thread_id) == "closed")

    @contextlib.asynccontextmanager
    async def migration_window(self, user_id):
        """迁移历史期间如帖子被锁定则临时解锁，结束后恢复。
        「临时解锁」记在数据库里：恢复成功前（包括崩溃、恢复失败），对账不会把它当成管理员解锁。
        迁移期间产生的锁定事件会排在迁移之后对账，那时状态已恢复"""
        thread_id = await self._ensure_thread(user_id, history=False)
        unlocked = False
        try:
            thread = await self._fetch_thread(thread_id, fresh=True)
            if thread.locked:
                row = self._thread_row(user_id)
                row.migration_unlocked = True
                self._commit()
                await thread.edit(locked=False, archived=False)
                unlocked = True
            elif thread.archived:
                await thread.edit(archived=False)
        except (discord.HTTPException, ThreadGone) as e:
            logger.warning(f"迁移前解锁帖子失败：{e}")
        try:
            yield thread_id
        finally:
            if unlocked:
                try:
                    thread = await self._fetch_thread(thread_id, fresh=True)
                    if not thread.locked:
                        await thread.edit(locked=True, archived=True)
                    row = self._thread_row(user_id)
                    row.migration_unlocked = False
                    self._commit()
                except (discord.HTTPException, ThreadGone) as e:
                    logger.warning(f"迁移后恢复帖子锁定失败，稍后重试：{e}")
                    self._schedule_reconcile(user_id)

    # ---------- 关闭状态对账 ----------

    def reconcile_lock(self, user_id):
        """TG 话题关闭/重开、Discord 帖子锁定/解锁后调用；同一用户排队中的对账只保留一个"""
        if not self.active or user_id in self._reconcile_pending:
            return
        self._reconcile_pending.add(user_id)

        async def job():
            self._reconcile_pending.discard(user_id)
            try:
                if await self._reconcile_lock_job(user_id):
                    self._reconcile_failures.pop(user_id, None)
            except Exception as e:
                if not is_transient(e):
                    raise
                logger.warning(f"对账暂时失败，稍后重试：{e}")
                self._schedule_reconcile(user_id)

        self._submit(user_id, job, "lock reconcile")

    def _schedule_reconcile(self, user_id):
        n = self._reconcile_failures[user_id] = self._reconcile_failures[user_id] + 1
        if n > 8:
            logger.error(f"用户 {user_id} 的关闭状态多次同步失败，等待下一次事件再试")
            self._reconcile_failures.pop(user_id, None)
            return
        self._later(min(600, 15 * 2**n), self.reconcile_lock, user_id)

    async def _startup_reconcile(self):
        """启动后核对离线期间的变化：只对看起来不一致的帖子发起对账"""
        try:
            locked = {t.id: t.locked for t in self.forum.threads}
            async for t in self.forum.archived_threads(limit=None):
                locked[t.id] = t.locked
        except Exception as e:
            logger.warning(f"启动对账时读取帖子列表失败：{e}")
            return
        count = 0
        for row in self.db.query(DiscordThread).filter(DiscordThread.thread_id != 0).all():
            state = locked.get(row.thread_id)
            if row.migration_unlocked or (
                state is not None and (state != row.lock_state or state != self._tg_closed(row.user_id))
            ):
                self.reconcile_lock(row.user_id)
                count += 1
        if count:
            logger.info(f"启动对账：{count} 个帖子需要同步关闭状态")

    async def _reconcile_lock_job(self, user_id):
        """返回 True 表示已一致或已同步成功"""
        row = self._thread_row(user_id)
        u = self.db.query(User).filter(User.user_id == user_id).first()
        tg_thread = u.message_thread_id if u else 0
        if not row or not row.thread_id or not tg_thread:
            return True
        try:
            thread = await self._fetch_thread(row.thread_id, fresh=True)
            if row.migration_unlocked:
                # 迁移时本程序临时解开的锁还没恢复：先恢复，不能当成管理员解锁
                if not thread.locked:
                    await thread.edit(locked=True, archived=True)
                    thread = await self._fetch_thread(row.thread_id, fresh=True)
                row.migration_unlocked = False
                self._commit()
        except ThreadGone:
            self._mark_thread_gone(row.thread_id)
            return True
        dc = bool(thread.locked)
        tg = get_topic_status(self.db, tg_thread) == "closed"
        synced = row.lock_state
        if dc == tg:
            if synced != dc:
                row.lock_state = dc
                self._commit()
            return True
        if synced is None:
            synced = dc  # 没有记录时以 TG 为准
        if tg != synced:
            # TG 侧变了（双方都变时也以 TG 为准）
            await self._apply_lock_to_discord(thread, tg)
            row.lock_state = tg
            self._commit()
            return True
        if await self._apply_lock_to_tg(user_id, tg_thread, dc, thread):
            row.lock_state = dc
            self._commit()
            return True
        self._schedule_reconcile(user_id)
        return False

    async def _apply_lock_to_discord(self, thread, closed, note=True):
        """先改状态再发提示；提示发送失败不影响状态同步"""
        if closed:
            if thread.archived:
                await thread.edit(archived=False)
            if note:
                with contextlib.suppress(discord.HTTPException):
                    await thread.send(
                        "🔒 管理员在 Telegram 关闭了对话，用户的新消息将不会被转发。", allowed_mentions=NO_MENTIONS
                    )
            await thread.edit(locked=True, archived=True)
        else:
            await thread.edit(archived=False, locked=False)
            if note:
                with contextlib.suppress(discord.HTTPException):
                    await thread.send("🔓 管理员在 Telegram 重新打开了对话。", allowed_mentions=NO_MENTIONS)

    async def _apply_lock_to_tg(self, user_id, tg_thread, closed, thread):
        """先调用 TG 接口，成功后才更新状态；失败时在帖子里提示并稍后重试"""
        try:
            if closed:
                await self.bot.close_forum_topic(admin_group_id, tg_thread)
            else:
                await self.bot.reopen_forum_topic(admin_group_id, tg_thread)
        except Exception as e:
            if not (isinstance(e, BadRequest) and "not_modified" in str(e).lower().replace(" ", "_")):
                logger.warning(f"同步 Discord 锁定状态到 TG 失败：{e}")
                if self._reconcile_failures[user_id] == 0:
                    with contextlib.suppress(discord.HTTPException):
                        await thread.send(f"⚠️ 无法同步到 Telegram，稍后自动重试：{e}", allowed_mentions=NO_MENTIONS)
                return False
        set_topic_status(self.db, tg_thread, "closed" if closed else "opened")
        with contextlib.suppress(Exception):
            await self.bot.send_message(
                admin_group_id,
                "🔒 对话已在 Discord 被关闭。" if closed else "🔓 对话已在 Discord 重新打开。",
                message_thread_id=tg_thread,
            )
        try:
            await self.bot.send_message(
                user_id,
                "管理员关闭了与您的对话,接下来您的留言将不会被转发给管理员,请知悉"
                if closed
                else "管理员重新打开了与您的对话,现在您发送的消息将会转发给管理员",
            )
        except Exception as e:
            logger.info(f"通知用户对话状态失败：{e}")
        return True

    async def _on_raw_thread_update(self, payload):
        if payload.parent_id != self.forum_channel_id:
            return
        if "locked" not in (payload.data.get("thread_metadata") or {}):
            return
        user_id = self._user_by_thread(payload.thread_id)
        if user_id:
            self.reconcile_lock(user_id)

    async def _on_raw_thread_delete(self, payload):
        """手动删除帖子只解除映射，不删除 TG 话题（需要删除整个会话请用 /clear）"""
        if payload.parent_id == self.forum_channel_id and self._user_by_thread(payload.thread_id):
            try:
                self._mark_thread_gone(payload.thread_id)
            except Exception:
                logger.exception("处理帖子删除失败")

    # ---------- 清除会话 ----------

    def delete_user_thread(self, user_id):
        """/clear：作废该用户所有未完成的同步任务，并删除 Discord 帖子"""
        if not self.active:
            return
        self._generation[user_id] += 1
        try:
            self.db.query(DiscordOutbox).filter(DiscordOutbox.user_id == user_id).delete(synchronize_session="fetch")
            self._commit()
        except Exception:
            logger.exception("清除待办失败")

        async def job():
            row = self._thread_row(user_id)
            if not row or not row.thread_id:
                return
            thread_id = row.thread_id
            try:
                thread = await self._fetch_thread(thread_id)
                await thread.delete()
            except ThreadGone:
                pass
            self._mark_thread_gone(thread_id)

        self._submit(user_id, job, "tg→dc clear")

    async def _on_clear_command(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.Thread) or channel.parent_id != self.forum_channel_id:
            return await interaction.response.send_message("请在用户的帖子里使用该命令。", ephemeral=True)
        if not self._is_admin(interaction.user):
            return await interaction.response.send_message("你没有权限执行此操作。", ephemeral=True)
        user_id = self._user_by_thread(channel.id)
        if not user_id or not self.on_clear:
            return await interaction.response.send_message("该帖子没有对应的用户。", ephemeral=True)
        await interaction.response.send_message("🗑️ 正在删除该会话…", ephemeral=True)
        try:
            await self.on_clear(user_id)
        except Exception as e:
            logger.exception("Discord /clear 失败")
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(f"❌ 删除失败：{e}", ephemeral=True)

    # ---------- 历史迁移 ----------

    def start_migration(self, status_chat_id, status_message_id, only_user_id=None):
        """返回错误提示；成功启动返回 None"""
        if not self.active:
            return "Discord 同步未正常运行，请检查日志。"
        if not self.history:
            return "未配置 TG_API_ID / TG_API_HASH，无法读取 Telegram 历史消息。"
        if self._migration_task and not self._migration_task.done():
            return "已经有一个迁移任务在进行中。"
        self._migration_task = asyncio.get_running_loop().create_task(
            self.history.run(status_chat_id, status_message_id, only_user_id)
        )
        return None
