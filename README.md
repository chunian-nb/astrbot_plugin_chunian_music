# 初念点歌 (astrbot_plugin_chunian_music)

✨ 网易云音源 + QQ 原生音乐卡片，一个插件全都要 ✨

作者：初念 | License: MIT

## 📖 简介

一款为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的点歌插件，把两件好事合到一起：

- 网易云音源：搜索命中率高，配合会员 Cookie 可解锁无损 / Hi-Res 等高音质
- QQ 原生音乐卡片：在 QQ(aiocqhttp / NapCat) 平台自动发送可点击跳转的音乐卡片，无需签名服务器

QQ 平台优先发卡片；卡片失败或其它平台时，自动回退为「文字 + 封面 + 播放链接」。

## ✨ 功能

- 交互式点歌：关键词搜索返回列表，回复数字选歌
- 命令与自然语言两种触发（`/点歌 xxx`、`来一首 xxx`）
- QQ 自定义音乐卡片（custom 类型，不依赖 musicSignUrl）
- 音乐卡片字段可配置：来源扩展字段、跳转 URL、音频 URL、标题、副标题、封面
- 最终卡片发送成功后，可自动撤回点歌命令、搜索列表和数字选择消息
- 智能音质回退，提高播放成功率
- 支持会员 Cookie，解锁高音质
- WebUI 可视化配置

## 📦 前置依赖：网易云 API 服务

推荐 Docker 一键部署：

    docker run -d --name ncm-api --restart always -p 3000:3000 \
      -e http_proxy= -e https_proxy= -e no_proxy= \
      -e HTTP_PROXY= -e HTTPS_PROXY= -e NO_PROXY= \
      moefurina/ncm-api:latest

访问 `http://localhost:3000` 能打开即为成功。
API 项目：https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced

## 🚀 安装

    cd /path/to/AstrBot/data/plugins
    git clone https://github.com/chunian-nb/astrbot_plugin_chunian_music.git

重启 AstrBot 即可。

## ⚙️ 配置

在 AstrBot WebUI 插件配置中设置：

- `api_url`：网易云 API 服务地址，例如 `http://172.17.0.1:3000`
- `quality`：优先音质 lossless / exhigh / higher / standard
- `search_limit`：每次搜索返回的歌曲数量
- `cookie`：网易云 Cookie（含 MUSIC_U），解锁会员音质
- `send_card`：QQ 平台是否优先发送音乐卡片
- `card_source`：卡片来源扩展字段 `source`，留空则不发送
- `card_url`：点击卡片后的跳转 URL，默认打开网易云歌曲页
- `card_audio`：卡片音频 URL
- `card_title`：卡片标题
- `card_subtitle`：卡片副标题，对应 OneBot 的 `content`
- `card_cover`：卡片封面 URL
- `recall_intermediate`：卡片成功后撤回点歌命令、歌曲列表和数字选择消息

卡片字段支持模板占位符：

- `{song_id}`：网易云歌曲 ID
- `{netease_url}`：网易云歌曲详情页
- `{title}`：歌曲名
- `{artists}`：歌手
- `{album}`：专辑
- `{duration}`：时长
- `{audio_url}`：实际音频地址
- `{cover_url}`：封面地址

默认卡片配置：

```text
card_url      = https://music.163.com/song?id={song_id}
card_audio    = {audio_url}
card_title    = {title}
card_subtitle = {artists} · {album}
card_cover    = {cover_url}
```

> `card_source` 不是 OneBot 11 自定义音乐卡片的标准字段。插件会把它作为兼容扩展字段发送；严格校验的 NapCat 版本若拒绝该字段，插件会自动去掉后重试。QQ 客户端左下角的“QQ音乐”来源小标通常由协议端或客户端决定，因此不保证能被覆盖。

### 自动撤回的限制

当 `recall_intermediate=true` 且最终音乐卡片发送成功时，插件按顺序尝试撤回：

1. 机器人发送的歌曲列表
2. 用户回复的数字
3. 用户发送的 `/点歌` 命令或自然语言点歌消息

机器人撤回自己发送的歌曲列表通常不需要额外权限；撤回群成员发送的命令和数字，需要机器人账号是该群的管理员或群主。撤回还受 QQ/NapCat 的消息撤回时限约束。某条消息撤回失败不会影响最终音乐卡片。

跨容器部署时 `api_url` 请填宿主机网桥地址（如 `http://172.17.0.1:3000`），不要用 localhost。

获取 Cookie：电脑浏览器登录 music.163.com，F12 → Network → 刷新 → 复制任意请求 Request Headers 里的 Cookie 整段（需含 MUSIC_U）。

## 📝 使用

- 命令：`/点歌 歌名`（别名 music / 听歌 / 网易云）
- 自然语言：`来一首 晴天`
- 选歌：返回列表后回复数字

## 🙏 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [NachoCrazy/netease-music-astrbot-plugin](https://github.com/NachoCrazy/netease-music-astrbot-plugin)
- [NeteaseCloudMusicApiEnhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)

## 📄 License

MIT