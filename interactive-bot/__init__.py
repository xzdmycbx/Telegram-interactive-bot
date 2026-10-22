import pkg_resources
import os
import sys
import json
import logging
from dotenv import load_dotenv


# 配置日志记录器
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s- %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("log.txt")],
)
logging.getLogger("httpx").setLevel(logging.ERROR)
current_package = os.path.basename(os.path.dirname(__file__))
logger = logging.getLogger(current_package)

# 读取配置文件
load_dotenv()
bot_token = os.getenv("BOT_TOKEN") or exit("BOT_TOKEN 未填写")
app_name = os.getenv("APP_NAME") or exit("APP_NAME 未填写")
welcome_message = os.getenv("WELCOME_MESSAGE") or "欢迎使用本机器人"
try:
    admin_group_id = int(os.getenv("ADMIN_GROUP_ID")) or exit("ADMIN_GROUP 未填写")
    admin_user_ids = [
        int(x.strip()) for x in os.getenv("ADMIN_USER_IDS").split(",")
    ] or exit("ADMIN_USER_IDS 未填写")
except ValueError:
    exit("ADMIN_GROUP_ID or ADMIN_USER_IDS 应该是数字\n其中ADMIN_USER_IDS是以“,”分隔")


is_delete_topic_as_ban_forever = os.getenv("DELETE_TOPIC_AS_FOREVER_BAN") == "TRUE"
is_delete_user_messages = os.getenv("DELETE_USER_MESSAGE_ON_CLEAR_CMD") == "TRUE"
disable_captcha = os.getenv("DISABLE_CAPTCHA") == "TRUE"
message_interval = int(os.getenv("MESSAGE_INTERVAL", 3))

_general_topic_id_raw = os.getenv("GENERAL_TOPIC_ID", "").strip()
general_topic_id = int(_general_topic_id_raw) if _general_topic_id_raw else None

# Discord 同步（可选，不填 DISCORD_BOT_TOKEN 则不启用）
discord_bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
try:
    discord_forum_channel_id = int(os.getenv("DISCORD_FORUM_CHANNEL_ID", "").strip() or 0)
    discord_notify_channel_id = int(os.getenv("DISCORD_NOTIFY_CHANNEL_ID", "").strip() or 0)
    discord_admin_role_ids = [
        int(x.strip()) for x in os.getenv("DISCORD_ADMIN_ROLE_IDS", "").split(",") if x.strip()
    ]
    tg_api_id = int(os.getenv("TG_API_ID", "").strip() or 0)
except ValueError:
    exit("DISCORD_FORUM_CHANNEL_ID / DISCORD_NOTIFY_CHANNEL_ID / DISCORD_ADMIN_ROLE_IDS / TG_API_ID 应该是数字")
tg_api_hash = os.getenv("TG_API_HASH", "").strip()
if discord_bot_token and not discord_forum_channel_id:
    exit("启用 Discord 同步时 DISCORD_FORUM_CHANNEL_ID 必须填写")

# 代理（可选）。PTB(httpx) 会自动读取该环境变量，Discord / Telethon 需要显式传入
proxy_url = (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "").strip() or None
