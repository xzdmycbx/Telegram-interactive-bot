# Telegram interactive bot (Telegram 双向机器人)

## 一、简介
Telegram的开源双向机器人。避免垃圾信息；让被限制的客户可以顺利联系到你。
支持后台多客服。在后台群组，可以安排多个客服以同一个机器人身份持续和客户沟通。

[English](https://github.com/MiHaKun/Telegram-interactive-bot/blob/master/README.en.md) | [示例机器人](https://t.me/CustomerConnectBot) | [示例后台](https://t.me/MiHaCMSGroup)

（ps：和示例机器人说话后，去后台看看，就大概知道原理了）

> 📖 **[完整使用教程](doc/tutorial.md)**：从创建机器人、申请 Telegram API、接入 Discord 到 Docker 部署、日常使用和排错，一步步讲清楚。

（ps：示例后台是公开群组，方便大家看效果而已。自己部署，最好用私有群组，安全性还是没问题的）

![image](https://github.com/user-attachments/assets/69dd3cd2-69e5-4925-9b9d-63b8ee8d2139)


### 特色
- 当客户通过机器人联系客服时，所有消息将被完整转发到后台管理群组，生成一个独立的以客户信息命名子论坛，用来和其他客户区分开来。
- 客服在子论坛中的回复，可以直接回复给客户。
- 客服可以通过关闭/开启子论坛来配置是否继续和客户对话。
- 提供永久封禁方案。env内有开关。
- 提供 /clear 命令，可以清除子论坛内的所有消息，同时也删除用户消息（极其不推荐如此使用，不过奈何也确实有时候有必要）。env内有开关。

### 优势
- 借助子论坛，可以增加多个管理成员，分担客服压力。
- 可以直观的保留和客户沟通的完整通讯记录。
- 可以得知某句话是哪个客服回复的，维系连贯的客户服务。


## 二、准备工作
本机器人的主要原理是将客户和机器人的对话，转发到一个群内（自用，最好是私有群），并归纳每个客户的消息到一个子版块。
所以，在开工前，你需要：
1. 找 @BotFather 申请一个机器人。

2. 获取机器人的token

3. 建立一个群组（按需设置是否公开）

4. 群组的“话题功能”打开。

5. 将自己的机器人，拉入群组。提升权限为管理员。

6. 管理权限切记包含`消息管理`，`话题管理`。

7. 通过机器人 @GetTheirIDBot 获取群组的内置ID和管理员用户ID。

   ![image-20240703082929589](./doc/cn/image-20240703082929589.png)![image-20240703083040852](./doc/cn/image-20240703083040852.png)

## 三、部署运行

### 1. 修改env
打开`.env_example`，将自己机器人的Token、账号的API_ID/HASH、管理群组ID和管理员ID补全。
另存`.env_example`为`.env`

### 2. 获取代码/构建python venv
```
git clone https://github.com/MiHaKun/Telegram-interactive-bot.git
cd Telegram-interactive-bot
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
```
### 3. 执行

#### 3.1 普通执行
```
python -m interactive-bot
```

**PS:** 正式运营，还是需要类似`PM2`、`supervisor`之类的进程管理工具，配合看门狗来实现不间断运行、自动重启、失效重启等功能。 

#### 3.2 docker 执行

1. 安装 Docker，参看 [Install Docker under Ubuntu 22.04](https://gist.github.com/dehsilvadeveloper/c3bdf0f4cdcc5c177e2fe9be671820c7)
2. 复制 `.env_example` 为 `.env` 并填写配置（见步骤一）
3. 启动服务：

```bash
docker compose up -d --build
```

**数据持久化说明：** 数据库（`assets/db.sqlite3`）、会话状态（`assets/*.pickle`）以及历史迁移用的 Telethon 会话（`assets/*-mtproto.session`）均挂载到宿主机的 `./assets` 目录，容器重建后数据不丢失。

```bash
# 查看日志
docker compose logs -f

# 停止服务
docker compose down

# 重新构建并启动（更新代码后）
docker compose up -d --build
```

本地调试也建议直接用 Docker 跑（同一套命令）。国内网络需要代理时，在 `.env` 里加上 `HTTPS_PROXY=http://host.docker.internal:端口`，容器会通过宿主机的代理访问 Telegram 和 Discord（Discord 只支持 http 代理）。

## 四、同步到 Discord（可选）

> 详细步骤（含 Discord 机器人创建、权限、my.telegram.org 申请 API）见 [完整使用教程](doc/tutorial.md)。

开启后，所有私聊会**同时**出现在 TG 后台群和 Discord 服务器里，两边的管理员都可以回复用户，互相能看到对方的回复。

| Telegram 后台群 | Discord |
| --- | --- |
| 每个用户一个话题 | 论坛（Forum）频道里每个用户一个帖子，首条为用户资料卡 |
| 用户 / 管理员的消息 | 通过 webhook 以对应昵称显示（TG 管理员显示为 `🛡️ 名字 · TG`） |
| 在话题里回复 | 在帖子里直接发言即回复用户；TG 后台会同步显示 `💬 名字 · Discord` |
| 关闭 / 重新打开话题 | 锁定 / 解锁帖子（双向同步） |
| `/clear` 删除会话 | 在帖子里用斜杠命令 `/clear`，效果与 TG 的 `/clear` 相同（删除 TG 话题与帖子）；直接删除帖子只解除关联，不影响 TG |

- 同一用户的消息严格按顺序同步；Discord 出问题不影响 TG 原有功能。
- 排队中的同步任务（消息、编辑、删除）会先记录到数据库：网络抖动等暂时性失败自动退避重试，仍失败的每 10 分钟及下次启动时继续补发（补发 TG 消息需要配置下面的 `TG_API_ID`）；发了一半的会先撤回再整条重发，不会留下残缺内容。
- 关闭状态会在启动时主动核对一次（覆盖程序离线期间的改动），同步到 TG 失败会自动重试。
- 发往 Discord 的消息会自动控制速度，尽量不触发 Discord 的限流。
- 头像：有公开用户名的用户自动使用 Telegram 公开头像；配置 `AVATAR_PUBLIC_URL` 后所有用户都显示真实头像（见教程第 7.5 步）。
- 想清空 Discord 重新迁移：在后台群发送 `/dc_reset confirm`，不需要动数据库（见教程第 9 步）。
- 执行 `/clear` 后，该用户还没完成的同步任务全部作废，不会在清除后再冒出消息。

### 1. 准备 Discord
1. 在 [Discord 开发者后台](https://discord.com/developers/applications) 新建应用 → Bot，复制 Token；在 Bot 页面开启 **Message Content Intent**。
2. 新建一个服务器（建议私有），在服务器里创建一个**论坛频道**（不要开启「发帖必须选择标签」）。
3. 用 OAuth2 → URL Generator 生成邀请链接（scope 选 `bot` 和 `applications.commands`），权限至少勾选：查看频道、发送消息、在帖子中发送消息、创建公开帖子、管理帖子、管理 Webhook、附加文件、嵌入链接、读取消息历史、添加反应。
4. 打开 Discord 的开发者模式（用户设置 → 高级），右键论坛频道「复制频道 ID」。

### 2. 配置 `.env`
```
DISCORD_BOT_TOKEN=xxxx
DISCORD_FORUM_CHANNEL_ID=论坛频道ID
# 可选：新用户通知发到哪个文字频道
DISCORD_NOTIFY_CHANNEL_ID=
# 可选：只有这些角色（和服务器管理员）的发言会转发给用户；留空 = 能在帖子里发言的人都可以
DISCORD_ADMIN_ROLE_IDS=
```

### 3. 迁移历史话题与大文件（推荐配置）
Bot API 读不到历史消息，也只能下载 20MB、上传 50MB 以内的文件。配置后，程序会以**机器人自己的身份**登录 MTProto（不需要登录任何用户账号），用于迁移历史、收发大文件（最大 2GB）和重启补发。到 <https://my.telegram.org> 申请 `api_id` / `api_hash` 填入：
```
TG_API_ID=123456
TG_API_HASH=xxxxxxxx
```
- 首次启用 Discord 同步时，机器人会在后台群发一条「已启用 Discord 同步」的提示，这条消息之前的都算历史，之后的走实时同步，两者不会重复。
- **自动迁移**：某个老用户的帖子第一次被创建时（他发来新消息或管理员回复他），会先把他的历史记录搬过去，再继续实时消息，顺序不会乱。
- **一次性迁移**：在后台群 General 发送 `/dc_migrate` 迁移全部用户；在某个用户的话题里发送则只迁移该用户。进度会实时更新，中断后再执行会从断点继续。
- 迁移后的消息带原始时间（Discord 会按查看者的时区显示），引用回复、相册、话题关闭记录都会保留。

### 4. 消息类型转换
| 类型 | Telegram → Discord | Discord → Telegram |
| --- | --- | --- |
| 文字格式 | 粗体/斜体/下划线/删除线/剧透/代码/代码块/引用/链接 → Markdown | Markdown → HTML（标题转粗体、列表转「•」）；格式错乱时自动退回纯文本 |
| 提及 / 表情 | 自定义表情显示为对应的普通 emoji | `@用户`/`#频道`/`@角色`/自定义表情/时间戳 转成可读文字 |
| 图片 / 视频 / GIF / 文件 / 音频 / 语音 | 下载后上传为附件，剧透标记保留 | 按类型发送；多个附件合并为媒体组；大于 10MB 的图片改为文件；webm/mkv 等转码为 mp4 |
| 相册（媒体组） | 合并成一条带多附件的消息（超过大小上限时自动分成多条） | — |
| 贴纸 | 动态贴纸（.tgs）和视频贴纸（.webm）**转成 GIF 动图**；静态贴纸直接上传 | 静态贴纸转图片；APNG / Lottie / GIF 贴纸**转成 GIF 动图** |
| 位置 / 场所 / 联系人 / 投票 / 骰子 | 渲染为文字（位置附地图链接） | 投票渲染为文字 |
| Story / 游戏 / 账单 / 抽奖 / 付费媒体 | 渲染为说明文字（公开 Story 附链接） | — |
| 转发的消息 | 显示「🔁 转发自 …」 | Discord 的「转发」会取出原消息的文字、附件和卡片（embed）一起发送 |
| 回复 | webhook 无法原生回复，显示「↪️ 回复 某人：摘要 [↗]」并可跳转 | 原生回复对应的 TG 消息 |
| 编辑 | 用户编辑：贴出新内容（与 TG 后台一致）；TG 管理员编辑：直接改 Discord 消息 | 同步修改 TG 用户和后台的消息 |
| 删除 | — （Bot API 收不到删除事件） | 删除 Discord 管理员发出的消息时，同步删除 TG 侧（受 48 小时限制） |
| 表情回应 | 同步添加/移除 | 仅 Telegram 支持的表情会同步 |
| 用户发来的繁体 | 转为简体（与 TG 后台一致） | — |

### 5. 大文件怎么处理
| 情况 | 处理方式 |
| --- | --- |
| TG 文件 > 20MB | 配置了 `TG_API_ID` 时用 MTProto 下载；否则显示「📎 未同步」说明 |
| 超过 Discord 上传上限（未加成服务器 10MB） | 视频 / GIF / 视频消息用 ffmpeg **压缩到上限以内**（按时长计算码率，太长压不下时放弃）；图片降质量缩小；其它文件显示说明 |
| Discord 附件 > 50MB | 配置了 `TG_API_ID` 时用 MTProto 发给用户（最大 2GB）；否则视频压缩到 50MB 以内，其它文件告知用户「过大未能发送」，并在 Discord 里提示管理员 |
| 无法同步的文件 | 说明里附「在 Telegram 中查看」链接，后台群成员点开即可看到原文件 |

ffmpeg 已内置在 Docker 镜像里；不用 Docker 时需要自行安装 ffmpeg，否则贴纸转 GIF、压缩、转码会自动降级为上传原文件或显示说明。

# ToDoList
- [x] 同步到 Discord（含历史话题迁移）
- [x] 准备完善下，docker化
- [x] 支持消息回复功能。消息间可以相互引用。
- [x] 完善下数据库。
- [x] 添加客户的人机识别，防止无聊的人用userbot来刷
- [x] 添加并识别媒体组消息。
- [x] 精简点代码，利用**payload来展开forwarding的参数。

# 关于

- 本产品基于Apache协议开源。
- 作者 米哈( [@MrMiHa](https://t.me/MrMiHa) )是一个苦逼程序员，不是煤场奴工，有问题别太理直气壮的跑来下命令。
- 讨论群组是 : https://t.me/DeveloperTeamGroup 欢迎加入后玩耍
- 随意Fork，记得保留`关于`的内容。
- 初版写了2小时。喜欢请打赏。不会部署，群里找我。
- 服务器推荐RackNerd的。实际上，我也确实用这个。够便宜。这款就够：[2核3G--年32刀](https://my.racknerd.com/aff.php?aff=11705&pid=905) 
- 实在搞不定部署，可以群里找大家帮忙部署下。服务器也可以找大家共用： https://t.me/DeveloperTeamGroup 
- 实在实在实在搞不定部署，找  [@MrMiHa](https://t.me/MrMiHa)  同学付费部署……

# 详细部署文档

[米哈同学的知识库](https://miha.uk/docs/tutor/telegram-interactive-bot/)
