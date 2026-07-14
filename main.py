"""
初念点歌 - Chunian Music Plugin for AstrBot
- Author: 初念
- 说明: 结合网易云音源(搜索全 + 支持会员Cookie高音质)与 QQ 原生音乐卡片发送。
- 基于 NachoCrazy/netease-music-astrbot-plugin 二次开发, 新增 aiocqhttp 自定义音乐卡片能力。
- aiocqhttp(NapCat/Lagrange) 平台下默认发送可点击跳转的音乐卡片; 其它平台自动回退为文字+封面+链接。
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


# --- Helpers ---
def _find_ffmpeg() -> Optional[str]:
    """Try to locate the ffmpeg executable. Returns the absolute path or None."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    ]
    candidates.append(os.path.join(os.getcwd(), "ffmpeg", "bin", "ffmpeg.exe"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    try:
        proc = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if proc.returncode == 0:
            return "ffmpeg"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


# --- API Wrapper ---
class NeteaseMusicAPI:
    """A wrapper for the NeteaseCloudMusicApi to simplify interactions."""

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
        """Get the audio stream URL for a song with automatic quality fallback."""
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


# --- Main Plugin Class ---
class Main(star.Star):
    """网易云点歌 + QQ 音乐卡片。"""

    def __init__(self, context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self.config.setdefault("api_url", "http://127.0.0.1:3000")
        self.config.setdefault("quality", "exhigh")
        self.config.setdefault("search_limit", 5)
        self.config.setdefault("cookie", "")
        # 新增: 是否在 QQ(aiocqhttp) 平台优先发送音乐卡片
        self.config.setdefault("send_card", True)

        self.waiting_users: Dict[str, Dict[str, Any]] = {}
        self.song_cache: Dict[str, List[Dict[str, Any]]] = {}
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.api = NeteaseMusicAPI(self.config["api_url"], self.http_session)

        self._ffmpeg_path = _find_ffmpeg()
        if self._ffmpeg_path:
            ffmpeg_dir = os.path.dirname(self._ffmpeg_path)
            current_path = os.environ.get("PATH", "")
            if ffmpeg_dir not in current_path:
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + current_path
        self.cleanup_task: Optional[asyncio.Task] = None

    # --- Lifecycle Hooks ---
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
            expired_sessions = []
            for session_id, user_session in self.waiting_users.items():
                if user_session["expire"] < now:
                    expired_sessions.append((session_id, user_session["key"]))
            for session_id, cache_key in expired_sessions:
                if session_id in self.waiting_users:
                    del self.waiting_users[session_id]
                if cache_key in self.song_cache:
                    del self.song_cache[cache_key]

    # --- Event Handlers ---
    @filter.command("点歌", alias={"music", "听歌", "网易云"})
    async def cmd_handler(self, event: AstrMessageEvent, keyword: str = ""):
        if not keyword.strip():
            await event.send(MessageChain([Plain("主人，请告诉我您想听什么歌喵~ 例如：/点歌 Lemon")]))
            return
        event.stop_event()
        await self.search_and_show(event, keyword.strip())

    @filter.regex(r"(?i)^(来.?一首|播放|听.?听|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$")
    async def natural_language_handler(self, event: AstrMessageEvent):
        match = re.search(
            r"(?i)^(来.?一首|播放|听.?听|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$",
            event.message_str,
        )
        if match:
            keyword = match.group(2).strip()
            if keyword:
                event.stop_event()
                await self.search_and_show(event, keyword)

    @filter.regex(r"^\d+$", priority=999)
    async def number_selection_handler(self, event: AstrMessageEvent):
        session_id = event.get_session_id()
        if session_id not in self.waiting_users:
            return
        user_session = self.waiting_users[session_id]
        if time.time() > user_session["expire"]:
            return
        try:
            num = int(event.message_str.strip())
        except ValueError:
            return
        limit = self.config.get("search_limit", 5)
        if not (1 <= num <= limit):
            return
        event.stop_event()
        del self.waiting_users[session_id]
        await self.play_selected_song(event, user_session["key"], num)

    # --- Core Logic ---
    async def search_and_show(self, event: AstrMessageEvent, keyword: str):
        try:
            songs = await self.api.search_songs(keyword, self.config["search_limit"])
        except Exception as e:
            logger.error(f"初念点歌: 搜索失败 {e!s}")
            await event.send(MessageChain([Plain("呜喵...和音乐服务器的连接断掉了...主人，请检查一下 API 服务是否正常运行喵？")]))
            return
        if not songs:
            await event.send(MessageChain([Plain(f"对不起主人...我没能找到「{keyword}」这首歌喵... T_T")]))
            return
        cache_key = f"{event.get_session_id()}_{int(time.time())}"
        self.song_cache[cache_key] = songs
        response_lines = [f"主人，我为您找到了 {len(songs)} 首歌曲喵！请回复数字告诉我您想听哪一首~"]
        for i, song in enumerate(songs, 1):
            artists = " / ".join(a["name"] for a in song.get("artists", []))
            album = song.get("album", {}).get("name", "未知专辑")
            duration_ms = song.get("duration", 0)
            dur_str = f"{duration_ms//60000}:{(duration_ms%60000)//1000:02d}"
            response_lines.append(f"{i}. {song['name']} - {artists} 《{album}》 [{dur_str}]")
        await event.send(MessageChain([Plain("\n".join(response_lines))]))
        self.waiting_users[event.get_session_id()] = {"key": cache_key, "expire": time.time() + 60}

    async def play_selected_song(self, event: AstrMessageEvent, cache_key: str, num: int):
        if cache_key not in self.song_cache:
            await event.send(MessageChain([Plain("喵呜~ 主人选择得太久了，搜索结果已经凉掉了哦，请重新点歌吧~")]))
            return
        songs = self.song_cache[cache_key]
        if not (1 <= num <= len(songs)):
            await event.send(MessageChain([Plain("主人，您输入的数字不对哦，请选择列表里的歌曲编号喵~")]))
            return
        selected_song = songs[num - 1]
        song_id = selected_song["id"]
        try:
            song_details = await self.api.get_song_details(song_id)
            if not song_details:
                raise ValueError("无法获取歌曲详细信息。")
            audio_url = await self.api.get_audio_url(song_id, self.config["quality"], self.config.get("cookie", ""))
            if not audio_url:
                await event.send(MessageChain([Plain("喵~ 这首歌可能需要 VIP 或者没有版权，暂时不能为主人播放呢...")]))
                return
            title = song_details.get("name", "")
            artists = " / ".join(a["name"] for a in song_details.get("ar", []))
            album = song_details.get("al", {}).get("name", "未知专辑")
            cover_url = song_details.get("al", {}).get("picUrl", "")
            duration_ms = song_details.get("dt", 0)
            dur_str = f"{duration_ms//60000}:{(duration_ms%60000)//1000:02d}"
            await self._send_song_messages(
                event, num, song_id, title, artists, album, dur_str, cover_url, audio_url
            )
        except Exception as e:
            logger.error(f"初念点歌: 播放歌曲 {song_id} 失败 {e!s}")
            await event.send(MessageChain([Plain("呜...获取歌曲信息的时候失败了喵...")]))
        finally:
            if cache_key in self.song_cache:
                del self.song_cache[cache_key]

    async def _send_song_messages(
        self, event: AstrMessageEvent, num: int, song_id: int,
        title: str, artists: str, album: str, dur_str: str, cover_url: str, audio_url: str,
    ):
        """优先在 aiocqhttp(QQ) 平台发送自定义音乐卡片; 失败或其它平台回退为文字+封面+语音/链接。"""
        # 1) QQ 平台: 尝试发送自定义音乐卡片 (无需签名服务器)
        if self.config.get("send_card", True) and event.get_platform_name() == "aiocqhttp":
            try:
                jump_url = f"https://music.163.com/song?id={song_id}"
                card_segment = {
                    "type": "music",
                    "data": {
                        "type": "custom",
                        "url": jump_url,
                        "audio": audio_url,
                        "title": title,
                        "content": f"{artists} · {album}",
                        "image": cover_url,
                    },
                }
                client = event.bot  # aiocqhttp CQHttp 实例
                group_id = event.get_group_id()
                if group_id:
                    await client.call_action("send_group_msg", group_id=int(group_id), message=[card_segment])
                else:
                    await client.call_action("send_private_msg", user_id=int(event.get_sender_id()), message=[card_segment])
                return
            except Exception as e:
                logger.warning(f"初念点歌: 音乐卡片发送失败，回退为文字+链接。原因: {e!s}")

        # 2) 回退方案: 文字信息 + 封面 + 语音/链接
        detail_text = (
            f"遵命，主人！为您播放第 {num} 首歌曲~\n\n"
            f"♪ 歌名：{title}\n"
            f"🎤 歌手：{artists}\n"
            f"💿 专辑：{album}\n"
            f"⏳ 时长：{dur_str}\n"
            f"✨ 音质：{self.config['quality']}\n\n"
            f"请主人享用喵~\n"
        )
        info_components = [Plain(detail_text)]
        image_data = await self.api.download_image(cover_url)
        if image_data:
            info_components.append(Image.fromBase64(base64.b64encode(image_data).decode()))
        await event.send(MessageChain(info_components))
        try:
            await event.send(MessageChain([Record(file=audio_url)]))
        except Exception as e:
            err_msg = str(e)
            if "ffmpeg" in err_msg.lower() or "not found" in err_msg.lower():
                await event.send(MessageChain([Plain(f"🔊 点击播放：{audio_url}")]))
            else:
                await event.send(MessageChain([Plain(f"🔊 点击播放：{audio_url}")]))
