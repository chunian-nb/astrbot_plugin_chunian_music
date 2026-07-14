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