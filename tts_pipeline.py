#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
文章转语音 (TTS) 自动化管线
=============================
流程：
  1. 使用 LLM API 将文本分段（每段 ≤ 200 字，播客朗读风格）
  2. 调用 ComfyUI API（CosyVoice3 / xiaoying-read）逐段生成语音
  3. 使用 FFmpeg 合并分段音频，段间添加 1 秒停顿，并转换为 320Kbps 立体声 MP3

配置方式：
  在项目根目录下的 .env 文件中配置（也可通过系统环境变量传入）：
  - LLM_API_BASE: LLM API 基地址（如 https://api.openai.com/v1）
  - LLM_API_KEY:  LLM API 密钥
  - LLM_MODEL:    模型名称（默认 gpt-4o-mini）
  - COMFYUI_URL:  ComfyUI 地址（默认 http://127.0.0.1:8188）

用法：
  python tts_pipeline.py                          # 处理 input/ 下所有 txt (生成 320k 立体声)
  python tts_pipeline.py --file 第三小节.txt       # 处理指定文件
  python tts_pipeline.py --step split             # 仅分段
  python tts_pipeline.py --step tts               # 仅生成语音（需先分段）
  python tts_pipeline.py --step merge             # 仅合并音频（需先生成语音）
  python tts_pipeline.py --step stereo            # 仅将已合并音频转为 320Kbps 立体声
"""

import os
import sys
import json
import re
import time
import shutil
import argparse
import subprocess
from pathlib import Path
from typing import Callable, Optional

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    import requests
except ImportError:
    print("错误：请先安装 requests 库")
    print("  venv\\Scripts\\pip.exe install requests")
    sys.exit(1)

# ── 路径配置 ──────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent
INPUT_DIR = PROJECT_DIR / "input"
OUTPUT_DIR = PROJECT_DIR / "output"
TEMP_DIR = PROJECT_DIR / "temp"
BGM_DIR = PROJECT_DIR / "bgm"
WORKFLOW_PATH = PROJECT_DIR / "workflows" / "xiaoying-read.json"

BGM_DIR.mkdir(parents=True, exist_ok=True)

# ── 加载环境变量文件 (.env) ───────────────────────────────────
def reload_env():
    """重新加载 .env 文件"""
    try:
        from dotenv import load_dotenv
        env_path = PROJECT_DIR / ".env"
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
    except ImportError:
        pass

reload_env()

# ── 参数配置（优先从环境变量/.env中读取，否则使用默认值）────
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
MAX_SEGMENT_CHARS = int(os.environ.get("MAX_SEGMENT_CHARS", "200"))
PAUSE_DURATION = float(os.environ.get("PAUSE_DURATION", "1.0"))  # 段间停顿（秒）
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))        # 轮询 ComfyUI 状态间隔（秒）
TIMEOUT = int(os.environ.get("TIMEOUT", "180"))                  # 单段 TTS 超时（秒）
DEFAULT_BITRATE = os.environ.get("AUDIO_BITRATE", "320k")        # 默认音频码率


# ══════════════════════════════════════════════════════════════
#  系统诊断工具
# ══════════════════════════════════════════════════════════════

def check_comfyui_health(url: str = None) -> tuple[bool, str]:
    """检测 ComfyUI 服务是否可访问"""
    target_url = url or os.environ.get("COMFYUI_URL", COMFYUI_URL)
    try:
        resp = requests.get(f"{target_url.rstrip('/')}/system_stats", timeout=4)
        if resp.status_code == 200:
            return True, "ComfyUI 服务在线且连接正常"
        return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        return False, f"无法连接到 ComfyUI ({target_url}): {str(e)}"


def check_llm_health(api_base: str = None, api_key: str = None, model: str = None) -> tuple[bool, str]:
    """检测 LLM API 连通性"""
    base = (api_base or os.environ.get("LLM_API_BASE", "")).rstrip("/")
    key = api_key or os.environ.get("LLM_API_KEY", "")
    target_model = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")

    if not base or not key:
        return False, "未配置 LLM_API_BASE 或 LLM_API_KEY"

    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": target_model,
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 5,
            },
            timeout=8,
        )
        if resp.status_code == 200:
            return True, f"LLM API 正常响应 (模型: {target_model})"
        return False, f"HTTP {resp.status_code}: {resp.text[:150]}"
    except Exception as e:
        return False, f"LLM API 连接失败: {str(e)}"


def check_ffmpeg_health() -> tuple[bool, str]:
    """检测 FFmpeg 是否可用"""
    try:
        res = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0:
            version_line = res.stdout.decode("utf-8", errors="ignore").splitlines()[0]
            return True, f"FFmpeg 可用 ({version_line[:40]})"
        return False, "FFmpeg 命令异常"
    except FileNotFoundError:
        return False, "系统 PATH 中未找到 ffmpeg"
    except Exception as e:
        return False, f"检测 FFmpeg 出错: {str(e)}"


# ══════════════════════════════════════════════════════════════
#  步骤 1：LLM 分段
# ══════════════════════════════════════════════════════════════

def split_text_with_llm(
    text: str,
    custom_prompt: Optional[str] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    max_chars: Optional[int] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
) -> list[str]:
    """调用 LLM API 将文本按播客朗读习惯分段，每段 ≤ 200 字。"""
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        else:
            print(msg)

    reload_env()
    api_base = (api_base or os.environ.get("LLM_API_BASE", "")).rstrip("/")
    api_key = api_key or os.environ.get("LLM_API_KEY", "")
    model = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    max_chars = max_chars or int(os.environ.get("MAX_SEGMENT_CHARS", MAX_SEGMENT_CHARS))

    if not api_base or not api_key:
        err_msg = "未检测到 LLM API 配置，请在 .env 中设置 LLM_API_BASE 与 LLM_API_KEY"
        log(err_msg, "error")
        raise ValueError(err_msg)

    if custom_prompt:
        prompt = custom_prompt.replace("{text}", text).replace("{max_chars}", str(max_chars))
    else:
        prompt = (
            "你是一个专业的播客文稿编辑。请将以下文章按照播客朗读的习惯进行分段。\n"
            "\n"
            "要求：\n"
            f"1. 每段不超过{max_chars}个汉字（严格遵守）\n"
            "2. 在语义自然的地方断句，保持每段语意完整，适合连贯朗读\n"
            "3. 必须完整保留原文中的每一个字，不得删除、跳过或修改任何内容\n"
            "4. 标题行（如'第X小节'等单独成行的标题）单独作为一段，不与正文合并\n"
            "5. 以 JSON 数组格式返回，每个元素是一个段落字符串\n"
            "6. 只返回 JSON 数组，不要有其他任何文字或 markdown 标记\n"
            "\n"
            "文章内容：\n"
            f"{text}"
        )

    log(f"  调用 LLM API: {api_base} (model={model}, max_chars={max_chars})")
    
    max_retries = 3
    timeout_secs = int(os.environ.get("LLM_TIMEOUT", "120"))
    
    resp = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                f"{api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                },
                timeout=timeout_secs,
            )
            resp.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            log(f"    [警告] LLM 请求失败 (尝试 {attempt}/{max_retries}): {e}", "warning")
            if attempt == max_retries:
                log("    错误：已达到最大重试次数，分段失败。", "error")
                raise
            time.sleep(2 * attempt)

    if resp is None:
        raise RuntimeError("LLM 请求无响应")

    content = resp.json()["choices"][0]["message"]["content"].strip()

    if content.startswith("```"):
        lines = content.split("\n")
        start = 1
        end = len(lines)
        if lines[-1].strip() == "```":
            end = -1
        content = "\n".join(lines[start:end]).strip()

    try:
        segments = json.loads(content)
    except json.JSONDecodeError as e:
        log(f"JSON 解析失败: {e}\n原文内容: {content[:200]}...", "error")
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            segments = json.loads(match.group(0))
        else:
            raise

    if not isinstance(segments, list):
        raise ValueError(f"LLM 返回的不是 JSON 数组: {type(segments)}")

    segments = [s.strip() for s in segments if s and s.strip()]

    for i, seg in enumerate(segments, 1):
        char_count = len(seg)
        if char_count > max_chars:
            log(f"  ⚠ 第 {i} 段长度 {char_count} 字，超过 {max_chars} 字限制", "warning")

    return segments


def save_segments(segments: list[str], name: str) -> list[Path]:
    """将分段文本保存到 temp/<name>/ 目录下的 .txt 文件。"""
    seg_dir = TEMP_DIR / name
    seg_dir.mkdir(parents=True, exist_ok=True)

    for f in seg_dir.glob("*.txt"):
        try:
            f.unlink()
        except Exception:
            pass

    paths = []
    for i, seg in enumerate(segments, 1):
        p = seg_dir / f"{i:03d}.txt"
        p.write_text(seg.strip(), encoding="utf-8")
        paths.append(p)

    return paths


# ══════════════════════════════════════════════════════════════
#  步骤 2：ComfyUI TTS
# ══════════════════════════════════════════════════════════════

def generate_tts(
    text: str,
    filename_prefix: str,
    comfyui_url: str = COMFYUI_URL,
    speaker_preset: Optional[str] = None,
    speed: Optional[float] = None,
    seed: Optional[int] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> bytes:
    """
    调用 ComfyUI API 生成单段语音。
    返回音频文件的二进制数据（MP3）。
    """
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        else:
            print(msg)

    comfyui_url = comfyui_url.rstrip("/")

    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    
    if "9" in workflow and "inputs" in workflow["9"]:
        workflow["9"]["inputs"]["text"] = text
        if speaker_preset is not None and speaker_preset != "":
            workflow["9"]["inputs"]["speaker_preset"] = speaker_preset
        if speed is not None:
            workflow["9"]["inputs"]["speed"] = float(speed)
        if seed is not None:
            workflow["9"]["inputs"]["seed"] = int(seed)

    if "7" in workflow and "inputs" in workflow["7"]:
        workflow["7"]["inputs"]["filename_prefix"] = filename_prefix

    if cancel_check and cancel_check():
        raise RuntimeError("任务已取消")

    resp = requests.post(
        f"{comfyui_url}/prompt",
        json={"prompt": workflow},
        timeout=30,
    )
    resp.raise_for_status()
    prompt_id = resp.json().get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI 返回未包含 prompt_id: {resp.text}")

    log(f"    任务已提交: {prompt_id[:16]}...")

    start_time = time.time()
    while True:
        if cancel_check and cancel_check():
            raise RuntimeError("任务已取消")

        time.sleep(POLL_INTERVAL)
        elapsed = time.time() - start_time
        if elapsed > TIMEOUT:
            raise TimeoutError(f"ComfyUI 任务超时（{TIMEOUT}s）: {prompt_id}")

        try:
            hist_resp = requests.get(
                f"{comfyui_url}/history/{prompt_id}",
                timeout=10,
            )
            hist_resp.raise_for_status()
        except requests.RequestException as e:
            log(f"    轮询出错（将重试）: {e}", "warning")
            continue

        history = hist_resp.json()
        if prompt_id not in history:
            continue

        status_info = history[prompt_id].get("status", {})
        status_str = status_info.get("status_str", "")
        if status_str == "error":
            messages = status_info.get("messages", [])
            raise RuntimeError(f"ComfyUI 任务失败: {messages}")

        outputs = history[prompt_id].get("outputs", {})
        node_output = outputs.get("7", {})
        audio_list = node_output.get("audio", node_output.get("gifs", []))

        if not audio_list:
            continue

        audio_info = audio_list[0]
        filename = audio_info["filename"]
        subfolder = audio_info.get("subfolder", "")
        file_type = audio_info.get("type", "output")

        dl_resp = requests.get(
            f"{comfyui_url}/view",
            params={
                "filename": filename,
                "subfolder": subfolder,
                "type": file_type,
            },
            timeout=60,
        )
        dl_resp.raise_for_status()

        size_kb = len(dl_resp.content) / 1024
        log(f"    已下载: {filename} ({size_kb:.1f} KB)")
        return dl_resp.content

    raise RuntimeError("未能获取音频输出")


def generate_all_tts(
    segment_files: list[Path],
    name: str,
    comfyui_url: str = COMFYUI_URL,
    only_segments: Optional[set[int]] = None,
    speaker_preset: Optional[str] = None,
    speed: Optional[float] = None,
    seed: Optional[int] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> list[Path]:
    """对所有（或指定的）分段调用 TTS，将音频 MP3 保存到 temp/<name>/ 目录。"""
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        else:
            print(msg)

    audio_dir = TEMP_DIR / name
    audio_dir.mkdir(parents=True, exist_ok=True)

    if only_segments:
        for idx in only_segments:
            old = audio_dir / f"{idx:03d}.mp3"
            if old.exists():
                try:
                    old.unlink()
                    log(f"  已删除旧音频: {old.name}")
                except Exception:
                    pass
    else:
        for f in audio_dir.glob("*.mp3"):
            try:
                f.unlink()
            except Exception:
                pass

    audio_paths = []
    total = len(segment_files)

    for i, seg_file in enumerate(segment_files, 1):
        if cancel_check and cancel_check():
            log("任务已中止", "warning")
            break

        if only_segments and i not in only_segments:
            audio_path = audio_dir / f"{i:03d}.mp3"
            if audio_path.exists():
                log(f"  [{i}/{total}] 跳过（保留原有音频）: {audio_path.name}")
                audio_paths.append(audio_path)
            else:
                log(f"  [{i}/{total}] ⚠ 跳过但音频文件不存在: {i:03d}.mp3", "warning")
            continue

        text = seg_file.read_text(encoding="utf-8").strip()
        if not text:
            log(f"  [{i}/{total}] 跳过空段落: {seg_file.name}", "warning")
            continue

        preview = text[:40].replace("\n", " ")
        log(f"  [{i}/{total}] 生成语音: {preview}...")
        if progress_callback:
            progress_callback(i, total, preview)

        filename_prefix = f"audio/{name}_{i:03d}"

        audio_data = generate_tts(
            text=text,
            filename_prefix=filename_prefix,
            comfyui_url=comfyui_url,
            speaker_preset=speaker_preset,
            speed=speed,
            seed=seed,
            log_callback=log_callback,
            cancel_check=cancel_check,
        )

        audio_path = audio_dir / f"{i:03d}.mp3"
        audio_path.write_bytes(audio_data)
        audio_paths.append(audio_path)

    existing_audios = sorted(audio_dir.glob("*.mp3"))
    return existing_audios


# ══════════════════════════════════════════════════════════════
#  步骤 3：FFmpeg 合并与立体声 (320Kbps) 转换
# ══════════════════════════════════════════════════════════════

def convert_to_stereo(
    input_path: Path,
    output_path: Optional[Path] = None,
    bitrate: str = DEFAULT_BITRATE,
    log_callback: Optional[Callable[[str, str], None]] = None,
) -> Path:
    """
    将音频转换为双声道立体声（左右声道合并），并以 320Kbps 高码率 MP3 输出。
    """
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        else:
            print(msg)

    if not input_path.exists():
        raise FileNotFoundError(f"音频文件不存在: {input_path}")

    target_out = output_path or input_path
    target_out.parent.mkdir(parents=True, exist_ok=True)

    # 若输入输出相同，先写入临时文件
    temp_target = target_out.parent / f"temp_stereo_{input_path.stem}.mp3" if target_out == input_path else target_out

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-af", "aformat=channel_layouts=stereo",
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        "-ar", "44100",
        "-ac", "2",
        str(temp_target)
    ]

    log(f"  执行立体声转换 (左右双声道, 44.1kHz, {bitrate} MP3)...")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace")
        log(f"  FFmpeg 立体声转换失败:\n{err}", "error")
        if temp_target.exists() and temp_target != target_out:
            temp_target.unlink()
        raise RuntimeError(f"立体声转换失败: {err}")

    if temp_target != target_out:
        if target_out.exists():
            target_out.unlink()
        shutil.move(str(temp_target), str(target_out))

    size_mb = target_out.stat().st_size / (1024 * 1024)
    log(f"  ✓ 高保真立体声已生成: {target_out.name} ({size_mb:.2f} MB, {bitrate})", "success")
    return target_out


def get_audio_duration(file_path: Path) -> float:
    """使用 ffprobe 获取音频文件的时长（秒）"""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception:
        pass
    return 0.0


def mix_background_music(
    voice_path: Path,
    bgm_path: Path,
    output_path: Optional[Path] = None,
    bgm_volume: float = 0.15,
    fade_duration: float = 5.0,
    bitrate: str = DEFAULT_BITRATE,
    log_callback: Optional[Callable[[str, str], None]] = None,
) -> Path:
    """
    将人声音频与背景音乐混合：
    1. 自动循环背景音乐匹配人声时长；
    2. 保持人声 100% 原始饱满音量，背景音乐按比例调低 (如 0.15 = 15%)；
    3. 人声播毕后，背景音乐延长指定时长 (默认 5 秒) 平滑淡出收尾；
    4. 输出 320Kbps 双声道立体声 MP3。
    """
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        else:
            print(msg)

    if not voice_path.exists():
        raise FileNotFoundError(f"人声音频文件不存在: {voice_path}")
    if not bgm_path.exists():
        raise FileNotFoundError(f"背景音乐文件不存在: {bgm_path}")

    target_out = output_path or voice_path
    target_out.parent.mkdir(parents=True, exist_ok=True)

    temp_target = target_out.parent / f"temp_mixed_{voice_path.stem}.mp3" if target_out == voice_path else target_out

    # FFmpeg amix 双输入时会默认各除以 2，因此我们将人声乘 2.0，BGM 乘 (bgm_volume * 2.0) 以确保 100% 原声人声音量
    voice_vol = 2.0
    bgm_vol = bgm_volume * 2.0

    voice_dur = get_audio_duration(voice_path)
    if voice_dur > 0 and fade_duration > 0:
        # 人声末尾补入 fade_duration 静音保持 amix 正常双路工作，BGM 从 voice_dur 开始淡出 fade_duration 秒
        filter_complex = (
            f"[0:a]volume={voice_vol:.2f},apad=pad_dur={fade_duration:.2f},aformat=channel_layouts=stereo[voice];"
            f"[1:a]volume={bgm_vol:.3f},afade=t=out:st={voice_dur:.2f}:d={fade_duration:.2f},aformat=channel_layouts=stereo[bgm];"
            f"[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0[out]"
        )
        fade_info = f"，人声结束后 BGM 延长 {fade_duration:.0f} 秒平滑淡出"
    else:
        filter_complex = (
            f"[0:a]volume={voice_vol:.2f},aformat=channel_layouts=stereo[voice];"
            f"[1:a]volume={bgm_vol:.3f},aformat=channel_layouts=stereo[bgm];"
            f"[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2[out]"
        )
        fade_info = ""

    cmd = [
        "ffmpeg", "-y",
        "-i", str(voice_path),
        "-stream_loop", "-1",
        "-i", str(bgm_path),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        "-ar", "44100",
        "-ac", "2",
        str(temp_target)
    ]

    log(f"  执行背景音乐混音 ({bgm_path.name}, 音量 {int(bgm_volume * 100)}%{fade_info})...")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace")
        log(f"  FFmpeg 背景音乐混音失败:\n{err}", "error")
        if temp_target.exists() and temp_target != target_out:
            temp_target.unlink()
        raise RuntimeError(f"背景音乐混音失败: {err}")

    if temp_target != target_out:
        if target_out.exists():
            target_out.unlink()
        shutil.move(str(temp_target), str(target_out))

    size_mb = target_out.stat().st_size / (1024 * 1024)
    log(f"  ✓ 背景音乐混音完成: {target_out.name} ({size_mb:.2f} MB, 立体声 {bitrate})", "success")
    return target_out


def merge_audio(
    audio_files: list[Path],
    output_path: Path,
    pause: float = PAUSE_DURATION,
    stereo: bool = True,
    bitrate: str = DEFAULT_BITRATE,
    bgm_path: Optional[Path] = None,
    bgm_volume: float = 0.15,
    log_callback: Optional[Callable[[str, str], None]] = None,
):
    """
    使用 FFmpeg 合并多个 MP3 文件，段间添加指定时长的静音停顿，
    默认转换为左右双声道立体声并以 320Kbps 输出，若指定 bgm_path 则自动混入背景音乐。
    """
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        else:
            print(msg)

    if not audio_files:
        err = "没有音频文件可合并"
        log(f"  错误：{err}", "error")
        raise ValueError(err)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(audio_files)

    if n == 1:
        if stereo:
            convert_to_stereo(audio_files[0], output_path, bitrate=bitrate, log_callback=log_callback)
        else:
            shutil.copy2(audio_files[0], output_path)
            log(f"  只有 1 个音频文件，直接复制到: {output_path.name}")
    else:
        cmd = ["ffmpeg", "-y"]

        for f in audio_files:
            cmd.extend(["-i", str(f)])

        filter_parts = []
        for i in range(n - 1):
            filter_parts.append(f"[{i}:a]apad=pad_dur={pause}[a{i}]")

        concat_inputs = "".join(f"[a{i}]" for i in range(n - 1))
        concat_inputs += f"[{n - 1}:a]"

        if stereo:
            filter_parts.append(f"{concat_inputs}concat=n={n}:v=0:a=1,aformat=channel_layouts=stereo[out]")
        else:
            filter_parts.append(f"{concat_inputs}concat=n={n}:v=0:a=1[out]")

        filter_complex = ";".join(filter_parts)
        cmd.extend(["-filter_complex", filter_complex])
        cmd.extend(["-map", "[out]"])

        if stereo:
            cmd.extend(["-c:a", "libmp3lame", "-b:a", bitrate, "-ar", "44100", "-ac", "2"])

        cmd.append(str(output_path))

        log(f"  执行 FFmpeg 合并（{n} 个分段，段间 {pause}s 停顿，立体声={stereo}，码率={bitrate}）...")
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stderr_text = result.stderr.decode("utf-8", errors="replace")

        if result.returncode != 0:
            log(f"  FFmpeg 错误:\n{stderr_text}", "error")
            raise RuntimeError(f"FFmpeg 合并失败: {stderr_text}")

        size_mb = output_path.stat().st_size / (1024 * 1024)
        log(f"  ✓ 合并完成: {output_path.name} ({size_mb:.2f} MB, 立体声 320k)")

    # 若指定了背景音乐，执行混音
    if bgm_path and Path(bgm_path).exists():
        mix_background_music(
            voice_path=output_path,
            bgm_path=Path(bgm_path),
            output_path=output_path,
            bgm_volume=bgm_volume,
            bitrate=bitrate,
            log_callback=log_callback,
        )


# ══════════════════════════════════════════════════════════════
#  主流程处理
# ══════════════════════════════════════════════════════════════

def process_file(
    input_path: Path,
    comfyui_url: str = COMFYUI_URL,
    pause: float = PAUSE_DURATION,
    speaker_preset: Optional[str] = None,
    speed: Optional[float] = None,
    seed: Optional[int] = None,
    stereo: bool = True,
    bitrate: str = DEFAULT_BITRATE,
    bgm_path: Optional[Path] = None,
    bgm_volume: float = 0.15,
    log_callback: Optional[Callable[[str, str], None]] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
):
    """处理单个文本文件的完整三步流程（支持自动合并 320k 立体声并混入背景音乐）。"""
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        else:
            print(msg)

    name = input_path.stem
    log(f"\n{'═' * 56}")
    log(f"  处理文件: {input_path.name}")
    log(f"{'═' * 56}")

    text = input_path.read_text(encoding="utf-8")

    # 步骤 1: 分段
    log("\n[步骤 1/3] 使用 LLM 分段...")
    segments = split_text_with_llm(text, log_callback=log_callback)
    seg_files = save_segments(segments, name)
    log(f"  共分为 {len(seg_files)} 段")

    # 步骤 2: TTS
    log(f"\n[步骤 2/3] 调用 ComfyUI 生成语音 ({comfyui_url})...")
    audio_files = generate_all_tts(
        seg_files,
        name,
        comfyui_url=comfyui_url,
        speaker_preset=speaker_preset,
        speed=speed,
        seed=seed,
        log_callback=log_callback,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    log(f"  共生成 {len(audio_files)} 个音频文件")

    # 步骤 3: 合并为立体声 (320Kbps)，并混入背景音乐（若提供）
    log(f"\n[步骤 3/3] FFmpeg 合并为 320Kbps 立体声音频...")
    output_path = OUTPUT_DIR / f"{name}.mp3"
    merge_audio(
        audio_files,
        output_path,
        pause=pause,
        stereo=stereo,
        bitrate=bitrate,
        bgm_path=bgm_path,
        bgm_volume=bgm_volume,
        log_callback=log_callback,
    )

    log(f"\n{'─' * 56}")
    log(f"  ✓ 完成: {output_path.name}")
    log(f"{'─' * 56}")


def step_split_only(input_path: Path, custom_prompt=None, max_chars=None, log_callback=None):
    """仅执行分段步骤。"""
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        else:
            print(msg)

    name = input_path.stem
    text = input_path.read_text(encoding="utf-8")

    log(f"\n处理文件: {input_path.name}")
    segments = split_text_with_llm(text, custom_prompt=custom_prompt, max_chars=max_chars, log_callback=log_callback)
    seg_files = save_segments(segments, name)

    log(f"\n共分为 {len(seg_files)} 段，保存到 temp/{name}/：")
    for i, seg in enumerate(segments, 1):
        log(f"  {i:3d}. ({len(seg):3d} 字) {seg[:60]}...")
    return seg_files


def step_tts_only(
    input_path: Path,
    comfyui_url: str = COMFYUI_URL,
    only_segments: Optional[set[int]] = None,
    speaker_preset: Optional[str] = None,
    speed: Optional[float] = None,
    seed: Optional[int] = None,
    log_callback=None,
    progress_callback=None,
    cancel_check=None,
):
    """仅执行 TTS 步骤（需要已有分段文件）。"""
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        else:
            print(msg)

    name = input_path.stem
    seg_dir = TEMP_DIR / name
    seg_files = sorted(seg_dir.glob("*.txt"))

    if not seg_files:
        err = f"{seg_dir} 中没有找到分段文件，请先执行分段"
        log(f"错误：{err}", "error")
        raise FileNotFoundError(err)

    if only_segments:
        log(f"\n处理文件: {input_path.name}（重新生成第 {sorted(only_segments)} 段）")
    else:
        log(f"\n处理文件: {input_path.name}（{len(seg_files)} 个分段，全量生成）")

    audio_files = generate_all_tts(
        seg_files,
        name,
        comfyui_url=comfyui_url,
        only_segments=only_segments,
        speaker_preset=speaker_preset,
        speed=speed,
        seed=seed,
        log_callback=log_callback,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    log(f"\n共处理 {len(audio_files)} 个音频文件")
    return audio_files


def step_merge_only(
    input_path: Path,
    pause: float = PAUSE_DURATION,
    stereo: bool = True,
    bitrate: str = DEFAULT_BITRATE,
    bgm_path: Optional[Path] = None,
    bgm_volume: float = 0.15,
    log_callback=None,
):
    """仅执行合并步骤（需要已有音频文件）。"""
    def log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        else:
            print(msg)

    name = input_path.stem
    audio_dir = TEMP_DIR / name
    audio_files = sorted(audio_dir.glob("*.mp3"))

    if not audio_files:
        err = f"{audio_dir} 中没有找到 MP3 文件，请先生成语音"
        log(f"错误：{err}", "error")
        raise FileNotFoundError(err)

    output_path = OUTPUT_DIR / f"{name}.mp3"
    log(f"\n合并 {len(audio_files)} 个音频文件（转为 320Kbps 立体声）...")
    merge_audio(
        audio_files,
        output_path,
        pause=pause,
        stereo=stereo,
        bitrate=bitrate,
        bgm_path=bgm_path,
        bgm_volume=bgm_volume,
        log_callback=log_callback,
    )
    return output_path


def step_stereo_only(input_path: Path, bitrate: str = DEFAULT_BITRATE, log_callback=None):
    """仅将已生成的 output MP3 转换为 320Kbps 双声道立体声。"""
    name = input_path.stem
    output_path = OUTPUT_DIR / f"{name}.mp3"
    if not output_path.exists():
        err = f"未找到已合并的音频文件: {output_path}"
        if log_callback:
            log_callback(err, "error")
        raise FileNotFoundError(err)

    return convert_to_stereo(output_path, output_path, bitrate=bitrate, log_callback=log_callback)


def step_bgm_only(
    input_path: Path,
    bgm_path: Path,
    bgm_volume: float = 0.15,
    bitrate: str = DEFAULT_BITRATE,
    log_callback=None,
):
    """仅将已合并的人声音频与指定背景音乐混音。"""
    name = input_path.stem
    output_path = OUTPUT_DIR / f"{name}.mp3"
    if not output_path.exists():
        err = f"未找到已合并的人声音频文件: {output_path}"
        if log_callback:
            log_callback(err, "error")
        raise FileNotFoundError(err)

    return mix_background_music(
        voice_path=output_path,
        bgm_path=bgm_path,
        output_path=output_path,
        bgm_volume=bgm_volume,
        bitrate=bitrate,
        log_callback=log_callback,
    )


def main():
    parser = argparse.ArgumentParser(
        description="文章转语音 (TTS) 自动化管线 - 支持 320Kbps 立体声与背景音乐混音",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--step",
        choices=["split", "tts", "retts", "merge", "stereo", "bgm", "all"],
        default="all",
        help="执行步骤：split=仅分段, tts=全量语音合成, retts=仅重新生成指定段落并合成, merge=仅合并, stereo=仅转换为立体声(320k), bgm=仅混入背景音乐, all=完整流程 (默认: all)",
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="指定 input/ 目录下的文件名（默认处理所有 .txt 文件）",
    )
    parser.add_argument(
        "--segments",
        type=str,
        default=None,
        metavar="N[,N...]",
        help="指定需要重新生成语音的段落序号（1-based，逗号分隔），配合 --step tts 或 retts 使用。例如: --segments 1,3,5",
    )
    parser.add_argument(
        "--comfyui-url",
        type=str,
        default=COMFYUI_URL,
        help=f"ComfyUI API 地址（默认: {COMFYUI_URL}）",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=PAUSE_DURATION,
        help=f"段间停顿秒数（默认: {PAUSE_DURATION}）",
    )
    parser.add_argument(
        "--stereo",
        action="store_true",
        default=True,
        help="是否转换为左右双声道立体声 (默认: True)",
    )
    parser.add_argument(
        "--bitrate",
        type=str,
        default="320k",
        help="音频比特率（默认: 320k）",
    )
    parser.add_argument(
        "--bgm",
        type=str,
        default=None,
        help="背景音乐文件路径（可在 bgm/ 目录下，如 --bgm bgm/piano.mp3）",
    )
    parser.add_argument(
        "--bgm-volume",
        type=float,
        default=0.15,
        help="背景音乐音量比例（0.0 ~ 1.0，默认 0.15 即 15%）",
    )
    args = parser.parse_args()

    only_segments = None
    if args.segments:
        try:
            only_segments = {int(s.strip()) for s in args.segments.split(",") if s.strip()}
        except ValueError:
            print("错误：--segments 参数格式不正确，请使用逗号分隔的整数，如 '1,3,5'")
            sys.exit(1)

    bgm_path = None
    if args.bgm:
        cand = Path(args.bgm)
        if not cand.exists():
            cand = BGM_DIR / args.bgm
        if not cand.exists():
            print(f"错误：未找到背景音乐文件: {args.bgm}")
            sys.exit(1)
        bgm_path = cand

    if args.file:
        target = INPUT_DIR / args.file
        if not target.exists():
            print(f"错误：文件不存在: {target}")
            sys.exit(1)
        input_files = [target]
    else:
        input_files = sorted(INPUT_DIR.glob("*.txt"))
        if not input_files:
            print(f"错误：{INPUT_DIR} 目录下没有 .txt 文件")
            sys.exit(1)

    print(f"找到 {len(input_files)} 个输入文件")

    for input_file in input_files:
        if args.step == "split":
            step_split_only(input_file)
        elif args.step == "tts":
            step_tts_only(input_file, args.comfyui_url, only_segments)
        elif args.step == "retts":
            if not only_segments:
                print("错误：--step retts 必须配合 --segments 使用，例如: --segments 1,3,5")
                sys.exit(1)
            step_tts_only(input_file, args.comfyui_url, only_segments)
            step_merge_only(
                input_file,
                pause=args.pause,
                stereo=args.stereo,
                bitrate=args.bitrate,
                bgm_path=bgm_path,
                bgm_volume=args.bgm_volume,
            )
        elif args.step == "merge":
            step_merge_only(
                input_file,
                pause=args.pause,
                stereo=args.stereo,
                bitrate=args.bitrate,
                bgm_path=bgm_path,
                bgm_volume=args.bgm_volume,
            )
        elif args.step == "stereo":
            step_stereo_only(input_file, bitrate=args.bitrate)
        elif args.step == "bgm":
            if not bgm_path:
                print("错误：--step bgm 必须指定 --bgm 参数")
                sys.exit(1)
            step_bgm_only(input_file, bgm_path=bgm_path, bgm_volume=args.bgm_volume, bitrate=args.bitrate)
        else:
            process_file(
                input_file,
                comfyui_url=args.comfyui_url,
                pause=args.pause,
                stereo=args.stereo,
                bitrate=args.bitrate,
                bgm_path=bgm_path,
                bgm_volume=args.bgm_volume,
            )

    print("\n🎉 全部处理完成！")


if __name__ == "__main__":
    main()
