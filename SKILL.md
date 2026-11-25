---
name: comic-to-video
description: 在完全不改变漫画原画的基础上，将单页多格或连环漫画自动制作成高清动态漫视频（MP4），并自动匹配多角色中文神经配音、分格运镜、轻量非破坏性特效、拟声音效与氛围配乐。适用于漫画作品动态化展示、获奖漫画视频化、大屏轮播与短视频发布。
---

# 漫画零改动转视频技能（comic-to-video）

当用户提供一个漫画目录或一组漫画图片，要求在**完全不改变漫画原画**的前提下将其制作成带配音和轻量特效的视频时，按以下 4 步执行。

## 核心红线（绝不违反）

1. **严禁重绘或修改原画**：绝不调用 AI 生图或图生视频模型改写漫画画面；所有画面必须 100% 源自原始高清图片像素。
2. **严禁裁切气泡与文字**：划分分格坐标（`crop: [x, y, w, h]`）时，必须完整包住伸出边框外的对话气泡、拟声词炸裂框与跨格旁白框；非 16:9 分格自动使用同图高斯模糊暗化衬底 + 原比例完整居中，绝不拉伸变形。
3. **以音定画，节奏自然**：先生成配音、自动测量语音时长再决定镜头时长，避免“话没说完切镜头”或长时间干等。

---

## 第一步：通读漫画与精测分格

1. 列出目标目录下的全部漫画图片（按文件名自然排序），批量调用 `read_file` 完整阅读每一页的画面、人物、情绪与全部文字（旁白、对话、拟声词）。
2. 如需精准分格坐标，可用简短 Python 脚本或目测估算每个分格的归一化坐标 `[x, y, w, h]`（取值 `0.0 ~ 1.0`），原则是**宁多留 1%~2% 边距，绝不切到气泡边角**。
   - 若某页有多个紧凑小格共同表达连贯动作（如三连特写），拆分为连续独立镜头，用 `cut` 硬切营造电影剪辑感。
   - 若某句跨格旁白连接两幅画面，可将该旁白所在分格合框呈现，或先单格建立再合框收尾。

---

## 第二步：编写分镜、配音与特效方案（`storyboard.json`）

在目标作品目录下创建 `<作品名>_storyboard.json`，完整规划每个镜头的：
- **分格裁切**：`image`、`crop: [x, y, w, h]`
- **多角色配音（`lines`）**：内置 8 种中文神经声线预设（`role`），也可直接指定 `voice` / `rate` / `pitch`：
  - `narrator`（沉稳旁白 `zh-CN-YunyangNeural`）
  - `youth` / `youth_excited`（青年男声 `zh-CN-YunxiNeural`）
  - `uncle` / `uncle_cheer`（浑厚/喜庆大叔 `zh-CN-YunjianNeural`）
  - `female` / `female_lively`（自然/活泼女声 `zh-CN-XiaoxiaoNeural` / `XiaoyiNeural`）
  - `elder`（沧桑长辈）
- **镜头运镜（`motion`）**：`zoom_in`、`zoom_out`、`impact_zoom`、`pan_left`、`pan_right`、`tilt_up`、`tilt_down`、`static`
- **非破坏性轻量特效（`effects`）**：可自由组合
  - `vignette`（电影暗角聚焦）
  - `candle_flicker`（油灯/烛光暖色微闪）
  - `rain`（细雨斜丝）
  - `steam`（热气/酒气微升雾气）
  - `sparks`（鞭炮/喜庆火星粒子）
  - `sunbeams`（晨光斜射光斑）
  - `film_grain`（复古胶片微颗粒）
  - `shake`（短促镜头震动，适合爆炸/冲击感）
- **转场（`transition_in` / `transition_out`）**：`fade`（黑淡）、`fadeblack`（黑场转折，适合入梦/梦醒）、`fadewhite`（白闪，适合穿越时空）、`cut`（干脆硬切）
- **拟声音效（`sfx`）**：`rain_ambience`、`sizzle`、`bottle_open`、`water_drop`、`firecracker`、`chime_dream`、`whoosh`

JSON 结构示例：
```json
{
  "title": "一饮百年",
  "fps": 30,
  "bgm_mood": "nostalgic",
  "title_card": { "title": "一饮百年", "subtitle": "穿越百年的那一口人间烟火", "duration": 2.4 },
  "end_card": { "title": "一饮百年", "subtitle": "敬过去，更敬我辈奋斗的美好", "duration": 2.8 },
  "shots": [
    {
      "id": "p01_1",
      "image": "01.png",
      "crop": [0.02, 0.01, 0.48, 0.46],
      "motion": "zoom_in",
      "effects": ["vignette", "film_grain"],
      "sfx": [],
      "transition_in": "fadeblack",
      "transition_out": "fade",
      "lines": [
        { "role": "narrator", "text": "民国六年七月，北京城的旗子换得比天还快。" }
      ]
    }
  ]
}
```

---

## 第三步：一键渲染高清视频与预览拼图

使用 `uv` 运行内置渲染脚本（自动管理 `edge-tts`、`pillow`、`numpy`，零系统污染；断网时自动降级为 macOS `say`）：

```bash
uv run --with edge-tts --with pillow --with numpy python3 \
  "$HOME/.gemini/config/skills/comic-to-video/scripts/build_comic_video.py" \
  --storyboard "<作品目录>/<作品名>_storyboard.json" \
  --output "<作品目录>/<作品名>_动态漫.mp4" \
  --aspect 16:9 \
  --save-preview-frames
```

- 如需竖屏（抖音/视频号），将 `--aspect 16:9` 改为 `--aspect 9:16`。
- 如用户提供了外部配乐，追加 `--bgm "/path/to/music.mp3"`；否则自动生成温和的五声调式氛围配乐并开启人声自动闪避（ducking）。

---

## 第四步：目检验收与交付

1. 用 `read_file` 打开自动生成的 `<作品名>_动态漫_preview_grid.jpg` 拼图，逐格检查：
   - 所有漫画原画是否 100% 完整无畸变？
   - 所有对话气泡、旁白框和拟声词是否完整无裁切？
   - 氛围特效是否自然克制、不遮挡文字？
2. 用 `ffprobe` 核验输出 MP4 的分辨率、帧率、音视频流与总时长。
3. 交付成品 MP4、`storyboard.json`（方便用户后续微调台词、换声线或增减特效重渲染）及一份简明的配音与特效清单。
