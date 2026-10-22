# Telegram 双向机器人 · 完整使用教程

> 从零开始：创建机器人 → 配置后台群 → （可选）接入 Discord → 用 Docker 部署 → 日常使用 → 排错与维护。
> 只想用 Telegram 后台的话，跳过第 4、5 步即可。

## 目录

1. [工作原理](#1-工作原理)
2. [准备 Telegram 机器人和后台群](#2-准备-telegram-机器人和后台群)
3. [获取各种 ID](#3-获取各种-id)
4. [申请 Telegram API（api_id / api_hash）](#4-申请-telegram-apiapi_id--api_hash)
5. [准备 Discord（可选）](#5-准备-discord可选)
6. [填写配置 `.env`](#6-填写配置-env)
7. [用 Docker 部署运行](#7-用-docker-部署运行)
8. [首次启动检查](#8-首次启动检查)
9. [迁移历史记录到 Discord](#9-迁移历史记录到-discord)
10. [日常使用](#10-日常使用)
11. [消息类型与大文件](#11-消息类型与大文件)
12. [常见问题排查](#12-常见问题排查)
13. [备份、更新与停用](#13-备份更新与停用)

---

## 1. 工作原理

- 用户私聊机器人，机器人把消息转发到一个**开启了话题功能的 Telegram 群**（后台群），每个用户一个话题。管理员在话题里回复，机器人再把回复发给用户。
- 开启 Discord 同步后，同一份对话**同时**出现在 Discord 服务器的**论坛频道**里（每个用户一个帖子）。两边的管理员都可以回复用户，并能看到对方的回复。
- 用户始终只和机器人对话，看不到管理员是谁，也看不到 Discord 的存在。

```
用户私聊 ──► 机器人 ──► TG 后台群（话题）
                   └──► Discord 论坛（帖子）
TG 管理员回复 / Discord 管理员回复 ──► 机器人 ──► 用户（并互相镜像）
```

---

## 2. 准备 Telegram 机器人和后台群

### 2.1 创建机器人

1. 在 Telegram 里搜索 **@BotFather**，发送 `/newbot`。
2. 按提示输入机器人的显示名称，再输入用户名（必须以 `bot` 结尾，例如 `my_support_bot`）。
3. BotFather 会返回一串 **Token**（形如 `7126xxxxxx:AAH...`），这就是 `BOT_TOKEN`，**不要泄露**。

### 2.2 创建后台群

1. 新建一个群组（建议**私有群**，因为里面是用户的私聊内容）。
2. 群设置 → 打开 **话题（Topics）**。打开后群会自动变成超级群组。
3. 把机器人拉进群，并**设为管理员**，至少勾选：
   - **删除消息**（Delete messages）
   - **管理话题**（Manage topics）
4. 把需要处理客服消息的同事也拉进群。

---

## 3. 获取各种 ID

在 Telegram 里搜索 **@GetTheirIDBot**（或 `@userinfobot` 等同类机器人）：

| 需要的 ID | 获取方法 | 填到 |
| --- | --- | --- |
| 后台群 ID | 把 @GetTheirIDBot 拉进后台群，它会告诉你群 ID（以 `-100` 开头），获取后可以把它移出群 | `ADMIN_GROUP_ID` |
| 管理员用户 ID | 私聊 @GetTheirIDBot，它会返回你的用户 ID；其他管理员各自获取 | `ADMIN_USER_IDS`（多个用英文逗号分隔） |
| （可选）新用户通知话题 ID | 在后台群里新建一个话题（如「新用户通知」），在话题里右键任意消息 → 复制链接，链接形如 `https://t.me/c/1234567890/5/6`，中间的 `5` 就是话题 ID | `GENERAL_TOPIC_ID` |

> `ADMIN_USER_IDS` 里的人才能使用 `/clear`、`/broadcast`、`/dc_migrate` 等管理命令。

---

## 4. 申请 Telegram API（api_id / api_hash）

**为什么需要**：Telegram 给机器人的普通接口（Bot API）读不到历史消息，下载文件上限 20MB，上传上限 50MB。配置 api_id / api_hash 后，程序会**以机器人自己的身份**登录 Telegram 的底层协议（MTProto），用来：

- 把后台群里已有的历史话题迁移到 Discord；
- 同步超过 20MB 的文件到 Discord；
- 把 Discord 上超过 50MB（最大 2GB）的文件发给用户；
- 程序重启后补发没来得及同步的 Telegram 消息。

> 只用 Telegram 后台（不接 Discord）不需要申请。接 Discord 的话**强烈建议**申请，并且最好在**第一次启动前**就填好。

### 申请步骤

1. 用浏览器打开 **https://my.telegram.org**。
2. 输入你的 Telegram 手机号（带国家区号，例如 `+8613800000000`），点 **Next**。
   - 任何一个 Telegram 账号都可以，不需要是机器人的创建者。
3. 验证码**不会走短信**，而是发到你的 **Telegram App** 里（由官方账号「Telegram」发送）。把验证码填进网页，点 **Sign In**。
4. 登录后点 **API development tools**。
5. 填写表单：

   | 字段 | 怎么填 |
   | --- | --- |
   | App title | 随意，例如 `support bridge` |
   | Short name | 5~32 位英文字母或数字，例如 `supportbridge` |
   | URL | 可留空 |
   | Platform | 选 `Other`（或 `Desktop`） |
   | Description | 可留空 |

6. 点 **Create application**。页面会显示：
   - **App api_id**：一串数字 → 填到 `TG_API_ID`
   - **App api_hash**：32 位字母数字 → 填到 `TG_API_HASH`

### 申请时报错怎么办

点 Create 后只弹出一个 **ERROR**，是最常见的问题，通常和网络环境有关：

- 换一个代理节点或线路再试（机房 IP 容易被拒），或者用手机流量开热点；
- 关闭广告拦截、隐私类浏览器插件，或换无痕窗口、换浏览器；
- 所有字段只用英文字母和数字，不要用中文和符号；
- 刚注册的新号偶尔会被拒，过一段时间再试。

### 安全提示

- api_hash 等同密码，不要公开。一个账号只能申请一个应用，之后在同一页面可以随时查看。
- 程序只用它登录**机器人账号**，不会登录、也不会读取你的个人账号。
- 登录后会在 `assets/` 目录生成 `应用名-mtproto.session` 文件，它相当于机器人的登录凭证，同样不要泄露。

---

## 5. 准备 Discord（可选）

### 5.1 创建 Discord 机器人

1. 打开 **https://discord.com/developers/applications**，点右上角 **New Application**，起个名字，创建。
2. 左侧 **General Information** 页记下 **Application ID**（后面生成邀请链接要用）。
3. 左侧 **Bot** 页：
   - 点 **Reset Token** → 复制 Token（只显示一次），填到 `DISCORD_BOT_TOKEN`；
   - 在 **Privileged Gateway Intents** 里打开 **MESSAGE CONTENT INTENT**，点保存（**必须开**，否则读不到管理员的回复内容）；
   - 建议关闭 **Public Bot**，防止别人把你的机器人拉进他们的服务器。

### 5.2 创建服务器和论坛频道

1. 在 Discord 客户端左侧点 **+** → 亲自创建 → 仅供我和我的朋友使用，建一个服务器（建议专用、私密）。
2. 创建频道 → 选择 **论坛（Forum）**，例如命名为 `私聊`。
   - 如果看不到「论坛」选项：服务器设置 → **启用社区**，按向导开启后就有了。
3. 论坛频道设置里**不要**开启「发帖时必须选择标签」（否则机器人建帖会失败）。
4. 强烈建议把这个论坛频道设为**仅客服角色可见**（频道设置 → 权限），因为里面是用户的私聊内容。
5. （可选）再建一个普通文字频道，用来接收「新用户发起私聊」的通知。

### 5.3 邀请机器人进服务器

**方法一：直接用链接**（把 `应用ID` 换成 5.1 里记下的 Application ID）：

```
https://discord.com/oauth2/authorize?client_id=应用ID&scope=bot+applications.commands&permissions=326954503232
```

**方法二：开发者后台生成**：左侧 **OAuth2** → **OAuth2 URL Generator**

- SCOPES 勾选：`bot`、`applications.commands`（后者是 Discord 里的 `/clear` 命令需要的）
- BOT PERMISSIONS 勾选：
  - View Channels（查看频道）
  - Send Messages（发送消息；在论坛里即「发帖」）
  - Send Messages in Threads（在帖子中发送消息）
  - Create Public Threads（创建公开帖子）
  - Manage Threads（管理帖子：锁定、归档、删除）
  - Manage Webhooks（管理 Webhook：用来以用户名义显示消息）
  - Attach Files（附加文件）
  - Embed Links（嵌入链接）
  - Read Message History（读取消息历史）
  - Add Reactions（添加表情回应）

复制页面底部生成的链接，在浏览器打开，选择你的服务器并授权。

> 如果论坛频道设置了「仅某角色可见」，记得在该频道的权限里给机器人（或它的角色）也加上上述权限。

### 5.4 获取 Discord 的 ID

1. 用户设置 → **高级** → 打开 **开发者模式**。
2. 右键论坛频道 → **复制频道 ID** → 填到 `DISCORD_FORUM_CHANNEL_ID`。
3. （可选）右键通知用的文字频道 → 复制频道 ID → 填到 `DISCORD_NOTIFY_CHANNEL_ID`。
4. （可选）服务器设置 → 角色 → 右键客服角色 → **复制身份组 ID** → 填到 `DISCORD_ADMIN_ROLE_IDS`（多个用逗号分隔）。
   - 填了以后，只有这些角色（以及服务器管理员）在帖子里的发言会发给用户；
   - 不填则「能在帖子里发言的人」都可以回复用户，此时请用频道权限控制可见范围。

---

## 6. 填写配置 `.env`

把项目根目录的 `.env_example` 复制为 `.env`，按下面的说明填写。`#` 开头的行是注释，去掉 `#` 才生效；**注释请单独写一行，不要写在值的后面**。

```ini
# ===== 基本配置 =====
# 随意，用于数据文件命名
APP_NAME=interactive-bot
# 第 2.1 步
BOT_TOKEN=7126xxxxxx:AAH...

# ===== 业务配置 =====
# 用户发送 /start 时的欢迎语，\n 表示换行
WELCOME_MESSAGE="你好，请问有什么可以帮你？"
# 第 3 步：后台群 ID
ADMIN_GROUP_ID=-1001234567890
# 第 3 步：管理员 ID，多个用英文逗号分隔
ADMIN_USER_IDS=11111111,22222222
# TRUE：删除话题即永久拉黑该用户
DELETE_TOPIC_AS_FOREVER_BAN=FALSE
# TRUE：/clear 时同时删除用户那边的聊天记录
DELETE_USER_MESSAGE_ON_CLEAR_CMD=TRUE
# TRUE：关闭新用户人机验证
DISABLE_CAPTCHA=FALSE
# 用户两条消息的最小间隔（秒），0 不限制
MESSAGE_INTERVAL=3
# 新用户通知发到哪个话题，留空发到 General
# GENERAL_TOPIC_ID=5

# ===== Discord 同步（可选，不填 DISCORD_BOT_TOKEN 则不启用）=====
# 第 5.1 步
DISCORD_BOT_TOKEN=MTE...
# 第 5.4 步
DISCORD_FORUM_CHANNEL_ID=123456789012345678
# DISCORD_NOTIFY_CHANNEL_ID=
# DISCORD_ADMIN_ROLE_IDS=

# ===== Telegram API（推荐，第 4 步）=====
TG_API_ID=1234567
TG_API_HASH=0123456789abcdef0123456789abcdef

# ===== 代理（国内服务器 / 本地才需要，见第 7.4 步）=====
# HTTPS_PROXY=http://host.docker.internal:7890
```

> `.env` 里有各种密钥，**不要提交到 Git、不要发给别人**（项目已在 `.gitignore` 和 `.dockerignore` 里排除它）。

---

## 7. 用 Docker 部署运行

服务器和本地电脑都推荐用 Docker，命令完全一样。镜像里已内置 ffmpeg（贴纸转 GIF、视频压缩需要）。

### 7.1 安装 Docker

- **Windows / macOS**：安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，安装后启动它。
- **Linux 服务器**：
  ```bash
  curl -fsSL https://get.docker.com | sh
  ```

### 7.2 获取代码并启动

```bash
git clone https://github.com/xzdmycbx/Telegram-interactive-bot.git
cd Telegram-interactive-bot
cp .env_example .env        # Windows PowerShell 用：copy .env_example .env
# 编辑 .env，按第 6 步填写
docker compose up -d --build
```

### 7.3 常用命令

```bash
docker compose logs -f           # 查看实时日志（Ctrl+C 退出查看，不影响运行）
docker compose restart           # 重启
docker compose down              # 停止
docker compose up -d --build     # 修改代码或更新后重新构建并启动
```

### 7.4 需要代理时（国内网络）

Telegram 和 Discord 在国内都需要代理。在 `.env` 里加上：

```ini
HTTPS_PROXY=http://host.docker.internal:端口
```

- 端口填你代理软件的 **HTTP 代理端口**（Clash 一般是 `7890`，v2rayN 一般是 `10809`）。
- 只支持 **http** 代理（Discord 的库不支持 socks）。
- **Windows / macOS（Docker Desktop）** 一般直接可用；**Linux** 需要在代理软件里开启「允许局域网连接」（监听 `0.0.0.0`），容器才能访问到宿主机上的代理。
- 海外服务器不需要配置代理。

---

## 8. 首次启动检查

1. 看日志 `docker compose logs -f`，正常情况下会看到：
   - `Discord 已连接：xxx，论坛频道：#私聊`（启用了 Discord 时）
   - 没有 `ERROR` 字样的报错
2. 用**管理员账号**私聊机器人发送 `/start`，机器人回复「配置正确，后台群组：xxx」并列出可用命令，说明 Telegram 部分正常。
3. 启用 Discord 后第一次启动，机器人会在后台群发一条 **「🔗 已启用 Discord 同步」**。**这条消息之前的都算历史**（需要迁移），之后的走实时同步，两者不会重复。
4. 用另一个普通账号私聊机器人发一句话，检查：
   - 后台群出现了新话题；
   - Discord 论坛出现了新帖子（首条是用户资料卡）；
   - 在话题里或帖子里回复，用户都能收到，另一边也能看到。

---

## 9. 迁移历史记录到 Discord

前提：已经填好 `TG_API_ID` / `TG_API_HASH`。

- **自动迁移**：某个老用户的帖子第一次被创建时（他发来新消息，或管理员回复他），会先把他的历史记录搬过去，再继续实时消息，顺序不会乱。
- **一次性全部迁移**：在后台群的 **General**（或 `GENERAL_TOPIC_ID` 对应的话题）发送：
  ```
  /dc_migrate
  ```
  机器人会回复一条进度消息并持续更新：扫描历史 → 逐个用户迁移 → 完成汇总（含失败名单）。
- **只迁移某个用户**：在该用户的话题里发送 `/dc_migrate`。
- 中途中断（重启、报错）没关系，再发一次 `/dc_migrate` 会从断点继续，已迁移的不会重复。
- 迁移后的消息带原始时间（Discord 按查看者的时区显示），引用回复、相册、话题关闭记录都会保留；之前已关闭的对话，帖子会保持锁定。

---

## 10. 日常使用

### 10.1 Telegram 后台群

| 操作 | 效果 |
| --- | --- |
| 在用户话题里发消息 | 发给用户（Discord 帖子里显示为 `🛡️ 你的名字 · TG`） |
| 回复（引用）话题里的某条消息 | 用户那边也是引用回复 |
| 编辑自己发过的回复 | 用户和 Discord 那边同步修改 |
| 对消息点表情回应 | 同步到用户和 Discord |
| 关闭话题 / 重新打开话题 | 用户收到通知，之后的消息不再转发 / 恢复转发；Discord 帖子同步锁定 / 解锁 |
| 在话题里发送 `/clear` | 删除话题、Discord 帖子，以及（按配置）用户那边的聊天记录 |
| 回复一条消息并发送 `/broadcast` | 把被回复的消息广播给所有用户 |
| 在 General 发送 `/dc_migrate` | 迁移历史到 Discord（见第 9 步） |

### 10.2 Discord 论坛

| 操作 | 效果 |
| --- | --- |
| 在用户的帖子里发言 | 发给用户；TG 后台显示为 `💬 你的名字 · Discord` |
| 使用 Discord 的「回复」 | 用户那边是引用回复 |
| 发送图片 / 视频 / 文件 / 语音 / 贴纸 | 自动转换格式后发给用户（见第 11 步） |
| 编辑 / 删除自己发的消息 | 同步修改 / 删除用户和 TG 后台那边的消息（删除受 Telegram 48 小时限制） |
| 对消息点表情回应 | 同步到用户（仅限 Telegram 支持的表情） |
| 锁定帖子 / 解锁帖子 | 等同于在 TG 关闭 / 重新打开对话 |
| 在帖子里使用斜杠命令 `/clear` | 与 TG 的 `/clear` 相同 |
| 手动删除帖子 | 只解除关联，不影响 TG；用户再发消息会建新帖 |

- 帖子里显示的用户消息用的是用户的名字；TG 管理员的回复显示为 `🛡️ 名字 · TG`。
- 用户发来的繁体中文会自动转成简体（两边后台都是）。
- 对话处于关闭状态时，在帖子里发言不会发给用户，机器人会提示你先解锁。

---

## 11. 消息类型与大文件

### 11.1 格式和类型转换

| 类型 | Telegram → Discord | Discord → Telegram |
| --- | --- | --- |
| 文字格式（粗体、斜体、代码、引用、链接、剧透等） | 自动转换 | 自动转换（格式错乱时退回纯文本） |
| 图片 / 视频 / GIF / 文件 / 音频 / 语音 | 作为附件上传 | 按类型发送，多个附件合并成相册；webm/mkv 自动转 mp4 |
| 贴纸 | 动态贴纸、视频贴纸**转成 GIF 动图** | APNG / Lottie / GIF 贴纸**转成 GIF 动图** |
| 位置、联系人、投票、骰子、Story、游戏、账单、抽奖 | 显示为说明文字 | 投票显示为文字 |
| 转发的消息 | 显示「🔁 转发自 …」 | 取出原消息的文字、附件、卡片一起发送 |
| 回复 | 显示「↪️ 回复 某人：摘要 [↗]」，可点击跳转 | 原生引用回复 |

### 11.2 大文件

| 情况 | 处理方式 |
| --- | --- |
| Telegram 文件超过 20MB | 配置了 TG API 时自动下载；没配置则显示「📎 未同步」说明 |
| 超过 Discord 上传上限（普通服务器 10MB，加成后 50MB/100MB） | 视频自动压缩到上限以内；图片自动压缩；其它文件显示说明 |
| Discord 附件超过 50MB | 配置了 TG API 时直接发给用户（最大 2GB）；否则视频压缩到 50MB 以内，其它文件告诉用户「过大未能发送」，并在 Discord 里提示你 |
| 实在同步不了的文件 | 说明里附「在 Telegram 中查看」链接，后台群成员点开就能看到原文件 |

---

## 12. 常见问题排查

先看日志：`docker compose logs -f`。

| 现象 / 日志 | 原因与解决 |
| --- | --- |
| `BOT_TOKEN 未填写`、`应该是数字` | `.env` 没填好，或 ID 里混了空格、中文逗号 |
| 管理员 `/start` 提示「后台管理群组设置错误」 | 群 ID 不对，或机器人不在群里、不是管理员 |
| 用户消息没进后台群 | 群没开「话题」，或机器人缺少「管理话题」权限 |
| `Discord 登录失败` | `DISCORD_BOT_TOKEN` 错误或已重置，重新复制 |
| `缺少 Message Content Intent` | 开发者后台 Bot 页面打开 MESSAGE CONTENT INTENT |
| `必须是一个论坛（Forum）频道`、`找不到 Discord 论坛频道` | `DISCORD_FORUM_CHANNEL_ID` 复制错了，或复制成了服务器 ID |
| `权限不足` | 重新用第 5.3 步的链接授权，并检查论坛频道的权限覆盖设置 |
| Discord 里没有 `/clear` 命令 | 邀请时没勾 `applications.commands`，用第 5.3 步的链接重新授权一次；Discord 客户端按 Ctrl+R 刷新 |
| 新用户的帖子没建出来 | 论坛开启了「发帖必须选择标签」，关掉即可 |
| 管理员在 Discord 回复了，用户没收到 | 检查发言者是否属于 `DISCORD_ADMIN_ROLE_IDS`；对话是否被关闭（帖子是否锁定）；用户是否屏蔽了机器人（机器人会在帖子里提示） |
| 大文件显示「未同步」 | 配置 `TG_API_ID` / `TG_API_HASH`；超过 Discord 上限的非视频文件无法同步，点链接去 Telegram 查看 |
| my.telegram.org 创建应用提示 ERROR | 见第 4 步「申请时报错怎么办」 |
| `/dc_migrate` 提示未配置 | 填好 `TG_API_ID` / `TG_API_HASH` 后重启 |
| 迁移报告里有失败用户 | 看日志里的原因，修好后再发一次 `/dc_migrate`，会从断点继续 |
| 国内网络连不上 | 配置第 7.4 步的代理；Linux 记得开代理软件的「允许局域网连接」 |

> Discord 暂时连不上不会影响 Telegram 后台的正常使用。期间的同步任务会保存下来，恢复后自动补发。

---

## 13. 备份、更新与停用

### 备份

所有数据都在项目的 `assets/` 目录（Docker 运行时映射到宿主机）：

| 文件 | 内容 |
| --- | --- |
| `db.sqlite3` | 用户、话题、消息对应关系、Discord 同步记录 |
| `应用名.pickle` | 人机验证等会话状态 |
| `应用名-mtproto.session` | 机器人的 MTProto 登录凭证 |

定期把 `assets/` 和 `.env` 复制到安全的地方即可。迁移服务器时带上这两样，就能无缝继续。

### 更新

```bash
git pull
docker compose up -d --build
```

停止时程序会先把正在排队的同步任务做完（最多等约 20 秒），做不完的会在启动后自动补发。

### 停用 Discord 同步

在 `.env` 里删掉（或注释掉）`DISCORD_BOT_TOKEN`，然后 `docker compose up -d`。Telegram 后台照常工作；以后再填回来，会接着之前的帖子继续同步，但停用期间的消息不会补到 Discord。
