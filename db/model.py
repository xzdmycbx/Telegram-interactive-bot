from sqlalchemy import Boolean, Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from .database import Base


class MediaGroupMesssage(Base):
    __tablename__ = "media_group_message"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer)
    message_id = Column(Integer)
    media_group_id = Column(Integer)
    is_header = Column(Boolean)
    caption_html = Column(String(1024 * 64))


class FormnStatus(Base):
    __tablename__ = "formn_status"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer)
    message_thread_id = Column(Integer)
    status = Column(String(64))


class MessageMap(Base):
    __tablename__ = "message_map"
    id = Column(Integer, primary_key=True, index=True)
    user_chat_message_id = Column(Integer)
    group_chat_message_id = Column(Integer)
    user_id = Column(Integer)


class User(Base):
    __tablename__ = "user"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, unique=True, index=True)
    first_name = Column(String(64))
    last_name = Column(String(64))
    username = Column(String(64))
    is_premium = Column(Boolean)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    message_thread_id = Column(Integer, default=0)


# ---------- Discord 同步 ----------


class DiscordThread(Base):
    """TG 用户 ↔ Discord 论坛帖子"""

    __tablename__ = "discord_thread"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, unique=True, index=True)
    # 0 = 帖子已被删除，下次有消息时重新创建
    thread_id = Column(Integer, index=True, default=0)
    # 已迁移到的最大 TG 群消息 ID（用于断点续迁）
    history_migrated_upto = Column(Integer, default=0)
    history_done = Column(Boolean, default=False)
    # 上一次两边一致时的锁定（关闭）状态，用于判断是哪一边发生了变化
    lock_state = Column(Boolean, nullable=True)
    # 迁移历史时被本程序临时解锁、尚未恢复（崩溃或恢复失败时据此补救，不能当成管理员解锁）
    migration_unlocked = Column(Boolean, default=False)


class DiscordPost(Base):
    """一次同步（可能包含多条源消息、多条目标消息）。

    TG → Discord：编辑时据此重建正文；Discord → TG：source = "dc:<Discord 消息 ID>"。
    stage：0 进行中，1 已发给用户（仅 Discord → TG），2 全部完成。
    进行中的记录说明上次发了一半，重试前会先撤回已发出的部分。
    """

    __tablename__ = "discord_post"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    thread_id = Column(Integer)
    username = Column(String(128))
    header = Column(Text, default="")
    footer = Column(Text, default="")
    source = Column(String(64), index=True, nullable=True)
    stage = Column(Integer, default=0)


class DiscordMessageMap(Base):
    """TG 消息 ↔ Discord 消息。

    TG → Discord：每条 TG 消息一行（text_role 为空，指向承载其附件的 Discord 消息），
    另外正文的每个分段一行（text_role = text，part 为分段序号）。
    Discord → TG：每条发出的 TG 消息一行，text_role 表示它承载文字的方式 text / caption。
    """

    __tablename__ = "discord_message_map"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    user_chat_message_id = Column(Integer, index=True, nullable=True)
    group_chat_message_id = Column(Integer, index=True, nullable=True)
    discord_message_id = Column(Integer, index=True)
    discord_thread_id = Column(Integer, index=True)
    part = Column(Integer, default=0)
    # user / tg_admin / dc_admin / history / system
    origin = Column(String(16))
    text_role = Column(String(8), nullable=True)
    post_id = Column(Integer, index=True, nullable=True)
    # 该 TG 消息渲染后的正文（媒体组编辑时重建用）
    body = Column(Text, nullable=True)
    # 该 Discord 正文分段是否带附件（带附件的分段不能删除，只能清空文字）
    has_files = Column(Boolean, default=False)


class DiscordOutbox(Base):
    """已入队但尚未完成的同步任务；重启后补发"""

    __tablename__ = "discord_outbox"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    # tg / tg_edit：TG → Discord；dc / dc_edit / dc_delete：Discord → TG
    kind = Column(String(16))
    payload = Column(Text)
    attempts = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class BridgeState(Base):
    __tablename__ = "bridge_state"
    key = Column(String(64), primary_key=True)
    value = Column(String(1024))
