#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
TTS Studio Web API & Backend Server
===================================
基于 FastAPI 提供现代化 Web 工作台后端接口，支持 WebSocket 实时日志流、
分段即时编辑、单段重新生成、波形流式试听与一键合并导出。
"""

import os
import sys
import json
import time
import shutil
import asyncio
import threading
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any
from urllib.parse import quote

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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks, Query, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import tts_pipeline

# ── 路径配置 ──────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent
INPUT_DIR = PROJECT_DIR / "input"
OUTPUT_DIR = PROJECT_DIR / "output"
TEMP_DIR = PROJECT_DIR / "temp"
BGM_DIR = PROJECT_DIR / "bgm"
STATIC_DIR = PROJECT_DIR / "static"
ENV_FILE = PROJECT_DIR / ".env"

INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
BGM_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# ── FastAPI 实例 ──────────────────────────────────────────────
app = FastAPI(
    title="TTS Studio API",
    description="现代化文章转语音智能工作台",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── WebSocket 日志广播管理器 ───────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._loop = None

    def set_loop(self, loop):
        self._loop = loop

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast_json(self, data: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(data)
            except Exception:
                self.disconnect(connection)

    def send_broadcast_sync(self, data: dict):
        if self._loop and self.active_connections:
            asyncio.run_coroutine_threadsafe(self.broadcast_json(data), self._loop)

ws_manager = ConnectionManager()

# ── 全局任务执行状态 ──────────────────────────────────────────
class TaskManager:
    def __init__(self):
        self.is_running = False
        self.current_task_type = None
        self.current_document = None
        self.cancel_requested = False
        self.lock = threading.Lock()

    def start_task(self, task_type: str, document: str):
        with self.lock:
            if self.is_running:
                raise RuntimeError("当前已有正在执行的任务，请等待完成或取消")
            self.is_running = True
            self.current_task_type = task_type
            self.current_document = document
            self.cancel_requested = False

        ws_manager.send_broadcast_sync({
            "type": "task_status",
            "status": "running",
            "task_type": task_type,
            "document": document,
            "timestamp": time.strftime("%H:%M:%S"),
        })

    def finish_task(self, success: bool, message: str = ""):
        with self.lock:
            self.is_running = False
            task_type = self.current_task_type
            doc = self.current_document
            self.current_task_type = None
            self.current_document = None
            self.cancel_requested = False

        ws_manager.send_broadcast_sync({
            "type": "task_status",
            "status": "completed" if success else "failed",
            "task_type": task_type,
            "document": doc,
            "message": message,
            "timestamp": time.strftime("%H:%M:%S"),
        })

    def request_cancel(self):
        with self.lock:
            if self.is_running:
                self.cancel_requested = True
                return True
            return False

    def is_cancelled(self) -> bool:
        return self.cancel_requested

task_manager = TaskManager()

def log_emitter(msg: str, level: str = "info"):
    print(f"[{level.upper()}] {msg}")
    ws_manager.send_broadcast_sync({
        "type": "log",
        "message": msg,
        "level": level,
        "timestamp": time.strftime("%H:%M:%S"),
    })

def progress_emitter(current: int, total: int, preview: str = ""):
    percent = int((current / total) * 100) if total > 0 else 0
    ws_manager.send_broadcast_sync({
        "type": "progress",
        "current": current,
        "total": total,
        "percent": percent,
        "preview": preview,
        "timestamp": time.strftime("%H:%M:%S"),
    })


# ── 数据模型 ──────────────────────────────────────────────────
class ConfigModel(BaseModel):
    LLM_API_BASE: Optional[str] = None
    LLM_API_KEY: Optional[str] = None
    LLM_MODEL: Optional[str] = None
    COMFYUI_URL: Optional[str] = None
    MAX_SEGMENT_CHARS: Optional[int] = 200
    PAUSE_DURATION: Optional[float] = 1.0
    SPEAKER_PRESET: Optional[str] = "xiaoying-best"
    SPEED: Optional[float] = 0.95
    SEED: Optional[int] = 1623340739

class DocumentCreate(BaseModel):
    name: str
    content: str

class SegmentUpdate(BaseModel):
    text: str

class SegmentsBatchSave(BaseModel):
    segments: List[str]

class PipelineRunRequest(BaseModel):
    document: str
    step: str = "all"  # all, split, tts, retts, merge, stereo, bgm
    segments: Optional[List[int]] = None
    comfyui_url: Optional[str] = None
    speaker_preset: Optional[str] = None
    speed: Optional[float] = None
    seed: Optional[int] = None
    pause: Optional[float] = None
    stereo: Optional[bool] = True
    bitrate: Optional[str] = "320k"
    bgm_file: Optional[str] = None
    bgm_volume: Optional[float] = 0.15
    custom_prompt: Optional[str] = None
    max_chars: Optional[int] = None


# ══════════════════════════════════════════════════════════════
#  API 路由实现
# ══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_event():
    loop = asyncio.get_running_loop()
    ws_manager.set_loop(loop)
    tts_pipeline.reload_env()


@app.get("/api/status")
def get_system_status():
    """获取系统服务（ComfyUI / LLM / FFmpeg）运行健康状态"""
    tts_pipeline.reload_env()
    comfy_ok, comfy_msg = tts_pipeline.check_comfyui_health()
    llm_ok, llm_msg = tts_pipeline.check_llm_health()
    ffmpeg_ok, ffmpeg_msg = tts_pipeline.check_ffmpeg_health()

    return {
        "comfyui": {"status": comfy_ok, "message": comfy_msg, "url": os.environ.get("COMFYUI_URL", tts_pipeline.COMFYUI_URL)},
        "llm": {"status": llm_ok, "message": llm_msg, "model": os.environ.get("LLM_MODEL", "gpt-4o-mini")},
        "ffmpeg": {"status": ffmpeg_ok, "message": ffmpeg_msg},
        "task": {
            "is_running": task_manager.is_running,
            "current_task_type": task_manager.current_task_type,
            "current_document": task_manager.current_document,
        }
    }


@app.get("/api/config")
def get_config():
    """获取当前配置"""
    tts_pipeline.reload_env()
    return {
        "LLM_API_BASE": os.environ.get("LLM_API_BASE", ""),
        "LLM_API_KEY": os.environ.get("LLM_API_KEY", ""),
        "LLM_MODEL": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        "COMFYUI_URL": os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188"),
        "MAX_SEGMENT_CHARS": int(os.environ.get("MAX_SEGMENT_CHARS", "200")),
        "PAUSE_DURATION": float(os.environ.get("PAUSE_DURATION", "1.0")),
        "SPEAKER_PRESET": os.environ.get("SPEAKER_PRESET", "xiaoying-best"),
        "SPEED": float(os.environ.get("SPEED", "0.95")),
        "SEED": int(os.environ.get("SEED", "1623340739")),
    }


@app.post("/api/config")
def save_config(config: ConfigModel):
    """保存配置至 .env 文件"""
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    env_dict = {}
    for line in lines:
        line_str = line.strip()
        if line_str and not line_str.startswith("#") and "=" in line_str:
            k, v = line_str.split("=", 1)
            env_dict[k.strip()] = v.strip()

    # 更新传入的值
    data = config.dict(exclude_none=True)
    for k, v in data.items():
        env_dict[k] = str(v)
        os.environ[k] = str(v)

    out_lines = [
        "# ── LLM 配置 ─────────────────────────",
        f"LLM_API_BASE={env_dict.get('LLM_API_BASE', '')}",
        f"LLM_API_KEY={env_dict.get('LLM_API_KEY', '')}",
        f"LLM_MODEL={env_dict.get('LLM_MODEL', 'gpt-4o-mini')}",
        "",
        "# ── ComfyUI 配置 ─────────────────────",
        f"COMFYUI_URL={env_dict.get('COMFYUI_URL', 'http://127.0.0.1:8188')}",
        "",
        "# ── 音频与切分参数 ─────────────────────",
        f"MAX_SEGMENT_CHARS={env_dict.get('MAX_SEGMENT_CHARS', '200')}",
        f"PAUSE_DURATION={env_dict.get('PAUSE_DURATION', '1.0')}",
        f"SPEAKER_PRESET={env_dict.get('SPEAKER_PRESET', 'xiaoying-best')}",
        f"SPEED={env_dict.get('SPEED', '0.95')}",
        f"SEED={env_dict.get('SEED', '1623340739')}",
    ]

    ENV_FILE.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    tts_pipeline.reload_env()
    log_emitter("系统配置已更新", "success")
    return {"status": "ok", "message": "配置保存成功"}


# ── 文章管理接口 ──────────────────────────────────────────────

@app.get("/api/documents")
def list_documents():
    """获取所有文档列表与处理状态"""
    docs = []
    # 扫描 input 目录下 txt
    for f in sorted(INPUT_DIR.glob("*.txt")):
        name = f.stem
        try:
            content = f.read_text(encoding="utf-8")
            char_count = len(content)
        except Exception:
            char_count = 0

        # 检查 temp 目录
        seg_dir = TEMP_DIR / name
        txt_files = list(seg_dir.glob("*.txt")) if seg_dir.exists() else []
        mp3_files = list(seg_dir.glob("*.mp3")) if seg_dir.exists() else []
        seg_count = len(txt_files)
        audio_seg_count = len(mp3_files)

        # 检查 output 目录
        out_mp3 = OUTPUT_DIR / f"{name}.mp3"
        has_output = out_mp3.exists()
        output_size = out_mp3.stat().st_size if has_output else 0
        output_mtime = out_mp3.stat().st_mtime if has_output else 0

        docs.append({
            "name": name,
            "filename": f.name,
            "char_count": char_count,
            "seg_count": seg_count,
            "audio_seg_count": audio_seg_count,
            "has_output": has_output,
            "output_size": output_size,
            "output_mtime": output_mtime,
            "output_url": f"/api/audio/output/{name}" if has_output else None,
        })
    return {"documents": docs}


@app.get("/api/documents/{name}")
def get_document_content(name: str):
    """获取单篇文档完整原文"""
    f = INPUT_DIR / f"{name}.txt"
    if not f.exists():
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"name": name, "content": f.read_text(encoding="utf-8")}


@app.post("/api/documents")
def save_document(doc: DocumentCreate):
    """新建或覆盖保存文档"""
    name = doc.name.strip().replace(".txt", "")
    if not name:
        raise HTTPException(status_code=400, detail="文档名称不能为空")
    
    file_path = INPUT_DIR / f"{name}.txt"
    file_path.write_text(doc.content, encoding="utf-8")
    log_emitter(f"文档已保存: {file_path.name}", "success")
    return {"status": "ok", "name": name, "chars": len(doc.content)}


@app.delete("/api/documents/{name}")
def delete_document(name: str, clean_all: bool = Query(True, description="是否同时清理临时和输出文件")):
    """删除文档"""
    file_path = INPUT_DIR / f"{name}.txt"
    if file_path.exists():
        file_path.unlink()

    if clean_all:
        seg_dir = TEMP_DIR / name
        if seg_dir.exists():
            shutil.rmtree(seg_dir, ignore_errors=True)
        out_file = OUTPUT_DIR / f"{name}.mp3"
        if out_file.exists():
            out_file.unlink()

    log_emitter(f"文档及关联文件已删除: {name}", "info")
    return {"status": "ok", "message": "删除成功"}


# ── 分段管理与微调接口 ────────────────────────────────────────

@app.get("/api/documents/{name}/segments")
def get_document_segments(name: str):
    """获取指定文章的所有分段文本及音频状态"""
    seg_dir = TEMP_DIR / name
    if not seg_dir.exists():
        return {"name": name, "segments": [], "total": 0}

    txt_files = sorted(seg_dir.glob("*.txt"))
    segments = []

    for i, tf in enumerate(txt_files, 1):
        try:
            text = tf.read_text(encoding="utf-8").strip()
        except Exception:
            text = ""

        # 对应音频文件
        audio_file = seg_dir / f"{tf.stem}.mp3"
        has_audio = audio_file.exists()
        audio_size = audio_file.stat().st_size if has_audio else 0
        audio_mtime = audio_file.stat().st_mtime if has_audio else 0

        try:
            idx = int(tf.stem)
        except ValueError:
            idx = i

        segments.append({
            "idx": idx,
            "filename": tf.name,
            "text": text,
            "chars": len(text),
            "has_audio": has_audio,
            "audio_url": f"/api/audio/segment/{name}/{tf.stem}.mp3" if has_audio else None,
            "audio_size": audio_size,
            "audio_mtime": audio_mtime,
        })

    # 按 idx 排序
    segments.sort(key=lambda s: s["idx"])
    return {"name": name, "segments": segments, "total": len(segments)}


@app.post("/api/documents/{name}/segment/{idx}")
def update_single_segment(name: str, idx: int, data: SegmentUpdate):
    """即时更新单段文本"""
    seg_dir = TEMP_DIR / name
    seg_dir.mkdir(parents=True, exist_ok=True)
    file_path = seg_dir / f"{idx:03d}.txt"
    file_path.write_text(data.text.strip(), encoding="utf-8")
    return {"status": "ok", "idx": idx, "chars": len(data.text.strip())}


@app.post("/api/documents/{name}/segments")
def save_all_segments(name: str, data: SegmentsBatchSave):
    """全量重写分段列表"""
    seg_dir = TEMP_DIR / name
    seg_dir.mkdir(parents=True, exist_ok=True)

    # 备份已有音频
    existing_audios = {f.stem: f.read_bytes() for f in seg_dir.glob("*.mp3")}

    # 清理所有旧 txt 与 mp3
    for f in seg_dir.glob("*.*"):
        f.unlink()

    # 写入新 txt
    for i, seg_text in enumerate(data.segments, 1):
        idx_str = f"{i:03d}"
        tf = seg_dir / f"{idx_str}.txt"
        tf.write_text(seg_text.strip(), encoding="utf-8")
        # 如果老音频有对应 key 则恢复
        if idx_str in existing_audios:
            af = seg_dir / f"{idx_str}.mp3"
            af.write_bytes(existing_audios[idx_str])

    log_emitter(f"已更新 {len(data.segments)} 个分段", "success")
    return {"status": "ok", "total": len(data.segments)}


@app.post("/api/documents/{name}/segment/split")
def split_segment(name: str, idx: int = Query(...), position: int = Query(...)):
    """在光标位置拆分段落"""
    seg_dir = TEMP_DIR / name
    if not seg_dir.exists():
        raise HTTPException(status_code=404, detail="未找到分段目录")

    txt_files = sorted(seg_dir.glob("*.txt"))
    all_texts = [f.read_text(encoding="utf-8") for f in txt_files]

    if idx < 1 or idx > len(all_texts):
        raise HTTPException(status_code=400, detail="段落索引无效")

    target_idx = idx - 1
    target_text = all_texts[target_idx]
    
    if position <= 0 or position >= len(target_text):
        raise HTTPException(status_code=400, detail="拆分位置超出范围")

    part1 = target_text[:position].strip()
    part2 = target_text[position:].strip()

    new_texts = all_texts[:target_idx] + [part1, part2] + all_texts[target_idx+1:]
    
    # 重新落盘
    for f in seg_dir.glob("*.*"):
        f.unlink()

    for i, t in enumerate(new_texts, 1):
        (seg_dir / f"{i:03d}.txt").write_text(t, encoding="utf-8")

    log_emitter(f"已将第 {idx} 段拆分为 2 段", "info")
    return {"status": "ok", "total": len(new_texts)}


@app.post("/api/documents/{name}/segment/merge_next")
def merge_with_next_segment(name: str, idx: int = Query(...)):
    """与下一段合并"""
    seg_dir = TEMP_DIR / name
    if not seg_dir.exists():
        raise HTTPException(status_code=404, detail="未找到分段目录")

    txt_files = sorted(seg_dir.glob("*.txt"))
    all_texts = [f.read_text(encoding="utf-8") for f in txt_files]

    if idx < 1 or idx >= len(all_texts):
        raise HTTPException(status_code=400, detail="无下一段可合并")

    target_idx = idx - 1
    merged_text = (all_texts[target_idx] + " " + all_texts[target_idx + 1]).strip()

    new_texts = all_texts[:target_idx] + [merged_text] + all_texts[target_idx+2:]

    for f in seg_dir.glob("*.*"):
        f.unlink()

    for i, t in enumerate(new_texts, 1):
        (seg_dir / f"{i:03d}.txt").write_text(t, encoding="utf-8")

    log_emitter(f"已将第 {idx} 段与第 {idx+1} 段合并", "info")
    return {"status": "ok", "total": len(new_texts)}


@app.post("/api/documents/{name}/segment/delete")
def delete_segment(name: str, idx: int = Query(...)):
    """删除指定段落"""
    seg_dir = TEMP_DIR / name
    if not seg_dir.exists():
        raise HTTPException(status_code=404, detail="未找到分段目录")

    txt_files = sorted(seg_dir.glob("*.txt"))
    all_texts = [f.read_text(encoding="utf-8") for f in txt_files]

    if idx < 1 or idx > len(all_texts):
        raise HTTPException(status_code=400, detail="段落序号超出范围")

    target_idx = idx - 1
    new_texts = all_texts[:target_idx] + all_texts[target_idx+1:]

    for f in seg_dir.glob("*.*"):
        f.unlink()

    for i, t in enumerate(new_texts, 1):
        (seg_dir / f"{i:03d}.txt").write_text(t, encoding="utf-8")

    log_emitter(f"已删除第 {idx} 段", "info")
    return {"status": "ok", "total": len(new_texts)}


@app.post("/api/documents/{name}/segment/insert")
def insert_segment(name: str, idx: int = Query(..., description="在此序号之后插入"), text: str = Query("")):
    """插入新段落"""
    seg_dir = TEMP_DIR / name
    seg_dir.mkdir(parents=True, exist_ok=True)
    txt_files = sorted(seg_dir.glob("*.txt"))
    all_texts = [f.read_text(encoding="utf-8") for f in txt_files]

    insert_pos = min(max(0, idx), len(all_texts))
    new_texts = all_texts[:insert_pos] + [text or "新段落内容"] + all_texts[insert_pos:]

    for f in seg_dir.glob("*.*"):
        f.unlink()

    for i, t in enumerate(new_texts, 1):
        (seg_dir / f"{i:03d}.txt").write_text(t, encoding="utf-8")

    log_emitter(f"已在位置 {insert_pos+1} 插入新段落", "info")
    return {"status": "ok", "total": len(new_texts)}


# ── 背景音乐 (BGM) 管理接口 ──────────────────────────────────

_audio_duration_cache = {}

def get_audio_duration(file_path: Path) -> float:
    """获取音频文件时长（秒），带内存缓存"""
    if not file_path.exists():
        return 0.0
    st_mtime = file_path.stat().st_mtime
    key = (str(file_path.resolve()), st_mtime)
    if key in _audio_duration_cache:
        return _audio_duration_cache[key]
    
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            dur = float(res.stdout.strip())
            _audio_duration_cache[key] = dur
            return dur
    except Exception:
        pass
    return 0.0

def format_duration(seconds: float) -> str:
    if not seconds or seconds <= 0:
        return "00:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


@app.get("/api/bgm")
def list_bgm_files():
    """获取备选背景音乐列表（包含精确时长）"""
    audio_exts = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"}
    files = []
    for f in sorted(BGM_DIR.glob("*.*")):
        if f.suffix.lower() in audio_exts:
            dur = get_audio_duration(f)
            files.append({
                "filename": f.name,
                "size": f.stat().st_size,
                "duration": dur,
                "duration_formatted": format_duration(dur),
                "mtime": f.stat().st_mtime,
                "url": f"/api/audio/bgm/{f.name}",
            })
    return {"bgm_files": files}


@app.post("/api/bgm/upload")
async def upload_bgm_file(file: UploadFile = File(...)):
    """上传新的背景音乐文件到 bgm/ 目录"""
    filename = file.filename
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    dest = BGM_DIR / filename
    with open(dest, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    log_emitter(f"背景音乐已上传: {filename}", "success")
    return {"status": "ok", "filename": filename, "url": f"/api/audio/bgm/{filename}"}


@app.delete("/api/bgm/{filename}")
def delete_bgm_file(filename: str):
    """删除背景音乐文件"""
    target = BGM_DIR / filename
    if target.exists():
        target.unlink()
        log_emitter(f"背景音乐已删除: {filename}", "info")
        return {"status": "ok", "message": "删除成功"}
    raise HTTPException(status_code=404, detail="背景音乐文件不存在")


@app.get("/api/audio/bgm/{filename}")
def stream_bgm_audio(filename: str):
    """流式播放/试听背景音乐"""
    target = BGM_DIR / filename
    if not target.exists():
        raise HTTPException(status_code=404, detail="背景音乐文件不存在")
    return FileResponse(path=target, media_type="audio/mpeg")


# ── 流水线执行接口 ──────────────────────────────────────────

def run_pipeline_worker(req: PipelineRunRequest):
    """后台流水线工作线程"""
    name = req.document
    input_file = INPUT_DIR / f"{name}.txt"

    if not input_file.exists():
        task_manager.finish_task(False, f"输入文件不存在: {name}.txt")
        log_emitter(f"错误: 输入文件不存在: {name}.txt", "error")
        return

    tts_pipeline.reload_env()
    comfyui_url = req.comfyui_url or os.environ.get("COMFYUI_URL", tts_pipeline.COMFYUI_URL)
    pause = req.pause if req.pause is not None else float(os.environ.get("PAUSE_DURATION", tts_pipeline.PAUSE_DURATION))
    speaker_preset = req.speaker_preset or os.environ.get("SPEAKER_PRESET", "xiaoying-best")
    speed = req.speed if req.speed is not None else float(os.environ.get("SPEED", "0.95"))
    seed = req.seed if req.seed is not None else int(os.environ.get("SEED", "1623340739"))
    max_chars = req.max_chars or int(os.environ.get("MAX_SEGMENT_CHARS", "200"))

    stereo = req.stereo if req.stereo is not None else True
    bitrate = req.bitrate or os.environ.get("AUDIO_BITRATE", "320k")

    bgm_path = None
    if req.bgm_file:
        cand = BGM_DIR / req.bgm_file
        if cand.exists():
            bgm_path = cand
    bgm_volume = req.bgm_volume if req.bgm_volume is not None else 0.15

    try:
        step = req.step

        if step == "split":
            log_emitter(f"开始对 [{name}] 执行智能分段...", "info")
            tts_pipeline.step_split_only(
                input_file,
                custom_prompt=req.custom_prompt,
                max_chars=max_chars,
                log_callback=log_emitter,
            )
            log_emitter(f"[{name}] 智能分段完成！", "success")

        elif step in ["tts", "retts"]:
            only_set = set(req.segments) if req.segments else None
            if only_set:
                log_emitter(f"开始对 [{name}] 重新生成指定分段: {sorted(only_set)} ...", "info")
            else:
                log_emitter(f"开始对 [{name}] 执行全量语音合成...", "info")

            tts_pipeline.step_tts_only(
                input_path=input_file,
                comfyui_url=comfyui_url,
                only_segments=only_set,
                speaker_preset=speaker_preset,
                speed=speed,
                seed=seed,
                log_callback=log_emitter,
                progress_callback=progress_emitter,
                cancel_check=task_manager.is_cancelled,
            )

            # retts 模式自动重新合并
            if step == "retts":
                log_emitter(f"正在自动重新合并音频 (320Kbps 立体声)...", "info")
                tts_pipeline.step_merge_only(
                    input_file,
                    pause=pause,
                    stereo=stereo,
                    bitrate=bitrate,
                    bgm_path=bgm_path,
                    bgm_volume=bgm_volume,
                    log_callback=log_emitter,
                )

            log_emitter(f"[{name}] 语音生成完成！", "success")

        elif step == "merge":
            log_emitter(f"开始对 [{name}] 执行音频合并（段间停顿 {pause}s，立体声={stereo}，码率={bitrate}）...", "info")
            tts_pipeline.step_merge_only(
                input_file,
                pause=pause,
                stereo=stereo,
                bitrate=bitrate,
                bgm_path=bgm_path,
                bgm_volume=bgm_volume,
                log_callback=log_emitter,
            )
            log_emitter(f"[{name}] 音频合并完成！", "success")

        elif step == "stereo":
            log_emitter(f"开始对 [{name}] 转换为 320Kbps 双声道立体声母带...", "info")
            tts_pipeline.step_stereo_only(
                input_file,
                bitrate=bitrate,
                log_callback=log_emitter,
            )
            log_emitter(f"[{name}] 立体声转换完成！", "success")

        elif step == "bgm":
            if not bgm_path:
                raise ValueError(f"未找到指定的背景音乐文件: {req.bgm_file}")
            log_emitter(f"开始对 [{name}] 合成背景音乐 ({bgm_path.name}, 音量 {int(bgm_volume * 100)}%)...", "info")
            tts_pipeline.step_bgm_only(
                input_file,
                bgm_path=bgm_path,
                bgm_volume=bgm_volume,
                bitrate=bitrate,
                log_callback=log_emitter,
            )
            log_emitter(f"[{name}] 背景音乐合成完成！", "success")

        elif step == "all":
            log_emitter(f"开始执行 [{name}] 全流程自动化生成（包含 320Kbps 立体声与 BGM 混音）...", "info")
            tts_pipeline.process_file(
                input_path=input_file,
                comfyui_url=comfyui_url,
                pause=pause,
                speaker_preset=speaker_preset,
                speed=speed,
                seed=seed,
                stereo=stereo,
                bitrate=bitrate,
                bgm_path=bgm_path,
                bgm_volume=bgm_volume,
                log_callback=log_emitter,
                progress_callback=progress_emitter,
                cancel_check=task_manager.is_cancelled,
            )
            log_emitter(f"🎉 [{name}] 全流程处理完成！", "success")

        task_manager.finish_task(True, "任务执行成功")

    except Exception as e:
        err_msg = str(e)
        log_emitter(f"任务执行异常: {err_msg}", "error")
        task_manager.finish_task(False, err_msg)


@app.post("/api/pipeline/run")
def run_pipeline(req: PipelineRunRequest, background_tasks: BackgroundTasks):
    """启动流水线任务"""
    try:
        task_manager.start_task(req.step, req.document)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    thread = threading.Thread(target=run_pipeline_worker, args=(req,), daemon=True)
    thread.start()

    return {"status": "started", "task_type": req.step, "document": req.document}


@app.post("/api/pipeline/cancel")
def cancel_pipeline():
    """请求中止当前任务"""
    if task_manager.request_cancel():
        log_emitter("已发送任务中止信号...", "warning")
        return {"status": "ok", "message": "中止信号已发送"}
    return {"status": "ok", "message": "当前没有正在运行的任务"}


# ── 音频流媒体与下载接口 ──────────────────────────────────────

@app.get("/api/audio/segment/{name}/{filename}")
def stream_segment_audio(name: str, filename: str):
    """流式返回单段音频"""
    audio_path = TEMP_DIR / name / filename
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="音频文件不存在")
    return FileResponse(
        path=audio_path,
        media_type="audio/mpeg",
    )


@app.get("/api/audio/output/{name}")
def stream_output_audio(name: str):
    """流式返回最终合并音频"""
    clean_name = name.replace(".mp3", "")
    audio_path = OUTPUT_DIR / f"{clean_name}.mp3"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="最终合成音频不存在")
    return FileResponse(
        path=audio_path,
        media_type="audio/mpeg",
    )


@app.get("/api/audio/download/{name}")
def download_output_audio(name: str):
    """下载最终合并音频 (支持中文文件名 RFC 5987 / RFC 6266 标准)"""
    clean_name = name.replace(".mp3", "")
    audio_path = OUTPUT_DIR / f"{clean_name}.mp3"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="音频文件不存在")
    encoded_filename = quote(f"{clean_name}.mp3")
    return FileResponse(
        path=audio_path,
        media_type="audio/mpeg",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


# ── WebSocket 实时日志通道 ────────────────────────────────────

@app.websocket("/ws/logs")
async def websocket_logs_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        # 发送连接问候与当前状态
        await websocket.send_json({
            "type": "connected",
            "message": "实时日志信道已建立",
            "timestamp": time.strftime("%H:%M:%S"),
            "task": {
                "is_running": task_manager.is_running,
                "current_task_type": task_manager.current_task_type,
                "current_document": task_manager.current_document,
            }
        })
        while True:
            # 保持长连接并接收客户端心跳
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


# ── 静态资源与页面挂载 ────────────────────────────────────────

@app.get("/")
def serve_spa():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>TTS Studio 前端静态文件加载中...</h1>")
    return FileResponse(index_file)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main():
    import uvicorn
    import webbrowser

    port = 8000
    host = "127.0.0.1"
    url = f"http://{host}:{port}"
    print(f"\n========================================================")
    print(f"  🎙️ TTS Studio Web 工作台正在启动...")
    print(f"  浏览器访问地址: {url}")
    print(f"========================================================\n")

    def open_browser():
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()

