#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
文章转语音 (TTS) 自动化管线
=============================
流程：
  1. 使用 LLM API 将文本分段（每段 ≤ 200 字，播客朗读风格）
  2. 调用 ComfyUI API（CosyVoice3 / xiaoying-read）逐段生成语音
  3. 使用 FFmpeg 合并分段音频，段间添加 1 秒停顿

配置方式：
  在项目根目录下的 .env 文件中配置（也可通过系统环境变量传入）：
  - LLM_API_BASE: LLM API 基地址（如 https://api.openai.com/v1）
  - LLM_API_KEY:  LLM API 密钥
  - LLM_MODEL:    模型名称（默认 gpt-4o-mini）
  - COMFYUI_URL:  ComfyUI 地址（默认 http://127.0.0.1:8188）

用法：
  python tts_pipeline.py                          # 处理 input/ 下所有 txt
  python tts_pipeline.py --file 第三小节.txt       # 处理指定文件
  python tts_pipeline.py --step split             # 仅分段
  python tts_pipeline.py --step tts               # 仅生成语音（需先分段）
  python tts_pipeline.py --step merge             # 仅合并音频（需先生成语音）
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
WORKFLOW_PATH = PROJECT_DIR / "workflows" / "xiaoying-read.json"

# ── 加载环境变量文件 (.env) ───────────────────────────────────
try:
    from dotenv import load_dotenv
    # 优先加载当前目录下的 .env 文件（不覆盖已存在的系统环境变量）
    env_path = PROJECT_DIR / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
except ImportError:
    pass

# ── 参数配置（优先从环境变量/.env中读取，否则使用默认值）────
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
MAX_SEGMENT_CHARS = int(os.environ.get("MAX_SEGMENT_CHARS", "200"))
PAUSE_DURATION = float(os.environ.get("PAUSE_DURATION", "1.0"))  # 段间停顿（秒）
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))        # 轮询 ComfyUI 状态间隔（秒）
TIMEOUT = int(os.environ.get("TIMEOUT", "180"))                  # 单段 TTS 超时（秒）


# ══════════════════════════════════════════════════════════════
#  步骤 1：LLM 分段
# ══════════════════════════════════════════════════════════════

def split_text_with_llm(text: str) -> list[str]:
    """调用 LLM API 将文本按播客朗读习惯分段，每段 ≤ 200 字。"""
    api_base = os.environ.get("LLM_API_BASE")
    api_key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")

    if not api_base or not api_key:
        print("错误：未检测到 LLM API 配置。")
        print(f"请在项目根目录的 .env 文件中配置：{PROJECT_DIR / '.env'}")
        print("  LLM_API_BASE=https://api.openai.com/v1")
        print("  LLM_API_KEY=sk-your-key")
        print("  LLM_MODEL=gpt-4o-mini")
        print("\n(可参考 .env.example 模板文件)")
        sys.exit(1)

    prompt = (
        "你是一个专业的播客文稿编辑。请将以下文章按照播客朗读的习惯进行分段。\n"
        "\n"
        "要求：\n"
        "1. 每段不超过200个汉字（严格遵守）\n"
        "2. 在语义自然的地方断句，保持每段语意完整，适合连贯朗读\n"
        "3. 必须完整保留原文中的每一个字，不得删除、跳过或修改任何内容\n"
        "4. 标题行（如'第X小节'等单独成行的标题）单独作为一段，不与正文合并\n"
        "5. 以 JSON 数组格式返回，每个元素是一个段落字符串\n"
        "6. 只返回 JSON 数组，不要有其他任何文字或 markdown 标记\n"
        "\n"
        "文章内容：\n"
        f"{text}"
    )

    print(f"  调用 LLM API: {api_base} (model={model})")
    
    max_retries = 3
    timeout_secs = int(os.environ.get("LLM_TIMEOUT", "120"))
    
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                f"{api_base.rstrip('/')}/chat/completions",
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
            break  # 请求成功，跳出重试循环
        except requests.exceptions.RequestException as e:
            print(f"    [警告] LLM 请求失败 (尝试 {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                print("    错误：已达到最大重试次数，分段失败。")
                raise
            time.sleep(2 * attempt)  # 递增重试延迟

    content = resp.json()["choices"][0]["message"]["content"].strip()

    # 去除 markdown 代码块标记（```json ... ```）
    if content.startswith("```"):
        lines = content.split("\n")
        start = 1
        end = len(lines)
        if lines[-1].strip() == "```":
            end = -1
        content = "\n".join(lines[start:end]).strip()

    segments = json.loads(content)

    if not isinstance(segments, list):
        raise ValueError(f"LLM 返回的不是 JSON 数组: {type(segments)}")

    # 过滤空段落
    segments = [s.strip() for s in segments if s.strip()]

    # 验证长度
    for i, seg in enumerate(segments, 1):
        char_count = len(seg)
        if char_count > MAX_SEGMENT_CHARS:
            print(f"  ⚠ 第 {i} 段长度 {char_count} 字，超过 {MAX_SEGMENT_CHARS} 字限制")

    return segments


def save_segments(segments: list[str], name: str) -> list[Path]:
    """将分段文本保存到 temp/<name>/ 目录下的 .txt 文件。"""
    seg_dir = TEMP_DIR / name
    seg_dir.mkdir(parents=True, exist_ok=True)

    # 清理旧的分段文件
    for f in seg_dir.glob("*.txt"):
        f.unlink()

    paths = []
    for i, seg in enumerate(segments, 1):
        p = seg_dir / f"{i:03d}.txt"
        p.write_text(seg.strip(), encoding="utf-8")
        paths.append(p)

    return paths


# ══════════════════════════════════════════════════════════════
#  步骤 2：ComfyUI TTS
# ══════════════════════════════════════════════════════════════

def generate_tts(text: str, filename_prefix: str, comfyui_url: str) -> bytes:
    """
    调用 ComfyUI API 生成单段语音。
    返回音频文件的二进制数据（MP3）。
    """
    # 加载并修改 workflow 模板
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    workflow["9"]["inputs"]["text"] = text
    workflow["7"]["inputs"]["filename_prefix"] = filename_prefix

    # 提交任务
    resp = requests.post(
        f"{comfyui_url}/prompt",
        json={"prompt": workflow},
        timeout=30,
    )
    resp.raise_for_status()
    prompt_id = resp.json()["prompt_id"]
    print(f"    任务已提交: {prompt_id[:16]}...")

    # 轮询等待完成
    start_time = time.time()
    while True:
        time.sleep(POLL_INTERVAL)
        elapsed = time.time() - start_time
        if elapsed > TIMEOUT:
            raise TimeoutError(
                f"ComfyUI 任务超时（{TIMEOUT}s）: {prompt_id}"
            )

        try:
            hist_resp = requests.get(
                f"{comfyui_url}/history/{prompt_id}",
                timeout=10,
            )
            hist_resp.raise_for_status()
        except requests.RequestException as e:
            print(f"    轮询出错（将重试）: {e}")
            continue

        history = hist_resp.json()
        if prompt_id not in history:
            # 任务尚未完成
            continue

        # 检查任务状态
        status_info = history[prompt_id].get("status", {})
        status_str = status_info.get("status_str", "")
        if status_str == "error":
            messages = status_info.get("messages", [])
            raise RuntimeError(f"ComfyUI 任务失败: {messages}")

        outputs = history[prompt_id].get("outputs", {})

        # 查找音频输出（节点 "7" = SaveAudioAdvanced）
        node_output = outputs.get("7", {})
        # SaveAudioAdvanced 可能使用 "audio" 或 "gifs" 作为输出键
        audio_list = node_output.get("audio", node_output.get("gifs", []))

        if not audio_list:
            # 输出尚未就绪
            continue

        # 下载音频文件
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
        print(f"    已下载: {filename} ({size_kb:.1f} KB)")
        return dl_resp.content

    # 不应到达此处
    raise RuntimeError("未能获取音频输出")


def generate_all_tts(
    segment_files: list[Path],
    name: str,
    comfyui_url: str,
    only_segments: set[int] | None = None,
) -> list[Path]:
    """对所有（或指定的）分段调用 TTS，将音频 MP3 保存到 temp/<name>/ 目录。

    Args:
        segment_files: 分段文本文件列表（已排序）
        name:          文件名（不含扩展名）
        comfyui_url:   ComfyUI API 地址
        only_segments: 若指定，则只重新生成这些序号（1-based）的分段，其余保留原有音频
    """
    audio_dir = TEMP_DIR / name
    audio_dir.mkdir(parents=True, exist_ok=True)

    if only_segments:
        # 部分重新生成：不清空现有音频，仅删除需要重新生成的那些
        for idx in only_segments:
            old = audio_dir / f"{idx:03d}.mp3"
            if old.exists():
                old.unlink()
                print(f"  已删除旧音频: {old.name}")
    else:
        # 全量生成：清理所有旧的音频文件
        for f in audio_dir.glob("*.mp3"):
            f.unlink()

    audio_paths = []
    total = len(segment_files)

    for i, seg_file in enumerate(segment_files, 1):
        # 部分重新生成时，跳过不在目标列表中的段落
        if only_segments and i not in only_segments:
            audio_path = audio_dir / f"{i:03d}.mp3"
            if audio_path.exists():
                print(f"  [{i}/{total}] 跳过（保留原有音频）: {audio_path.name}")
                audio_paths.append(audio_path)
            else:
                print(f"  [{i}/{total}] ⚠ 跳过但音频文件不存在: {i:03d}.mp3（可能需要补充生成）")
            continue

        text = seg_file.read_text(encoding="utf-8").strip()
        if not text:
            print(f"  [{i}/{total}] 跳过空段落: {seg_file.name}")
            continue

        preview = text[:40].replace("\n", " ")
        print(f"  [{i}/{total}] 生成语音: {preview}...")

        # filename_prefix 决定 ComfyUI 保存时的文件名前缀
        filename_prefix = f"audio/{name}_{i:03d}"

        audio_data = generate_tts(text, filename_prefix, comfyui_url)

        audio_path = audio_dir / f"{i:03d}.mp3"
        audio_path.write_bytes(audio_data)
        audio_paths.append(audio_path)

    # 按序号排序，确保合并顺序正确
    audio_paths.sort()
    return audio_paths


# ══════════════════════════════════════════════════════════════
#  步骤 3：FFmpeg 合并
# ══════════════════════════════════════════════════════════════

def merge_audio(
    audio_files: list[Path], output_path: Path, pause: float = PAUSE_DURATION
):
    """
    使用 FFmpeg 合并多个 MP3 文件，段间添加指定时长的静音停顿。
    参考 docs/ref-commands.md 中的命令格式。
    """
    if not audio_files:
        print("  错误：没有音频文件可合并")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(audio_files)

    if n == 1:
        # 单文件直接复制
        shutil.copy2(audio_files[0], output_path)
        print(f"  只有 1 个音频文件，直接复制到: {output_path}")
        return

    # 构建 FFmpeg 命令
    cmd = ["ffmpeg", "-y"]  # -y: 覆盖已有输出文件

    # 添加所有输入文件
    for f in audio_files:
        cmd.extend(["-i", str(f)])

    # 构建 filter_complex:
    #   除最后一个文件外，每个文件后添加 pause 秒的静音填充
    #   然后用 concat 合并所有流
    filter_parts = []
    for i in range(n - 1):
        filter_parts.append(f"[{i}:a]apad=pad_dur={pause}[a{i}]")

    # concat 输入：前 n-1 个带填充，最后一个直接使用
    concat_inputs = "".join(f"[a{i}]" for i in range(n - 1))
    concat_inputs += f"[{n - 1}:a]"
    filter_parts.append(f"{concat_inputs}concat=n={n}:v=0:a=1[out]")

    filter_complex = ";".join(filter_parts)
    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(["-map", "[out]", str(output_path)])

    print(f"  执行 FFmpeg 合并（{n} 个文件，段间 {pause}s 停顿）...")
    # 使用字节模式捕获输出，避免 Windows GBK 编码导致 UnicodeDecodeError
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_text = result.stderr.decode("utf-8", errors="replace")

    if result.returncode != 0:
        print(f"  FFmpeg 错误:\n{stderr_text}")
        raise RuntimeError("FFmpeg 合并失败")

    # 显示输出文件大小
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  ✓ 合并完成: {output_path} ({size_mb:.2f} MB)")


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════

def process_file(input_path: Path, comfyui_url: str, pause: float):
    """处理单个文本文件的完整三步流程。"""
    name = input_path.stem
    print(f"\n{'═' * 56}")
    print(f"  处理文件: {input_path.name}")
    print(f"{'═' * 56}")

    text = input_path.read_text(encoding="utf-8")

    # ── 步骤 1: 分段 ─────────────────────────────────────
    print("\n[步骤 1/3] 使用 LLM 分段...")
    segments = split_text_with_llm(text)
    seg_files = save_segments(segments, name)
    print(f"  共分为 {len(seg_files)} 段：")
    for i, seg in enumerate(segments, 1):
        print(f"    {i:3d}. ({len(seg):3d} 字) {seg[:50]}...")

    # ── 步骤 2: TTS ──────────────────────────────────────
    print(f"\n[步骤 2/3] 调用 ComfyUI 生成语音 ({comfyui_url})...")
    audio_files = generate_all_tts(seg_files, name, comfyui_url)
    print(f"  共生成 {len(audio_files)} 个音频文件")

    # ── 步骤 3: 合并 ─────────────────────────────────────
    print(f"\n[步骤 3/3] FFmpeg 合并音频...")
    output_path = OUTPUT_DIR / f"{name}.mp3"
    merge_audio(audio_files, output_path, pause)

    print(f"\n{'─' * 56}")
    print(f"  ✓ 完成: {output_path}")
    print(f"{'─' * 56}")


def step_split_only(input_path: Path):
    """仅执行分段步骤（调试用）。"""
    name = input_path.stem
    text = input_path.read_text(encoding="utf-8")

    print(f"\n处理文件: {input_path.name}")
    segments = split_text_with_llm(text)
    seg_files = save_segments(segments, name)

    print(f"\n共分为 {len(seg_files)} 段，保存到 temp/{name}/：")
    for i, seg in enumerate(segments, 1):
        print(f"  {i:3d}. ({len(seg):3d} 字) {seg[:60]}...")


def step_tts_only(
    input_path: Path, comfyui_url: str, only_segments: set[int] | None = None
):
    """仅执行 TTS 步骤（需要已有分段文件）。

    Args:
        only_segments: 若指定，则只重新生成这些序号（1-based）的分段
    """
    name = input_path.stem
    seg_dir = TEMP_DIR / name
    seg_files = sorted(seg_dir.glob("*.txt"))

    if not seg_files:
        print(f"错误：{seg_dir} 中没有找到分段文件，请先执行 --step split")
        sys.exit(1)

    if only_segments:
        print(f"\n处理文件: {input_path.name}（重新生成第 {sorted(only_segments)} 段）")
    else:
        print(f"\n处理文件: {input_path.name}（{len(seg_files)} 个分段，全量生成）")

    audio_files = generate_all_tts(seg_files, name, comfyui_url, only_segments)
    print(f"\n共处理 {len(audio_files)} 个音频文件")



def step_merge_only(input_path: Path, pause: float):
    """仅执行合并步骤（需要已有音频文件）。"""
    name = input_path.stem
    audio_dir = TEMP_DIR / name
    audio_files = sorted(audio_dir.glob("*.mp3"))

    if not audio_files:
        print(f"错误：{audio_dir} 中没有找到 MP3 文件，请先执行 --step tts")
        sys.exit(1)

    output_path = OUTPUT_DIR / f"{name}.mp3"
    print(f"\n合并 {len(audio_files)} 个音频文件...")
    merge_audio(audio_files, output_path, pause)


def main():
    parser = argparse.ArgumentParser(
        description="文章转语音 (TTS) 自动化管线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "配置说明：\n"
            "  支持在项目根目录下的 .env 文件中进行配置（参考 .env.example）：\n"
            "    LLM_API_BASE=https://api.openai.com/v1\n"
            "    LLM_API_KEY=sk-...\n"
            "    LLM_MODEL=gpt-4o-mini\n"
            "    COMFYUI_URL=http://127.0.0.1:8188\n"
            "    PAUSE_DURATION=1.0\n"
            "\n"
            "示例：\n"
            "  python tts_pipeline.py\n"
            "  python tts_pipeline.py --file 第三小节.txt\n"
            "  python tts_pipeline.py --step split\n"
        ),
    )
    parser.add_argument(
        "--step",
        choices=["split", "tts", "retts", "merge", "all"],
        default="all",
        help=(
            "执行步骤：split=仅分段, tts=全量语音合成, "
            "retts=仅重新生成指定段落并合成（需配合 --segments）, "
            "merge=仅合并, all=完整流程 (默认: all)"
        ),
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
    args = parser.parse_args()

    # ── 解析 --segments ──────────────────────────────────
    only_segments: set[int] | None = None
    if args.segments:
        try:
            only_segments = {int(s.strip()) for s in args.segments.split(",") if s.strip()}
        except ValueError:
            print(f"错误：--segments 参数格式不正确，请使用逗号分隔的整数，如 '1,3,5'")
            sys.exit(1)

    # ── 确定输入文件 ─────────────────────────────────────
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

    # ── 执行 ─────────────────────────────────────────────
    for input_file in input_files:
        if args.step == "split":
            step_split_only(input_file)
        elif args.step == "tts":
            step_tts_only(input_file, args.comfyui_url, only_segments)
        elif args.step == "retts":
            # 重新生成指定段落 → 自动重新合并
            if not only_segments:
                print("错误：--step retts 必须配合 --segments 使用，例如: --segments 1,3,5")
                sys.exit(1)
            print(f"\n[retts] 重新生成段落 {sorted(only_segments)} 并重新合成...")
            step_tts_only(input_file, args.comfyui_url, only_segments)
            step_merge_only(input_file, args.pause)
        elif args.step == "merge":
            step_merge_only(input_file, args.pause)
        else:  # all
            process_file(input_file, args.comfyui_url, args.pause)

    print("\n🎉 全部处理完成！")


if __name__ == "__main__":
    main()
