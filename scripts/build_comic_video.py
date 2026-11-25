#!/usr/bin/env python3
"""
comic-to-video: Zero-alteration comic-to-video rendering engine.
Preserves 100% of original comic artwork pixels while adding:
  - Panel-focused framing & Ken Burns camera motion (zoom/pan/tilt/shake)
  - Blurred-artwork immersive canvas framing (never stretches or crops speech bubbles)
  - Multi-voice Chinese neural TTS (edge-tts with macOS `say` fallback)
  - Non-destructive procedural film/atmosphere overlays (rain, candle flicker, steam, sparks, sunbeams, film grain, vignette)
  - Procedural SFX & gentle pentatonic ambient BGM (or external --bgm)
  - Smooth crossfade / fade-to-black / fade-to-white / hard-cut transitions

Usage:
  uv run --with edge-tts --with pillow --with numpy python3 build_comic_video.py \
    --storyboard storyboard.json \
    --output output.mp4 \
    [--aspect 16:9|9:16] \
    [--bgm custom.mp3] \
    [--save-preview-frames]
"""

import argparse
import asyncio
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Voice presets (Chinese Neural TTS via edge-tts)
# ---------------------------------------------------------------------------
VOICE_PRESETS = {
    "narrator": ("zh-CN-YunyangNeural", "-4%", "-2Hz"),      # 沉稳叙事旁白
    "youth": ("zh-CN-YunxiNeural", "-2%", "+0Hz"),           # 清朗青年男声
    "youth_excited": ("zh-CN-YunxiNeural", "+6%", "+4Hz"),   # 激动青年男声
    "uncle": ("zh-CN-YunjianNeural", "-4%", "-4Hz"),         # 浑厚豪爽大叔/乡亲
    "uncle_cheer": ("zh-CN-YunjianNeural", "+4%", "+2Hz"),   # 喜庆洪亮大叔
    "female": ("zh-CN-XiaoxiaoNeural", "+0%", "+0Hz"),       # 自然温暖女声
    "female_lively": ("zh-CN-XiaoyiNeural", "+4%", "+2Hz"),  # 活泼女声
    "elder": ("zh-CN-YunxiaNeural", "-8%", "-6Hz"),          # 苍老/回忆
}

SR = 44100  # Audio sample rate


# ---------------------------------------------------------------------------
# Audio helpers (WAV I/O, TTS, procedural SFX & gentle pentatonic BGM)
# ---------------------------------------------------------------------------
def read_audio_to_mono_f32(path: str, sr: int = SR) -> np.ndarray:
    """Decode any audio file to mono float32 numpy array via ffmpeg."""
    cmd = [
        "ffmpeg", "-y", "-i", path,
        "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
    if not res.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(res.stdout, dtype=np.float32).copy()


def write_wav_stereo(path: str, left: np.ndarray, right: np.ndarray, sr: int = SR):
    """Write float32 stereo [-1, 1] arrays to 16-bit PCM WAV."""
    n = min(len(left), len(right))
    stereo = np.stack([left[:n], right[:n]], axis=1)
    peak = float(np.max(np.abs(stereo))) if n > 0 else 0.0
    if peak > 0.96:
        stereo = stereo * (0.96 / peak)
    pcm = np.clip(stereo * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


async def synthesize_tts_edge(text: str, voice: str, rate: str, pitch: str, out_mp3: str):
    import edge_tts
    comm = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
    await comm.save(out_mp3)


def synthesize_line(line: dict, tmp_dir: str, idx: int) -> np.ndarray:
    """Synthesize one dialogue/narration line to mono float32 audio."""
    text = line["text"].strip()
    if not text:
        return np.zeros(0, dtype=np.float32)

    role = line.get("role", "narrator")
    default_voice, default_rate, default_pitch = VOICE_PRESETS.get(
        role, VOICE_PRESETS["narrator"]
    )
    voice = line.get("voice", default_voice)
    rate = line.get("rate", default_rate)
    pitch = line.get("pitch", default_pitch)

    out_mp3 = os.path.join(tmp_dir, f"tts_{idx:03d}.mp3")
    try:
        asyncio.run(synthesize_tts_edge(text, voice, rate, pitch, out_mp3))
        audio = read_audio_to_mono_f32(out_mp3)
    except Exception as exc:
        print(f"[warn] edge-tts failed ({exc}), falling back to macOS say", file=sys.stderr)
        out_aiff = os.path.join(tmp_dir, f"tts_{idx:03d}.aiff")
        subprocess.run(["say", "-v", "Tingting", "-o", out_aiff, text], check=True)
        audio = read_audio_to_mono_f32(out_aiff)

    # Trim leading/trailing near-silence gently so timing feels snappy
    if len(audio) > 0:
        thresh = 0.008
        nz = np.where(np.abs(audio) > thresh)[0]
        if len(nz) > 0:
            s = max(0, int(nz[0]) - int(0.04 * SR))
            e = min(len(audio), int(nz[-1]) + int(0.08 * SR))
            audio = audio[s:e]
        # Normalize speech loudness cleanly
        rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) > 0 else 0.0
        if rms > 1e-5:
            target_rms = 0.22
            audio = np.clip(audio * (target_rms / rms), -0.95, 0.95)
    return audio


def synth_sfx(sfx_name: str, duration: float, sr: int = SR) -> np.ndarray:
    """Generate clean procedural SFX (mono float32)."""
    n = max(1, int(duration * sr))
    t = np.linspace(0, duration, n, endpoint=False, dtype=np.float32)
    rng = np.random.default_rng(abs(hash(sfx_name)) % (2**31))

    if sfx_name == "rain_ambience":
        # Soft pink-ish rain noise with gentle fade in/out
        white = rng.normal(0, 1, n).astype(np.float32)
        # Simple lowpass moving average for warm rain texture
        kernel = np.ones(18, dtype=np.float32) / 18.0
        rain = np.convolve(white, kernel, mode="same")
        env = np.minimum(1.0, t / 0.6) * np.minimum(1.0, (duration - t) / 0.6)
        return (rain * env * 0.14).astype(np.float32)

    if sfx_name == "sizzle":
        # Warm frying sizzle / morning street ambience
        white = rng.normal(0, 1, n).astype(np.float32)
        # Band-like texture
        k1 = np.ones(4, dtype=np.float32) / 4.0
        k2 = np.ones(28, dtype=np.float32) / 28.0
        band = np.convolve(white, k1, mode="same") - np.convolve(white, k2, mode="same")
        mod = 0.75 + 0.25 * np.sin(2 * np.pi * 2.3 * t)
        env = np.minimum(1.0, t / 0.5) * np.minimum(1.0, (duration - t) / 0.5)
        return (band * mod * env * 0.16).astype(np.float32)

    if sfx_name == "bottle_open":
        out = np.zeros(n, dtype=np.float32)
        # Metallic twist + crisp pop + faint hiss
        pop_t = min(0.45, duration * 0.35)
        idx0 = int(pop_t * sr)
        pop_len = int(0.09 * sr)
        if idx0 + pop_len < n:
            tp = np.linspace(0, 0.09, pop_len, dtype=np.float32)
            pop = np.sin(2 * np.pi * (420 + 900 * np.exp(-tp * 55)) * tp) * np.exp(-tp * 42)
            out[idx0:idx0 + pop_len] += pop * 0.55
        hiss_start = idx0 + int(0.03 * sr)
        hiss_len = min(int(0.45 * sr), n - hiss_start)
        if hiss_len > 0:
            th = np.linspace(0, hiss_len / sr, hiss_len, dtype=np.float32)
            hiss = rng.normal(0, 1, hiss_len).astype(np.float32) * np.exp(-th * 6.5) * 0.12
            out[hiss_start:hiss_start + hiss_len] += hiss
        return out

    if sfx_name == "water_drop":
        out = np.zeros(n, dtype=np.float32)
        drop_t = min(0.55, duration * 0.45)
        idx0 = int(drop_t * sr)
        dlen = int(0.28 * sr)
        if idx0 + dlen < n:
            td = np.linspace(0, 0.28, dlen, dtype=np.float32)
            # Classic rising chirp of a liquid drop
            freq = 680 + 980 * (1.0 - np.exp(-td * 24.0))
            phase = 2 * np.pi * np.cumsum(freq) / sr
            drop = np.sin(phase) * np.exp(-td * 18.0) * 0.45
            out[idx0:idx0 + dlen] += drop
        return out

    if sfx_name == "firecracker":
        out = np.zeros(n, dtype=np.float32)
        # Rapid crackles followed by a celebratory low boom
        num_pops = max(12, int(duration * 14))
        for _ in range(num_pops):
            pos = int(rng.uniform(0.15, max(0.2, duration - 0.7)) * sr)
            plen = int(rng.uniform(0.015, 0.045) * sr)
            if pos + plen < n:
                tp = np.linspace(0, plen / sr, plen, dtype=np.float32)
                crack = rng.normal(0, 1, plen).astype(np.float32) * np.exp(-tp * 90)
                amp = rng.uniform(0.18, 0.42)
                out[pos:pos + plen] += crack * amp
        # Big "砰！" boom around 65% of clip
        bpos = int(min(duration - 0.5, duration * 0.68) * sr)
        blen = min(int(0.45 * sr), n - bpos)
        if bpos >= 0 and blen > 0:
            tb = np.linspace(0, blen / sr, blen, dtype=np.float32)
            boom = (
                np.sin(2 * np.pi * (110 * np.exp(-tb * 7.0) + 38) * tb) * np.exp(-tb * 7.5) * 0.6
                + rng.normal(0, 1, blen).astype(np.float32) * np.exp(-tb * 18.0) * 0.28
            )
            out[bpos:bpos + blen] += boom
        return out

    if sfx_name == "chime_dream":
        # Ethereal warm harmonic bell pad
        freqs = [523.25, 659.25, 783.99, 1046.5]
        out = np.zeros(n, dtype=np.float32)
        for i, f in enumerate(freqs):
            delay = i * 0.12
            mask = t >= delay
            td = np.maximum(0.0, t - delay)
            out += mask * np.sin(2 * np.pi * f * td) * np.exp(-td * 1.4) * (0.14 / (i + 1))
        return out

    if sfx_name == "whoosh":
        white = rng.normal(0, 1, n).astype(np.float32)
        k = np.ones(24, dtype=np.float32) / 24.0
        soft = np.convolve(white, k, mode="same")
        center = duration * 0.45
        env = np.exp(-((t - center) / max(0.15, duration * 0.25)) ** 2)
        return (soft * env * 0.22).astype(np.float32)

    return np.zeros(n, dtype=np.float32)


def synth_pentatonic_bgm(total_duration: float, mood: str = "nostalgic", sr: int = SR) -> tuple[np.ndarray, np.ndarray]:
    """Generate a gentle, cinema-like Chinese pentatonic ambient pad + soft pluck BGM."""
    n = max(1, int(total_duration * sr))
    left = np.zeros(n, dtype=np.float32)
    right = np.zeros(n, dtype=np.float32)

    # D major pentatonic: D, E, F#, A, B (warm, historic, uplifting)
    chords = [
        [146.83, 220.00, 293.66, 369.99],  # Dsus/D
        [123.47, 185.00, 246.94, 329.63],  # Bm7
        [110.00, 164.81, 220.00, 293.66],  # Asus4
        [146.83, 220.00, 329.63, 440.00],  # Dadd9
    ]
    melody_notes = [587.33, 659.25, 739.99, 880.00, 739.99, 659.25, 587.33, 440.00]
    chord_dur = 4.5
    t_cur = 0.0
    ci = 0
    while t_cur < total_duration:
        seg_dur = min(chord_dur, total_duration - t_cur)
        s0 = int(t_cur * sr)
        s1 = min(n, s0 + int(seg_dur * sr))
        slen = s1 - s0
        if slen <= 0:
            break
        ts = np.linspace(0, slen / sr, slen, endpoint=False, dtype=np.float32)
        # Smooth bell-shaped pad envelope
        env = np.sin(np.pi * ts / max(seg_dur, 0.1)) ** 0.8
        chord = chords[ci % len(chords)]
        pad_l = np.zeros(slen, dtype=np.float32)
        pad_r = np.zeros(slen, dtype=np.float32)
        for j, freq in enumerate(chord):
            detune = 1.0 + (0.0018 if j % 2 == 0 else -0.0018)
            wave_l = np.sin(2 * np.pi * freq * ts) + 0.25 * np.sin(4 * np.pi * freq * ts)
            wave_r = np.sin(2 * np.pi * (freq * detune) * ts) + 0.25 * np.sin(4 * np.pi * (freq * detune) * ts)
            w = 0.22 / len(chord)
            pad_l += wave_l * w
            pad_r += wave_r * w

        # Soft guzheng-like pluck on beat 0 and beat 2.25
        for beat_offset, note_idx in [(0.2, ci * 2), (2.4, ci * 2 + 1)]:
            if beat_offset < seg_dur:
                p0 = int(beat_offset * sr)
                plen = slen - p0
                tp = np.linspace(0, plen / sr, plen, endpoint=False, dtype=np.float32)
                nf = melody_notes[note_idx % len(melody_notes)]
                pluck = (
                    np.sin(2 * np.pi * nf * tp)
                    + 0.35 * np.sin(2 * np.pi * nf * 2 * tp) * np.exp(-tp * 3.0)
                ) * np.exp(-tp * 2.1) * 0.09
                pan = 0.62 if (note_idx % 2 == 0) else 0.38
                pad_l[p0:] += pluck * pan
                pad_r[p0:] += pluck * (1.0 - pan)

        left[s0:s1] += pad_l * env
        right[s0:s1] += pad_r * env
        t_cur += chord_dur
        ci += 1

    # Global gentle fade in/out
    t_all = np.linspace(0, total_duration, n, endpoint=False, dtype=np.float32)
    g_env = np.minimum(1.0, t_all / 1.5) * np.minimum(1.0, (total_duration - t_all) / 2.2)
    return (left * g_env * 0.55).astype(np.float32), (right * g_env * 0.55).astype(np.float32)


# ---------------------------------------------------------------------------
# Visual framing & non-destructive effects
# ---------------------------------------------------------------------------
def ease_in_out_cubic(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return 4 * p * p * p if p < 0.5 else 1 - ((-2 * p + 2) ** 3) / 2


def ease_out_quad(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return 1 - (1 - p) * (1 - p)


def find_cjk_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Songti.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def build_base_canvas(img: Image.Image, crop_norm: list[float], out_w: int, out_h: int,
                      overscan: float = 1.10, bg_style: str = "blur") -> Image.Image:
    """
    Crop the exact panel (including any protruding speech bubbles) without altering
    original artwork pixels, and place it onto an overscanned immersive canvas
    so Ken Burns camera motion never reveals black edges or clips bubbles.
    """
    iw, ih = img.size
    cx, cy, cw, ch = crop_norm
    x0 = max(0, int(round(cx * iw)))
    y0 = max(0, int(round(cy * ih)))
    x1 = min(iw, int(round((cx + cw) * iw)))
    y1 = min(ih, int(round((cy + ch) * ih)))
    panel = img.crop((x0, y0, x1, y1)).convert("RGB")
    pw, ph = panel.size

    cw_px = int(round(out_w * overscan))
    ch_px = int(round(out_h * overscan))

    # Build blurred atmospheric background from the same panel
    if bg_style == "dark":
        canvas = Image.new("RGB", (cw_px, ch_px), (18, 18, 20))
    else:
        scale_bg = max(cw_px / pw, ch_px / ph)
        bg = panel.resize((int(pw * scale_bg) + 2, int(ph * scale_bg) + 2), Image.Resampling.BILINEAR)
        bx0 = (bg.width - cw_px) // 2
        by0 = (bg.height - ch_px) // 2
        bg = bg.crop((bx0, by0, bx0 + cw_px, by0 + ch_px))
        bg = bg.filter(ImageFilter.GaussianBlur(radius=34))
        bg_arr = (np.asarray(bg, dtype=np.float32) * 0.30).astype(np.uint8)
        canvas = Image.fromarray(bg_arr, mode="RGB")

    # Fit panel cleanly inside safe inner box (preserve 100% original aspect ratio)
    # Keep ~92% of target viewport height/width so motion never clips text
    safe_w = int(round(out_w * 0.94))
    safe_h = int(round(out_h * 0.94))
    scale = min(safe_w / pw, safe_h / ph)
    new_w = max(1, int(round(pw * scale)))
    new_h = max(1, int(round(ph * scale)))
    panel_resized = panel.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # Subtle drop shadow behind the panel
    px = (cw_px - new_w) // 2
    py = (ch_px - new_h) // 2
    shadow = Image.new("RGBA", (cw_px, ch_px), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rectangle([px - 4, py + 8, px + new_w + 4, py + new_h + 14], fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=18))

    canvas_rgba = canvas.convert("RGBA")
    canvas_rgba = Image.alpha_composite(canvas_rgba, shadow)
    canvas_rgba.paste(panel_resized, (px, py))
    return canvas_rgba.convert("RGB")


def make_vignette_mask(w: int, h: int, strength: float = 0.28) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    nx = (xx - w * 0.5) / (w * 0.5)
    ny = (yy - h * 0.5) / (h * 0.5)
    r2 = np.clip(nx * nx + ny * ny, 0.0, 2.0)
    mask = 1.0 - strength * np.clip((r2 - 0.45) / 1.35, 0.0, 1.0)
    return mask[:, :, None].astype(np.float32)


def make_sunbeam_layer(w: int, h: int) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    nx = xx / w
    ny = yy / h
    # Soft diagonal warm morning rays from top-left
    diag = (nx * 0.72 + ny * 0.55)
    ray1 = np.exp(-((nx * 0.8 - ny * 0.65 - 0.02) ** 2) / 0.022)
    ray2 = np.exp(-((nx * 0.8 - ny * 0.65 - 0.22) ** 2) / 0.030)
    falloff = np.clip(1.15 - diag * 1.05, 0.0, 1.0)
    intensity = np.clip((ray1 * 0.7 + ray2 * 0.5) * falloff, 0.0, 1.0)
    layer = np.zeros((h, w, 3), dtype=np.float32)
    layer[:, :, 0] = intensity * 52.0
    layer[:, :, 1] = intensity * 40.0
    layer[:, :, 2] = intensity * 18.0
    return layer


def apply_effects(frame: np.ndarray, effects: list[str], t: float, dur: float,
                  vignette_mask: np.ndarray, sunbeam_layer: np.ndarray,
                  seed: int = 42) -> np.ndarray:
    """Apply subtle, non-destructive cinema overlays without altering comic art."""
    if not effects:
        return frame
    out = frame.astype(np.float32)
    h, w, _ = out.shape
    rng = np.random.default_rng(seed + int(t * 30))

    if "candle_flicker" in effects:
        # Gentle warm lamp breathing glow
        flicker = 1.0 + 0.028 * math.sin(t * 7.5) + 0.018 * math.sin(t * 17.3 + 1.1)
        out[:, :, 0] *= (1.0 + (flicker - 1.0) * 1.15)
        out[:, :, 1] *= (1.0 + (flicker - 1.0) * 0.85)
        out[:, :, 2] *= (1.0 + (flicker - 1.0) * 0.35)

    if "sunbeams" in effects:
        pulse = 0.78 + 0.22 * math.sin(t * 1.1)
        out += sunbeam_layer * pulse

    if "steam" in effects:
        # Soft translucent rising warm mist wisps in lower-mid region
        yy = np.linspace(0, 1, h, dtype=np.float32)[:, None]
        xx = np.linspace(0, 1, w, dtype=np.float32)[None, :]
        wisp = (
            np.sin(xx * 9.0 + yy * 3.0 + t * 1.6) * 0.5 + 0.5
        ) * (
            np.sin(xx * 15.0 - yy * 4.5 + t * 2.3) * 0.5 + 0.5
        )
        vert_mask = np.clip(1.0 - np.abs(yy - 0.52) / 0.42, 0.0, 1.0) ** 1.6
        mist = wisp * vert_mask * 16.0
        out[:, :, 0] += mist * 1.05
        out[:, :, 1] += mist * 0.98
        out[:, :, 2] += mist * 0.88

    if "rain" in effects:
        # Fine slanted rain streaks
        overlay = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(overlay)
        n_drops = 65
        for i in range(n_drops):
            # Deterministic streak trajectories moving down-left
            bx = ((i * 197 + int(t * 210)) % (w + 180)) - 90
            by = ((i * 131 + int(t * 980)) % (h + 180)) - 90
            length = 26 + (i % 24)
            draw.line([(bx, by), (bx - length * 0.22, by + length)], fill=120, width=1)
        rain_arr = np.asarray(overlay, dtype=np.float32)[:, :, None] * 0.32
        out += rain_arr

    if "sparks" in effects:
        # Festive red-gold sparks floating up/outward (e.g., firecrackers)
        overlay = Image.new("RGB", (w, h), (0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for i in range(36):
            phase = (t * 0.85 + i * 0.19) % 1.0
            sx = int((0.18 + 0.64 * ((i * 37 % 100) / 100.0)) * w + math.sin(t * 4 + i) * 18)
            sy = int((0.88 - phase * 0.55) * h)
            r = 2 + (i % 3)
            fade = math.sin(phase * math.pi)
            c_r = int(255 * fade)
            c_g = int((140 + (i % 90)) * fade)
            c_b = int(40 * fade)
            draw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(c_r, c_g, c_b))
        sp_arr = np.asarray(overlay, dtype=np.float32) * 0.72
        out += sp_arr

    if "film_grain" in effects:
        # Subtle vintage film grain
        noise = rng.normal(0.0, 4.2, (h // 2, w // 2, 1)).astype(np.float32)
        noise = np.repeat(np.repeat(noise, 2, axis=0), 2, axis=1)
        out += noise

    if "vignette" in effects:
        out *= vignette_mask

    return np.clip(out, 0, 255).astype(np.uint8)


def render_camera_frame(canvas: Image.Image, out_w: int, out_h: int,
                        motion: str, p: float, t: float, effects: list[str]) -> np.ndarray:
    """Extract viewport from overscanned canvas with smooth Ken Burns & optional shake."""
    cw, ch = canvas.size
    e = ease_in_out_cubic(p)

    # Default subtle zoom-in
    zoom = 1.0
    dx_norm = 0.0  # -1..1 of available slack
    dy_norm = 0.0

    if motion == "zoom_in":
        zoom = 1.00 + 0.065 * e
    elif motion == "zoom_out":
        zoom = 1.065 - 0.065 * e
    elif motion == "impact_zoom":
        # Faster initial push then gentle settle
        zoom = 1.00 + 0.075 * ease_out_quad(p)
    elif motion == "pan_right":
        zoom = 1.045
        dx_norm = -0.65 + 1.30 * e
    elif motion == "pan_left":
        zoom = 1.045
        dx_norm = 0.65 - 1.30 * e
    elif motion == "tilt_down":
        zoom = 1.045
        dy_norm = -0.65 + 1.30 * e
    elif motion == "tilt_up":
        zoom = 1.045
        dy_norm = 0.65 - 1.30 * e
    elif motion == "static":
        zoom = 1.00

    # Viewport size in canvas coordinates
    vw = out_w / zoom
    vh = out_h / zoom
    slack_x = max(0.0, (cw - vw) * 0.5)
    slack_y = max(0.0, (ch - vh) * 0.5)

    cx = cw * 0.5 + dx_norm * slack_x * 0.55
    cy = ch * 0.5 + dy_norm * slack_y * 0.55

    if "shake" in effects:
        # Energetic micro camera shake (e.g. firecrackers / bang)
        amp = 7.5 * (0.45 + 0.55 * math.sin(p * math.pi))
        cx += math.sin(t * 48.0) * amp
        cy += math.cos(t * 59.0) * amp * 0.8

    x0 = max(0.0, min(cw - vw, cx - vw * 0.5))
    y0 = max(0.0, min(ch - vh, cy - vh * 0.5))

    cropped = canvas.crop((x0, y0, x0 + vw, y0 + vh))
    resized = cropped.resize((out_w, out_h), Image.Resampling.BILINEAR)
    return np.asarray(resized, dtype=np.uint8)


def render_title_card(title: str, subtitle: str, out_w: int, out_h: int,
                      bg_img_path: str | None = None) -> Image.Image:
    """Create a restrained, cinematic title/end card."""
    if bg_img_path and os.path.exists(bg_img_path):
        bg = Image.open(bg_img_path).convert("RGB")
        scale = max(out_w / bg.width, out_h / bg.height)
        bg = bg.resize((int(bg.width * scale) + 2, int(bg.height * scale) + 2), Image.Resampling.BILINEAR)
        x0 = (bg.width - out_w) // 2
        y0 = (bg.height - out_h) // 2
        bg = bg.crop((x0, y0, x0 + out_w, y0 + out_h)).filter(ImageFilter.GaussianBlur(radius=30))
        arr = (np.asarray(bg, dtype=np.float32) * 0.24).astype(np.uint8)
        card = Image.fromarray(arr, mode="RGB")
    else:
        card = Image.new("RGB", (out_w, out_h), (16, 15, 14))

    draw = ImageDraw.Draw(card)
    f_title = find_cjk_font(int(out_h * 0.088))
    f_sub = find_cjk_font(int(out_h * 0.032))

    bbox = draw.textbbox((0, 0), title, font=f_title)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (out_w - tw) // 2
    ty = int(out_h * 0.43) - th // 2

    # Warm gold accent line
    line_w = min(int(tw * 0.65), int(out_w * 0.25))
    draw.rectangle(
        [(out_w - line_w) // 2, ty + th + 24, (out_w + line_w) // 2, ty + th + 27],
        fill=(212, 175, 112)
    )
    draw.text((tx, ty), title, font=f_title, fill=(246, 240, 228))

    if subtitle:
        sb = draw.textbbox((0, 0), subtitle, font=f_sub)
        sw = sb[2] - sb[0]
        draw.text(((out_w - sw) // 2, ty + th + 48), subtitle, font=f_sub, fill=(198, 190, 176))
    return card


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def build_video(storyboard_path: str, output_path: str, aspect: str = "16:9",
                bgm_path: str | None = None, save_previews: bool = False):
    with open(storyboard_path, "r", encoding="utf-8") as f:
        sb = json.load(f)

    fps = int(sb.get("fps", 30))
    if aspect == "9:16":
        out_w, out_h = 1080, 1920
    else:
        out_w, out_h = 1920, 1080

    base_dir = Path(storyboard_path).resolve().parent
    shots_cfg = sb["shots"]

    with tempfile.TemporaryDirectory(prefix="comic_vid_") as tmp_dir:
        print(f"[1/4] Synthesizing multi-role voiceovers ({len(shots_cfg)} shots)...")
        tts_counter = 0
        timeline = []

        # Optional title card
        if sb.get("title_card"):
            tc = sb["title_card"]
            first_img = str((base_dir / shots_cfg[0]["image"]).resolve())
            timeline.append({
                "type": "card",
                "title": tc.get("title", sb.get("title", "")),
                "subtitle": tc.get("subtitle", ""),
                "bg_image": first_img,
                "duration": float(tc.get("duration", 2.4)),
                "transition_in": "fadeblack",
                "transition_out": "fadeblack",
                "sfx": ["chime_dream"],
                "voice_segments": [],
            })

        # Prepare each comic shot & audio-driven duration
        for s_idx, shot in enumerate(shots_cfg):
            img_path = Path(shot["image"])
            if not img_path.is_absolute():
                img_path = (base_dir / img_path).resolve()
            if not img_path.exists():
                raise FileNotFoundError(f"Comic image not found: {img_path}")

            pad_before = float(shot.get("pad_before", 0.40))
            pad_after = float(shot.get("pad_after", 0.55))
            cursor = pad_before
            voice_segments = []

            for line in shot.get("lines", []):
                audio = synthesize_line(line, tmp_dir, tts_counter)
                tts_counter += 1
                voice_segments.append((cursor, audio))
                cursor += len(audio) / SR + float(line.get("delay_after", 0.35))

            if voice_segments:
                # Remove last delay_after and add pad_after
                last_delay = float(shot["lines"][-1].get("delay_after", 0.35))
                auto_dur = cursor - last_delay + pad_after
            else:
                auto_dur = float(shot.get("duration", 2.8))

            dur = max(auto_dur, float(shot.get("min_duration", 1.8)))
            timeline.append({
                "type": "shot",
                "id": shot.get("id", f"shot_{s_idx+1:02d}"),
                "image": str(img_path),
                "crop": shot.get("crop", [0.0, 0.0, 1.0, 1.0]),
                "motion": shot.get("motion", "zoom_in"),
                "effects": shot.get("effects", ["vignette"]),
                "sfx": shot.get("sfx", []),
                "bg_style": shot.get("bg_style", "blur"),
                "transition_in": shot.get("transition_in", "fade"),
                "transition_out": shot.get("transition_out", "fade"),
                "duration": dur,
                "voice_segments": voice_segments,
            })

        # Optional end card
        if sb.get("end_card"):
            ec = sb["end_card"]
            last_img = str((base_dir / shots_cfg[-1]["image"]).resolve())
            timeline.append({
                "type": "card",
                "title": ec.get("title", sb.get("title", "")),
                "subtitle": ec.get("subtitle", ""),
                "bg_image": last_img,
                "duration": float(ec.get("duration", 2.6)),
                "transition_in": "fadeblack",
                "transition_out": "fadeblack",
                "sfx": [],
                "voice_segments": [],
            })

        total_duration = sum(item["duration"] for item in timeline)
        total_frames = sum(max(1, int(round(item["duration"] * fps))) for item in timeline)
        print(f"[2/4] Mixing master audio track ({total_duration:.2f}s total)...")

        n_samples = int(math.ceil(total_duration * SR)) + SR
        # Build voice track (mono -> stereo centered) and SFX track
        voice_mono = np.zeros(n_samples, dtype=np.float32)
        sfx_mono = np.zeros(n_samples, dtype=np.float32)

        t_cursor = 0.0
        for item in timeline:
            dur = item["duration"]
            for offset_s, v_arr in item["voice_segments"]:
                start_i = int((t_cursor + offset_s) * SR)
                end_i = min(n_samples, start_i + len(v_arr))
                if end_i > start_i:
                    voice_mono[start_i:end_i] += v_arr[:end_i - start_i]
            for sfx_name in item.get("sfx", []):
                s_arr = synth_sfx(sfx_name, dur, SR)
                start_i = int(t_cursor * SR)
                end_i = min(n_samples, start_i + len(s_arr))
                if end_i > start_i:
                    sfx_mono[start_i:end_i] += s_arr[:end_i - start_i]
            t_cursor += dur

        # BGM track with smooth auto-ducking under voice
        if bgm_path and os.path.exists(bgm_path):
            bgm_m = read_audio_to_mono_f32(bgm_path, SR)
            if len(bgm_m) > 0:
                reps = int(math.ceil(n_samples / len(bgm_m)))
                bgm_m = np.tile(bgm_m, reps)[:n_samples] * 0.22
            else:
                bgm_m = np.zeros(n_samples, dtype=np.float32)
            bgm_l, bgm_r = bgm_m.copy(), bgm_m.copy()
        else:
            bgm_l, bgm_r = synth_pentatonic_bgm(n_samples / SR, sb.get("bgm_mood", "nostalgic"), SR)
            bgm_l = bgm_l[:n_samples]
            bgm_r = bgm_r[:n_samples]

        # Compute voice envelope for smooth BGM ducking
        win = int(0.18 * SR)
        v_abs = np.abs(voice_mono)
        # Fast block-based envelope
        step = 1024
        v_blocks = np.array([v_abs[i:i + step].max() for i in range(0, n_samples, step)], dtype=np.float32)
        k_smooth = np.ones(12, dtype=np.float32) / 12.0
        v_env_b = np.convolve(v_blocks, k_smooth, mode="same")
        v_env = np.repeat(v_env_b, step)[:n_samples]
        duck = np.where(v_env > 0.03, 0.42, 1.0).astype(np.float32)

        # Guard against a one-sample length mismatch between procedural BGM and
        # the voice/sfx tracks caused by float rounding of total_duration.
        bgm_l = np.pad(bgm_l, (0, max(0, len(voice_mono) - len(bgm_l))))[:len(voice_mono)]
        bgm_r = np.pad(bgm_r, (0, max(0, len(voice_mono) - len(bgm_r))))[:len(voice_mono)]
        master_l = voice_mono * 1.05 + sfx_mono * 0.85 + bgm_l * duck * 0.42
        master_r = voice_mono * 1.05 + sfx_mono * 0.85 + bgm_r * duck * 0.42

        audio_wav = os.path.join(tmp_dir, "master_audio.wav")
        write_wav_stereo(audio_wav, master_l[:int(total_duration * SR)], master_r[:int(total_duration * SR)], SR)

        # Render video frames directly into ffmpeg stdin
        print(f"[3/4] Rendering {total_frames} frames ({out_w}x{out_h} @ {fps}fps)...")
        out_path = Path(output_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{out_w}x{out_h}",
            "-r", str(fps),
            "-i", "pipe:0",
            "-i", audio_wav,
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-shortest",
            str(out_path),
        ]
        proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

        vignette_mask = make_vignette_mask(out_w, out_h, strength=0.26)
        sunbeam_layer = make_sunbeam_layer(out_w, out_h)
        img_cache: dict[str, Image.Image] = {}
        preview_thumbs: list[tuple[str, Image.Image]] = []

        for idx, item in enumerate(timeline):
            dur = item["duration"]
            n_frames = max(1, int(round(dur * fps)))
            t_in = item.get("transition_in", "fade")
            t_out = item.get("transition_out", "fade")
            fade_in_f = int(0.45 * fps) if t_in in ("fade", "fadeblack", "fadewhite") else 0
            fade_out_f = int(0.45 * fps) if t_out in ("fade", "fadeblack", "fadewhite") else 0

            if item["type"] == "card":
                card_img = render_title_card(item["title"], item["subtitle"], out_w, out_h, item.get("bg_image"))
                base_arr = np.asarray(card_img, dtype=np.uint8)
                for f_i in range(n_frames):
                    frame = base_arr.copy()
                    if fade_in_f > 0 and f_i < fade_in_f:
                        a = f_i / fade_in_f
                        frame = (frame.astype(np.float32) * a).astype(np.uint8)
                    elif fade_out_f > 0 and f_i >= n_frames - fade_out_f:
                        a = (n_frames - 1 - f_i) / fade_out_f
                        frame = (frame.astype(np.float32) * a).astype(np.uint8)
                    proc.stdin.write(frame.tobytes())
                    if save_previews and f_i == n_frames // 2:
                        preview_thumbs.append((item["title"], Image.fromarray(frame)))
            else:
                img_path = item["image"]
                if img_path not in img_cache:
                    img_cache[img_path] = Image.open(img_path).convert("RGB")
                canvas = build_base_canvas(
                    img_cache[img_path], item["crop"], out_w, out_h,
                    overscan=1.10, bg_style=item.get("bg_style", "blur")
                )
                effects = item.get("effects", [])
                motion = item.get("motion", "zoom_in")

                for f_i in range(n_frames):
                    p = f_i / max(1, n_frames - 1)
                    t_local = f_i / fps
                    frame = render_camera_frame(canvas, out_w, out_h, motion, p, t_local, effects)
                    frame = apply_effects(frame, effects, t_local, dur, vignette_mask, sunbeam_layer, seed=100 + idx * 17)

                    # Apply smooth transition in/out
                    if fade_in_f > 0 and f_i < fade_in_f:
                        a = f_i / fade_in_f
                        if t_in == "fadewhite":
                            frame = (frame.astype(np.float32) * a + 255.0 * (1.0 - a)).astype(np.uint8)
                        else:
                            frame = (frame.astype(np.float32) * a).astype(np.uint8)
                    elif fade_out_f > 0 and f_i >= n_frames - fade_out_f:
                        a = (n_frames - 1 - f_i) / fade_out_f
                        if t_out == "fadewhite":
                            frame = (frame.astype(np.float32) * a + 255.0 * (1.0 - a)).astype(np.uint8)
                        else:
                            frame = (frame.astype(np.float32) * a).astype(np.uint8)

                    proc.stdin.write(frame.tobytes())
                    if save_previews and f_i == n_frames // 2:
                        preview_thumbs.append((item["id"], Image.fromarray(frame)))

        proc.stdin.close()
        stderr_out = proc.stderr.read().decode("utf-8", errors="ignore")
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(f"FFmpeg failed (code {ret}):\n{stderr_out[-1200:]}")

        print(f"[4/4] Done! Video saved to: {out_path}")

        # Save contact sheet of midpoint frames for quick visual QA
        if save_previews and preview_thumbs:
            cols = 4
            rows = math.ceil(len(preview_thumbs) / cols)
            tw, th = 480, int(480 * out_h / out_w)
            sheet = Image.new("RGB", (cols * tw, rows * th), (24, 24, 24))
            draw = ImageDraw.Draw(sheet)
            font = find_cjk_font(18)
            for i, (label, thumb) in enumerate(preview_thumbs):
                r, c = divmod(i, cols)
                resized = thumb.resize((tw, th), Image.Resampling.LANCZOS)
                sheet.paste(resized, (c * tw, r * th))
                draw.rectangle([c * tw + 6, r * th + 6, c * tw + 180, r * th + 32], fill=(0, 0, 0))
                draw.text((c * tw + 12, r * th + 8), str(label), font=font, fill=(255, 230, 160))
            grid_path = out_path.with_name(out_path.stem + "_preview_grid.jpg")
            sheet.save(grid_path, quality=88)
            print(f"Preview contact sheet saved to: {grid_path}")


def main():
    parser = argparse.ArgumentParser(description="Zero-alteration comic-to-video renderer")
    parser.add_argument("--storyboard", required=True, help="Path to storyboard.json")
    parser.add_argument("--output", required=True, help="Output MP4 path")
    parser.add_argument("--aspect", choices=["16:9", "9:16"], default="16:9", help="Output video aspect ratio")
    parser.add_argument("--bgm", default=None, help="Optional custom BGM audio file")
    parser.add_argument("--save-preview-frames", action="store_true", help="Export midpoint preview contact sheet")
    args = parser.parse_args()

    build_video(
        storyboard_path=args.storyboard,
        output_path=args.output,
        aspect=args.aspect,
        bgm_path=args.bgm,
        save_previews=args.save_preview_frames,
    )


if __name__ == "__main__":
    main()
