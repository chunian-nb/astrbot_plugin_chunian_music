"""
初念点歌 - Chunian Music Plugin for AstrBot
- Author: 初念
- 网易云音源 + QQ 音乐卡片; card_type 二选一(custom/163); custom 卡片封面/标题/歌手/跳转可自定义(留空用真实); 发卡后撤回中间消息。
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
from PIL import Image as PILImage, ImageDraw, ImageFont
import io as _io


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

    async def get_songs_detail(self, ids):
        """批量获取歌曲详情，返回 {id: picUrl} 映射。"""
        try:
            ids_str = ",".join(str(i) for i in ids)
            url = f"{self.base_url}/song/detail?ids={ids_str}"
            async with self.session.get(url) as r:
                r.raise_for_status()
                data = await r.json()
                result = {}
                for song in data.get("songs", []):
                    pic = (song.get("al", {}) or {}).get("picUrl", "")
                    result[song.get("id")] = pic
                return result
        except Exception:
            return {}

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
        self.config.setdefault("send_card", True)
        # 卡片类型: custom(自定义,显示QQ音乐标) 或 163(网易云原生卡片,显示网易云标,需NapCat音卡签名)
        self.config.setdefault("card_type", "custom")
        # 以下四项仅对 custom 生效: 留空=用真实值, 填了=用自定义值
        self.config.setdefault("card_title", "")
        self.config.setdefault("card_singer", "")
        self.config.setdefault("card_image", "")
        self.config.setdefault("card_jump_url", "")
        # 发卡后撤回中间消息
        self.config.setdefault("recall_messages", True)

        self.waiting_users: Dict[str, Dict[str, Any]] = {}
        self.song_cache: Dict[str, List[Dict[str, Any]]] = {}
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.api = NeteaseMusicAPI(self.config["api_url"], self.http_session)
        # 图片渲染：字体路径（插件目录下）与开关
        self.config.setdefault("result_as_image", True)
        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self._font_path = os.path.join(self._plugin_dir, "DreamHanSans-W17.ttc")


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

    async def _fetch_cover(self, url, size=76, radius=10):
        """下载封面并裁成圆角，返回 PILImage(RGBA)，失败返回灰色占位。"""
        img = None
        try:
            data = await self.api.download_image(url)
            if data:
                img = PILImage.open(_io.BytesIO(data)).convert("RGB")
        except Exception:
            img = None
        if img is None:
            img = PILImage.new("RGB", (size, size), (244, 245, 247))
        img = img.resize((size, size), PILImage.LANCZOS)
        mask = PILImage.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, size, size], radius=radius, fill=255)
        out = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        return out

    async def _render_result_image(self, songs):
        """把搜索结果渲染成一张白色简洁风格图片，返回 PNG bytes。"""
        W, pad = 880, 40
        row_h, header_h, footer_h = 120, 130, 80
        H = header_h + row_h * len(songs) + footer_h
        img = PILImage.new("RGB", (W, H), (255, 255, 255))
        d = ImageDraw.Draw(img)
        fp = self._font_path
        f_title = ImageFont.truetype(fp, 44)
        f_subh = ImageFont.truetype(fp, 24)
        f_song = ImageFont.truetype(fp, 36)
        f_meta = ImageFont.truetype(fp, 24)
        f_num = ImageFont.truetype(fp, 34)
        f_foot = ImageFont.truetype(fp, 26)
        INK, GRAY, LINE, RED = (33, 33, 36), (150, 152, 158), (238, 239, 241), (236, 65, 65)

        d.text((pad, 44), "初念点歌", font=f_title, fill=INK)
        tw = d.textlength("网易云 · 搜索结果", font=f_subh)
        d.text((W - pad - tw, 60), "网易云 · 搜索结果", font=f_subh, fill=GRAY)
        d.line([(pad, header_h - 16), (W - pad, header_h - 16)], fill=LINE, width=2)

        try:
            ids = [s2.get("id") for s2 in songs if s2.get("id")]
            pic_map = await self.api.get_songs_detail(ids)
        except Exception:
            pic_map = {}

        y = header_h
        for i, s2 in enumerate(songs):
            mid = y + row_h // 2
            d.text((pad, mid - 22), str(i + 1), font=f_num, fill=RED)
            cx, cs = pad + 56, 76
            cover_url = pic_map.get(s2.get("id"), "") or s2.get("album", {}).get("picUrl", "")
            cov = await self._fetch_cover(cover_url, size=cs, radius=10)
            img.paste(cov, (cx, mid - cs // 2), cov)
            artists = " / ".join(a["name"] for a in s2.get("artists", []))
            album = s2.get("album", {}).get("name", "未知专辑")
            dms = s2.get("duration", 0)
            dur = f"{dms//60000}:{(dms%60000)//1000:02d}"
            tx = cx + cs + 20
            title = s2.get("name", "")
            if len(title) > 18:
                title = title[:18] + "…"
            d.text((tx, mid - 34), title, font=f_song, fill=INK)
            meta = f"{artists} · {album} · {dur}"
            if len(meta) > 30:
                meta = meta[:30] + "…"
            d.text((tx, mid + 6), meta, font=f_meta, fill=GRAY)
            if i < len(songs) - 1:
                d.line([(pad, y + row_h), (W - pad, y + row_h)], fill=LINE, width=2)
            y += row_h
        d.text((pad, H - 56), "回复数字选择你想听的歌", font=f_foot, fill=GRAY)

        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

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

        # 若开启图片模式，优先渲染图片发送
        if self.config.get("result_as_image", True) and self._font_path and os.path.isfile(self._font_path):
            try:
                png = await self._render_result_image(songs)
                await event.send(MessageChain([Image.fromBytes(png)]))
                self.waiting_users[event.get_session_id()] = {
                    "key": ck, "expire": time.time() + 60,
                    "cmd_msg_id": cmd_msg_id, "list_msg_id": None,
                }
                return
            except Exception as _e:
                logger.warning(f"初念点歌: 图片渲染失败，回退文字列表。原因: {_e!s}")

        list_msg_id = None
        if event.get_platform_name() == "aiocqhttp":
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
        for mid in msg_ids:
            if mid is None:
                continue
            try:
                await client.call_action("delete_msg", message_id=int(mid))
            except Exception as e:
                logger.warning(f"初念点歌: 撤回消息 {mid} 失败(可能无管理员权限): {e!s}")

    def _build_custom_card(self, song_id, title, artists, album, cover, audio_url):
        """构造 custom 音乐卡片段。四个可自定义项留空则用真实值。"""
        c_title = self.config.get("card_title", "") or title
        c_singer = self.config.get("card_singer", "") or artists
        c_image = self.config.get("card_image", "") or cover
        c_url = self.config.get("card_jump_url", "") or f"https://music.163.com/song?id={song_id}"
        return {
            "type": "music",
            "data": {
                "type": "custom",
                "url": c_url,       # 点击跳转(默认真实网易云地址)
                "audio": audio_url, # 音频直链
                "title": c_title,   # 标题
                "image": c_image,   # 封面(NapCat 字段名为 image)
                "singer": c_singer, # 歌手/副标题(NapCat 字段名为 singer)
            },
        }

    async def _send_card_segment(self, event, seg):
        client = event.bot
        gid = event.get_group_id()
        if gid:
            await client.call_action("send_group_msg", group_id=int(gid), message=[seg])
        else:
            await client.call_action("send_private_msg", user_id=int(event.get_sender_id()), message=[seg])

    async def _send_song_messages(self, event, num, song_id, title, artists, album, dur, cover, audio_url,
                                  cmd_msg_id=None, list_msg_id=None, num_msg_id=None):
        if self.config.get("send_card", True) and event.get_platform_name() == "aiocqhttp":
            card_type = self.config.get("card_type", "custom")
            try:
                if card_type == "163":
                    # 网易云原生卡片(显示网易云标, 需 NapCat 音卡签名; 失败则回退 custom)
                    seg = {"type": "music", "data": {"type": "163", "id": str(song_id)}}
                else:
                    seg = self._build_custom_card(song_id, title, artists, album, cover, audio_url)
                await self._send_card_segment(event, seg)
                if self.config.get("recall_messages", True):
                    await self._recall(event.bot, cmd_msg_id, list_msg_id, num_msg_id)
                return
            except Exception as e:
                logger.warning(f"初念点歌: {card_type} 卡片发送失败，尝试回退。原因: {e!s}")
                if card_type == "163":
                    # 163 失败 → 用 custom 再试一次
                    try:
                        seg = self._build_custom_card(song_id, title, artists, album, cover, audio_url)
                        await self._send_card_segment(event, seg)
                        if self.config.get("recall_messages", True):
                            await self._recall(event.bot, cmd_msg_id, list_msg_id, num_msg_id)
                        return
                    except Exception as e2:
                        logger.warning(f"初念点歌: custom 兜底也失败: {e2!s}")

        # 最终回退: 文字 + 封面 + 链接
        text = (f"遵命，主人！为您播放第 {num} 首歌曲~\n\n♪ 歌名：{title}\n🎤 歌手：{artists}\n"
                f"💿 专辑：{album}\n⏳ 时长：{dur}\n✨ 音质：{self.config['quality']}\n\n请主人享用喵~")
        comps = [Plain(text)]
        img = await self.api.download_image(cover)
        if img:
            comps.append(Image.fromBase64(base64.b64encode(img).decode()))
        await event.send(MessageChain(comps))
        await event.send(MessageChain([Plain(f"🔊 点击播放：{audio_url}")]))
