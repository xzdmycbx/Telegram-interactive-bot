# Telegram interactive bot (Telegram Bidirectional Bot)

## 0. Break, A friend of mine has developed a free platform of this kind of bot
### Key Features of the Hosted Version

- **Privacy-First Design** – Fully compliant with the Telegram API. Pure message forwarding only — no data is ever stored!
- **Anti-Spam Protection** – Smart abuse prevention to keep both customers and support agents safe from spam and unwanted messages.
- **Group Chat Integration** – Seamlessly forward chats to support groups for clear, organized message handling. Great for teams with multiple agents.
- **Lightning-Fast Response** – Powered by AWS global infrastructure with millisecond-level latency and 99.98% uptime.
- **Full Message Support** – Handles every type of Telegram message, no exceptions.
- **Totally Free to Use** – Core two-way customer support features are 100% free for life!

**Give it a try:** @FriesOfficialBot  
https://t.me/FriesOfficialBot

## I. Introduction

An open-source bidirectional bot for Telegram. It helps to avoid spam messages and allows restricted clients to contact you smoothly.

[中文文档](https://github.com/MiHaKun/Telegram-interactive-bot/blob/master/README.md) | [Sample Bot](https://t.me/CustomerConnectBot) | [Sample Backend](https://t.me/MiHaCMSGroup)

(Note: After interacting with the sample bot, check the background to understand the principle.)

(Note: The sample background is a public group for demonstration purposes. For private deployment, it's recommended to use a private group for security reasons.)

![image-20240708130408336](./doc/cn/image-20240708130408336.png)

### Features
- When a client contacts customer service through the bot, all messages will be completely forwarded to the background management group, creating a separate sub-forum named after the client's information to distinguish them from other clients.
- Replies from customer service in the sub-forum can be directly sent to the client.
- Customer service can configure whether to continue the conversation with the client by closing/opening the sub-forum.
- Provides a permanent ban solution. There is a switch in the environment variables.
- Provides a /clear command to clear all messages in the sub-forum, also deleting user messages (not recommended, but sometimes necessary). There is a switch in the environment variables.

### Advantages
- By using sub-forums, multiple management members can be added to share the customer service workload.
- Complete communication records with clients can be intuitively retained.
- It's possible to know which customer service representative replied to a particular message, maintaining coherent customer service.

## 2. Preparation
The main principle of this bot is to forward the conversation between the client and the bot to a group (preferably a private group) and categorize each client's messages into a sub-category. Therefore, before starting, you need to:
1. Find @BotFather and apply for a bot.
2. Obtain the bot's token.
3. Create a group (set as public as needed).
4. Enable "Topics" in the group.
5. Add your bot to the group and promote it to an administrator.
6. Remember to include "Message Management" and "Topic Management" in the administrative permissions.
7. Use the bot @GetTheirIDBot to obtain the built-in ID of the group and the user ID of the administrator.
8. Use the bot @GetTheirIDBot to get the built-in ID and administrator user ID of the group.

   ![image-20240703082929589](./doc/en/image-20240703083738158.png)![image-20240703083040852](./doc/en/image-20240703083634098.png)

## 3. Deployment and Execution

### 1. Modify .env
Open `.env_example`, fill in your bot's Token, account's API_ID/HASH, the management group's ID, and the administrator's ID. Save `.env_example` as `.env`.

### 2. Build Python venv
```
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
```

### 3. Start Execution

#### 3.1 Direct execution
```bash
python -m interactive-bot
```

**Note:** For production, use a process manager like `PM2` or `supervisor` for auto-restart and uninterrupted operation.

#### 3.2 Docker execution

1. Install Docker — see [Install Docker under Ubuntu 22.04](https://gist.github.com/dehsilvadeveloper/c3bdf0f4cdcc5c177e2fe9be671820c7)
2. Copy `.env_example` to `.env` and fill in your configuration
3. Start the service:

```bash
docker compose up -d --build
```

**Data persistence:** The database (`assets/db.sqlite3`) and session state (`assets/*.pickle`) are mounted from the host `./assets` directory, so data survives container rebuilds.

```bash
# View logs
docker compose logs -f

# Stop service
docker compose down

# Rebuild and restart after code updates
docker compose up -d --build
```

Docker is also the recommended way to run locally. If you need a proxy, add `HTTPS_PROXY=http://host.docker.internal:<port>` to `.env` (Discord only supports HTTP proxies).

## 4. Mirror to Discord (optional)

When enabled, every private chat appears **both** in the Telegram admin group and in a Discord server; admins on either side can reply and see each other's replies.

- Each user gets a post in a Discord **forum channel** (the first message is a profile card). User / Telegram admin messages are posted through a webhook under their names; anything an admin says in the post is sent to the user and mirrored into the Telegram topic.
- Closing / reopening a topic ⇄ locking / unlocking the post (three-way reconciliation, no flip-flopping). `/clear` works on both sides (a Discord slash command inside the post). Edits, reactions and deletions of Discord-originated messages are synced.
- Formatting, mentions, media, albums, locations, polls, forwards (including Discord forwards / embeds) and replies are converted. Animated (.tgs) and video (.webm) stickers are rendered to **GIF**; Discord APNG / Lottie stickers become GIFs on Telegram.
- Large files: with MTProto configured, files over the Bot API limits are downloaded / uploaded via MTProto (up to 2GB); videos over Discord's upload limit are compressed with ffmpeg; anything that still cannot be synced gets a note with a link to the original Telegram message.
- Queued sync jobs are persisted and replayed after a restart or crash, without duplicates.

Setup:
1. Create a Discord application/bot, enable **Message Content Intent**, invite it (scopes `bot` + `applications.commands`) with permissions: View Channels, Send Messages, Send Messages in Threads, Create Public Threads, Manage Threads, Manage Webhooks, Attach Files, Embed Links, Read Message History, Add Reactions.
2. Create a forum channel (do not require tags) and fill `DISCORD_BOT_TOKEN`, `DISCORD_FORUM_CHANNEL_ID` (optional: `DISCORD_NOTIFY_CHANNEL_ID`, `DISCORD_ADMIN_ROLE_IDS`).
3. Recommended: fill `TG_API_ID` / `TG_API_HASH` from <https://my.telegram.org> (needed for history migration, large files and replaying TG messages after a restart). The bot reads the admin group history via MTProto **as itself** (no user login). A user's history is migrated automatically when their post is first created; send `/dc_migrate` in General to migrate everyone (or inside a topic for that user only). Migration is resumable and never duplicates live messages.

# ToDoList
- [x] Mirror conversations to Discord (with history migration).
- [x] Support message reply functionality. Messages can reference each other.
- [x] Improve the database.
- [x] Add client's human-machine recognition to prevent bored individuals from using userbots to spam.
- [x] Add and recognize media group messages.
- [x] Streamline the code, use payload to expand forwarding parameters.

# About

- This product is open-source under the Apache License.
- The author, MiHa (@MrMiHa), is a struggling programmer, not a coal miner. If you have questions, please don't come and give orders.
- Discussion group: https://t.me/DeveloperTeamGroup. Feel free to join and play around.
- Feel free to fork, but remember to retain the content in "About".
- The initial version was written in 2 hours. If you like it, please donate. If you have trouble deploying, ask for help in the group.
- RackNerd's servers are recommended. In fact, I use this one. It's cheap enough.
This one is enough: [2 cores 3GB - $27 per year](https://my.racknerd.com/aff.php?aff=11705&pid=828)
- If you can't deploy it, you can ask everyone in the group to help. You can also share servers with everyone: https://t.me/DeveloperTeamGroup.
