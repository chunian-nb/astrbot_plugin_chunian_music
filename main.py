"""
初念点歌 - Chunian Music Plugin for AstrBot
- Author: 初念
- 网易云音源 + QQ 音乐卡片; 支持自定义卡片参数与发卡后撤回中间消息。
"""
import os
import re
import time
import base64
import aiohttp
import asyncio
import shutil
import subprocess
import urllib.parse
from typing import Dict, Any, Optional, List

from astrbot.api import star, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain, Image, Record


def _find_ffmpeg() -> Optional[str]:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        proc = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if proc.returncode == 0:
            return "ffmpeg"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _safe_msg_id(event) -> Optional[int]:
    """尽量取到消息 ID，用于撤回。"""
    try:
        mid = getattr(getattr(event, "message_obj", None), "message_id", None)
        if mid is not None:
            return int(mid)
    except Exception:
        pass
    return None


class NeteaseMusicAPI:
    def __init__(self, api_url: str, session: aiohttp.ClientSession):
        self.base_url = api_url.rstrip("/")
        self.session = session

    async def search_songs(self, keyword: str, limit: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/search?keywords={urllib.parse.quote(keyword)}&limit={limit}&type=1"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("result", {}).get("songs", [])

    async def get_song_details(self, song_id: int) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/song/detail?ids={str(song_id)}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data["songs"][0] if data.get("songs") else None

    async def get_audio_url(self, song_id: int, quality: str, cookie: str = "") -> Optional[str]:
        qualities_to_try = list(dict.fromkeys([quality, "exhigh", "higher", "standard"]))
        for q in qualities_to_try:
            url = f"{self.base_url}/song/url/v1?id={str(song_id)}&level={q}&cookie={cookie}"
            async with self.session.get(url) as r:
                r.raise_for_status()
                data = await r.json()
                audio_info = data.get("data", [{}])[0]
                if audio_info.get("url"):
                    return audio_info["url"]
        return None

    async def download_image(self, url: str) -> Optional[bytes]:
        if not url:
            return None
        async with self.session.get(url) as r:
            if r.status == 200:
                return await r.read()
        return None


class Main(star.Star):
    def __init__(self, context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self.config.setdefault("api_url", "http://127.0.0.1:3000")
        self.config.setdefault("quality", "exhigh")
        self.config.setdefault("search_limit", 5)
        self.config.setdefault("cookie", "")
        # 卡片相关
        self.config.setdefault("send_card", True)
        self.config.setdefault("card_type", "custom")  # custom 或 163
        self.config.setdefault("card_title_template", "{title}")
        self.config.setdefault("card_content_template", "{artist} · {album}")
        self.config.setdefault("card_url_template", "https://music.163.com/song?id={id}")
        # 发卡后撤回中间消息
        self.config.setdefault("recall_messages", True)

        self.waiting_users: Dict[str, Dict[str, Any]] = {}
        self.song_cache: Dict[str, List[Dict[str, Any]]] = {}
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.api = NeteaseMusicAPI(self.config["api_url"], self.http_session)

        self._ffmpeg_path = _find_ffmpeg()
        if self._ffmpeg_path and self._ffmpeg_path != "ffmpeg":
            ffmpeg_dir = os.path.dirname(self._ffmpeg_path)
            cur = os.environ.get("PATH", "")
            if ffmpeg_dir not in cur:
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + cur
        self.cleanup_task: Optional[asyncio.Task] = None

    async def initialize(self):
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info("初念点歌: 后台清理任务已启动。")

    async def terminate(self):
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()

    async def _periodic_cleanup(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired = []
            for sid, us in self.waiting_users.items():
                if us["expire"] < now:
                    expired.append((sid, us["key"]))
            for sid, ck in expired:
                if sid in self.waiting_users:
                    del self.waiting_users[sid]
                if ck in self.song_cache:
                    del self.song_cache[ck]

    # --- Handlers ---
    @filter.command("点歌", alias={"music", "听歌", "网易云"})
    async def cmd_handler(self, event: AstrMessageEvent, keyword: str = ""):
        if not keyword.strip():
            await event.send(MessageChain([Plain("主人，请告诉我您想听什么歌喵~ 例如：/点歌 Lemon")]))
            return
        event.stop_event()
        await self.search_and_show(event, keyword.strip(), _safe_msg_id(event))

    @filter.regex(r"(?i)^(来.?一首|播放|听.?听|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$")
    async def natural_language_handler(self, event: AstrMessageEvent):
        match = re.search(r"(?i)^(来.?一首|播放|听.?听|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$", event.message_str)
        if match:
            keyword = match.group(2).strip()
            if keyword:
                event.stop_event()
                await self.search_and_show(event, keyword, _safe_msg_id(event))

    @filter.regex(r"^\d+$", priority=999)
    async def number_selection_handler(self, event: AstrMessageEvent):
        sid = event.get_session_id()
        if sid not in self.waiting_users:
            return
        us = self.waiting_users[sid]
        if time.time() > us["expire"]:
            return
        try:
            num = int(event.message_str.strip())
        except ValueError:
            return
        if not (1 <= num <= self.config.get("search_limit", 5)):
            return
        event.stop_event()
        num_msg_id = _safe_msg_id(event)
        del self.waiting_users[sid]
        await self.play_selected_song(
            event, us["key"], num,
            us.get("cmd_msg_id"), us.get("list_msg_id"), num_msg_id,
        )

    # --- Core ---
    async def search_and_show(self, event: AstrMessageEvent, keyword: str, cmd_msg_id: Optional[int] = None):
        try:
            songs = await self.api.search_songs(keyword, self.config["search_limit"])
        except Exception as e:
            logger.error(f"初念点歌: 搜索失败 {e!s}")
            await event.send(MessageChain([Plain("呜喵...和音乐服务器的连接断掉了...请检查 API 服务喵？")]))
            return
        if not songs:
            await event.send(MessageChain([Plain(f"对不起主人...没能找到「{keyword}」这首歌喵... T_T")]))
            return
        ck = f"{event.get_session_id()}_{int(time.time())}"
        self.song_cache[ck] = songs
        lines = [f"主人，我为您找到了 {len(songs)} 首歌曲喵！请回复数字告诉我您想听哪一首~"]
        for i, song in enumerate(songs, 1):
            artists = " / ".join(a["name"] for a in song.get("artists", []))
            album = song.get("album", {}).get("name", "未知专辑")
            d = song.get("duration", 0)
            lines.append(f"{i}. {song['name']} - {artists} 《{album}》 [{d//60000}:{(d%60000)//1000:02d}]")
        list_text = "\n".join(lines)

        list_msg_id = None
        if event.get_platform_name() == "aiocqhttp":
            # 用原生 API 发送以获取 message_id (便于后续撤回)
            try:
                client = event.bot
                gid = event.get_group_id()
                if gid:
                    ret = await client.call_action("send_group_msg", group_id=int(gid), message=list_text)
                else:
                    ret = await client.call_action("send_private_msg", user_id=int(event.get_sender_id()), message=list_text)
                if isinstance(ret, dict):
                    list_msg_id = ret.get("message_id")
            except Exception as e:
                logger.warning(f"初念点歌: 列表原生发送失败，改用普通发送。{e!s}")
                await event.send(MessageChain([Plain(list_text)]))
        else:
            await event.send(MessageChain([Plain(list_text)]))

        self.waiting_users[event.get_session_id()] = {
            "key": ck, "expire": time.time() + 60,
            "cmd_msg_id": cmd_msg_id, "list_msg_id": list_msg_id,
        }

    async def play_selected_song(self, event, cache_key, num,
                                 cmd_msg_id=None, list_msg_id=None, num_msg_id=None):
        if cache_key not in self.song_cache:
            await event.send(MessageChain([Plain("喵呜~ 选择太久啦，结果凉掉了，请重新点歌吧~")]))
            return
        songs = self.song_cache[cache_key]
        if not (1 <= num <= len(songs)):
            await event.send(MessageChain([Plain("数字不对哦，请选择列表里的编号喵~")]))
            return
        song = songs[num - 1]
        song_id = song["id"]
        try:
            det = await self.api.get_song_details(song_id)
            if not det:
                raise ValueError("无法获取歌曲详细信息")
            audio_url = await self.api.get_audio_url(song_id, self.config["quality"], self.config.get("cookie", ""))
            if not audio_url:
                await event.send(MessageChain([Plain("喵~ 这首可能需要 VIP 或无版权，暂时放不了呢...")]))
                return
            title = det.get("name", "")
            artists = " / ".join(a["name"] for a in det.get("ar", []))
            album = det.get("al", {}).get("name", "未知专辑")
            cover = det.get("al", {}).get("picUrl", "")
            d = det.get("dt", 0)
            dur = f"{d//60000}:{(d%60000)//1000:02d}"
            await self._send_song_messages(
                event, num, song_id, title, artists, album, dur, cover, audio_url,
                cmd_msg_id, list_msg_id, num_msg_id,
            )
        except Exception as e:
            logger.error(f"初念点歌: 播放失败 {e!s}")
            await event.send(MessageChain([Plain("呜...获取歌曲信息失败了喵...")]))
        finally:
            if cache_key in self.song_cache:
                del self.song_cache[cache_key]

    async def _recall(self, client, *msg_ids):
        """尽力撤回若干消息; 无权限/超时等失败静默跳过。"""
        for mid in msg_ids:
            if mid is None:
                continue
            try:
                await client.call_action("delete_msg", message_id=int(mid))
            except Exception as e:
                logger.warning(f"初念点歌: 撤回消息 {mid} 失败(可能无管理员权限): {e!s}")

    async def _send_song_messages(self, event, num, song_id, title, artists, album, dur, cover, audio_url,
                                  cmd_msg_id=None, list_msg_id=None, num_msg_id=None):
        # QQ 平台: 发送音乐卡片
        if self.config.get("send_card", True) and event.get_platform_name() == "aiocqhttp":
            try:
                client = event.bot
                gid = event.get_group_id()
                card_type = self.config.get("card_type", "custom")

                if card_type == "163":
                    seg = {"type": "music", "data": {"type": "163", "id": str(song_id)}}
                else:
                    title_txt = self.config.get("card_title_template", "{title}").format(
                        title=title, artist=artists, album=album, id=song_id)
                    content_txt = self.config.get("card_content_template", "{artist} · {album}").format(
                        title=title, artist=artists, album=album, id=song_id)
                    jump_url = self.config.get("card_url_template", "https://music.163.com/song?id={id}").format(
                        title=title, artist=artists, album=album, id=song_id)
                    seg = {
                        "type": "music",
                        "data": {
                            "type": "custom",
                            "url": jump_url,
                            "audio": audio_url,
                            "title": title_txt,
                            "content": content_txt,
                            "image": cover,
                        },
                    }

                if gid:
                    await client.call_action("send_group_msg", group_id=int(gid), message=[seg])
                else:
                    await client.call_action("send_private_msg", user_id=int(event.get_sender_id()), message=[seg])

                # 卡片发送成功后, 撤回中间消息, 只留卡片
                if self.config.get("recall_messages", True):
                    await self._recall(client, cmd_msg_id, list_msg_id, num_msg_id)
                return
            except Exception as e:
                logger.warning(f"初念点歌: 卡片发送失败({self.config.get('card_type')})，回退。原因: {e!s}")
                # 163 失败时再尝试 custom 一次
                if self.config.get("card_type") == "163":
                    try:
                        client = event.bot
                        gid = event.get_group_id()
                        seg = {"type": "music", "data": {
                            "type": "custom",
                            "url": f"https://music.163.com/song?id={song_id}",
                            "audio": audio_url, "title": title,
                            "content": f"{artists} · {album}", "image": cover}}
                        if gid:
                            await client.call_action("send_group_msg", group_id=int(gid), message=[seg])
                        else:
                            await client.call_action("send_private_msg", user_id=int(event.get_sender_id()), message=[seg])
                        if self.config.get("recall_messages", True):
                            await self._recall(client, cmd_msg_id, list_msg_id, num_msg_id)
                        return
                    except Exception as e2:
                        logger.warning(f"初念点歌: custom 兜底也失败: {e2!s}")

        # 回退: 文字 + 封面 + 链接
        text = (f"遵命，主人！为您播放第 {num} 首歌曲~\n\n♪ 歌名：{title}\n🎤 歌手：{artists}\n"
                f"💿 专辑：{album}\n⏳ 时长：{dur}\n✨ 音质：{self.config['quality']}\n\n请主人享用喵~")
        comps = [Plain(text)]
        img = await self.api.download_image(cover)
        if img:
            comps.append(Image.fromBase64(base64.b64encode(img).decode()))
        await event.send(MessageChain(comps))
        await event.send(MessageChain([Plain(f"🔊 点击播放：{audio_url}")]))
