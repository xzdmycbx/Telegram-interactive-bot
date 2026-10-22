"""Discord webhook 消息的头像。

webhook 只能通过 avatar_url 指定头像，而且必须是公网能直接访问的图片地址；
Telegram 头像没有这样的公开地址（Bot API 的下载链接含机器人 Token，不能外泄），所以：

1. 配置了 AVATAR_PUBLIC_URL：程序自带一个小型 HTTP 服务提供用户的 Telegram 头像。
   地址里带头像版本和签名，别人猜不到；用户换头像后地址随之变化，Discord 不会一直显示旧图。
2. 否则：有公开用户名且头像公开的用户，用 t.me 页面里 Telegram 官方 CDN 的头像地址。
3. 都拿不到：用 Discord 自带的默认头像（用户、TG 管理员两种颜色区分）。
"""

import glob
import hashlib
import hmac
import html
import os
import re
import time
from collections import OrderedDict

import aiohttp
from aiohttp import web

from .dc_common import logger

DEFAULT_USER = "https://cdn.discordapp.com/embed/avatars/0.png"
DEFAULT_ADMIN = "https://cdn.discordapp.com/embed/avatars/2.png"
DEFAULT_DISCORD = "https://cdn.discordapp.com/embed/avatars/1.png"
CACHE_TTL = 12 * 3600
KEEP_VERSIONS = 3

_OG_IMAGE = re.compile(r'<meta\s+property="og:image"\s+content="([^"]+)"')
_TG_CDN = re.compile(r"^https://([\w-]+\.)*(cdn-telegram\.org|telesco\.pe)/")
_VERSION = re.compile(r"^[\w-]{1,64}$")


class AvatarProvider:
    def __init__(self, *, bot, http, bot_token, directory, public_url=None, port=8080, proxy=None):
        self.bot = bot
        self.http = http
        self.proxy = proxy
        self.public_url = (public_url or "").rstrip("/") or None
        self.port = port
        self.directory = directory
        self._secret = hashlib.sha256(b"avatar:" + bot_token.encode()).digest()
        self._cache = OrderedDict()  # user_id -> (url 或 None, 过期时间)
        self._runner = None

    # ---------- 对外 ----------

    async def url_for(self, user_id, username=None, *, admin=False):
        """返回头像地址；拿不到真实头像时返回默认头像，任何异常都不影响消息同步"""
        default = DEFAULT_ADMIN if admin else DEFAULT_USER
        if not user_id:
            return default
        cached = self._cache.get(user_id)
        if cached and cached[1] > time.time():
            return cached[0] or default
        url = None
        try:
            if self.public_url:
                url = await self._self_hosted(user_id)
            if url is None and username:
                url = await self._public_link(username)
        except Exception as e:
            logger.info(f"获取头像失败 {user_id}: {e}")
        self._cache[user_id] = (url, time.time() + CACHE_TTL)
        self._cache.move_to_end(user_id)
        while len(self._cache) > 5000:
            self._cache.popitem(last=False)
        return url or default

    # ---------- 自建服务 ----------

    def _sign(self, user_id, version):
        return hmac.new(self._secret, f"{user_id}/{version}".encode(), hashlib.sha256).hexdigest()[:24]

    def _path(self, user_id, version):
        return os.path.join(self.directory, f"{user_id}_{version}.jpg")

    async def _self_hosted(self, user_id):
        photos = await self.bot.get_user_profile_photos(user_id, limit=1)
        if not photos.total_count:
            return None
        sizes = photos.photos[0]
        # 头像显示得很小，取 320 左右的尺寸即可
        size = next((s for s in sizes if s.width >= 256), sizes[-1])
        version = re.sub(r"[^\w-]", "", size.file_unique_id)[:64]
        path = self._path(user_id, version)
        if not os.path.exists(path):
            f = await self.bot.get_file(size.file_id)
            data = bytes(await f.download_as_bytearray())
            os.makedirs(self.directory, exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as fp:
                fp.write(data)
            os.replace(tmp, path)
            self._prune(user_id)
        return f"{self.public_url}/avatar/{user_id}/{version}/{self._sign(user_id, version)}.jpg"

    def _prune(self, user_id):
        """每个用户只保留最近几个版本（旧消息仍引用旧地址，不能马上删）"""
        files = sorted(glob.glob(os.path.join(self.directory, f"{user_id}_*.jpg")), key=os.path.getmtime)
        for old in files[:-KEEP_VERSIONS]:
            try:
                os.remove(old)
            except OSError:
                pass

    async def start_server(self):
        app = web.Application()
        app.router.add_get("/avatar/{user_id}/{version}/{sig}.jpg", self._handle)
        app.router.add_get("/healthz", lambda request: web.Response(text="ok"))
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"头像服务已启动：端口 {self.port}，公网地址 {self.public_url}")
        return site

    async def close(self):
        if self._runner:
            await self._runner.cleanup()

    async def _handle(self, request):
        user_id = request.match_info["user_id"]
        version = request.match_info["version"]
        sig = request.match_info["sig"]
        if not user_id.isdigit() or not _VERSION.match(version):
            raise web.HTTPNotFound()
        if not hmac.compare_digest(sig, self._sign(int(user_id), version)):
            raise web.HTTPNotFound()
        path = self._path(int(user_id), version)
        if not os.path.exists(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=604800", "Content-Type": "image/jpeg"})

    # ---------- t.me 公开头像 ----------

    async def _public_link(self, username):
        timeout = aiohttp.ClientTimeout(total=10)
        async with self.http.get(f"https://t.me/{username}", proxy=self.proxy, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            text = await resp.text()
        m = _OG_IMAGE.search(text)
        if not m:
            return None
        url = html.unescape(m.group(1))
        # 没有头像或头像不公开时，页面给的是 Telegram 的 logo，不是用户头像
        return url if _TG_CDN.match(url) else None
