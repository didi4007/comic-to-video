# comic-to-video

在**完全不改变漫画原画**的基础上，将单页多格或连环漫画自动制作成高清动态漫视频（MP4）的 Antigravity 技能包。

## 核心特性

- **100% 保留原画像素**：严禁 AI 重绘或拉伸变形；通过精准分格裁切、同图暗化高斯模糊沉浸式衬底与平滑 Ken Burns 运镜，完整保留每一格原画、气泡与旁白文字。
- **多角色中文神经配音**：内置 8 种中文神经声线预设（`edge-tts`，断网自动降级 macOS `say`），按语音时长自动驱动镜头节奏（以音定画）。
- **非破坏性轻量特效**：支持暗角（`vignette`）、胶片微颗粒（`film_grain`）、细雨（`rain`）、油灯暖光微闪（`candle_flicker`）、热气雾气（`steam`）、喜庆火星（`sparks`）、晨光斜射（`sunbeams`）、镜头微震（`shake`）及淡入淡出/白闪/硬切转场。
- **拟声音效与氛围配乐**：内置程序化音效（细雨、煎炸、拧瓶盖、滴水、鞭炮、入梦钟声、转场风声）与五声调式氛围 BGM（配音时自动压低音量），也支持 `--bgm` 传入自有音乐。
- **横竖屏双画幅**：支持 `16:9`（1920×1080 大屏/横屏）与 `9:16`（1080×1920 短视频竖屏）。

## 一键安装

在终端运行以下命令即可自动安装到 `~/.gemini/config/skills/comic-to-video`（已安装时会自动更新）：

```bash
curl -fsSL https://raw.githubusercontent.com/didi4007/comic-to-video/main/install.sh | bash
```

或者手动克隆：

```bash
git clone https://github.com/didi4007/comic-to-video.git ~/.gemini/config/skills/comic-to-video
```

### 运行依赖

- `ffmpeg` / `ffprobe`
- `uv`（自动隔离管理 `edge-tts`、`pillow`、`numpy`，零污染系统 Python）

macOS 可通过 Homebrew 一键安装依赖：

```bash
brew install ffmpeg uv
```

## 使用方法

安装后，在 Antigravity 中直接发给助手漫画目录即可触发，例如：

> 用 `comic-to-video` 把这个漫画目录做成动态漫视频：`@[/path/to/comic_folder]`

也可直接命令行调用渲染引擎：

```bash
uv run --with edge-tts --with pillow --with numpy python3 \
  "$HOME/.gemini/config/skills/comic-to-video/scripts/build_comic_video.py" \
  --storyboard /path/to/storyboard.json \
  --output /path/to/output.mp4 \
  --aspect 16:9 \
  --save-preview-frames
```

详细分镜 JSON 格式、声线预设与特效列表见 [SKILL.md](./SKILL.md)。
