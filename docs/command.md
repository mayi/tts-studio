# 运行指南与使用方式

## 方式一：Web 智能工作台（推荐，最现代化便捷）

双击运行项目根目录下的 `start_web.ps1`（或在 PowerShell 中执行 `.\start_web.ps1`），或在命令行中运行：

```bash
# 进入虚拟环境并启动 Web 界面
.\venv\Scripts\python.exe web_app.py
```

启动后将自动在默认浏览器中打开 **`http://127.0.0.1:8000`**。

### ✨ Web 端核心功能：
1. **文章章节管理**：支持可视化新建、编辑原文、从本地上传 `.txt`、统计字符字数与预计朗读时长。
2. **智能分段 (LLM Split)**：一键调用大模型进行播客风格断句，支持实时调整单段最大字数（默认200字）。
3. **分段调优与即时试听 (Segment Studio)**：
   - 每段文本支持即时修改并自动保存；
   - 每段配备独立试听播放器；
   - **单段秒级重生成**：点击“重新生成此段”，无需重新跑完整篇即可针对有瑕疵的发音进行调优；
   - 支持段落合并、在指定位置拆分段落、插入新段落与批量选中重生成。
4. **母带合并与立体声 320Kbps 导出 (Audio Master)**：
   - WaveSurfer 波形可视化播放器，支持 0.75x~2.0x 倍速播放与快进快退；
   - 自动将单声道音频映射合并为标准双声道立体声（44.1kHz, 320Kbps MP3）；
   - 一键调节段间静音停顿秒数；
   - 一键下载最终完整 MP3 音频。
5. **背景音乐合成 (BGM Mixing)**：
   - 备选背景音乐独立存放于项目 `bgm/` 文件夹中；
   - 支持在 Web 界面一键上传新背景音乐或删除已有背景音乐；
   - 支持在 Web 界面在线试听所选背景音乐；
   - 背景音乐音量可自由拖动调节（0%~50%，推荐 10%~20%）；
   - 混音算法自动循环匹配人声长度，并在结尾平滑淡出，不压过人声 100% 饱满度。
6. **实时控制台与诊断**：WebSocket 毫秒级流式回显执行日志与进度，并在设置面板中一键诊断 ComfyUI、LLM 和 FFmpeg。

---

## 方式二：命令行 CLI 模式

### 1. 进入虚拟环境

```bash
# Windows
.\venv\Scripts\activate.bat
```

### 2. 全文生成（支持立体声与背景音乐）

```bash
python tts_pipeline.py
# 或指定单一文件并混入背景音乐
python tts_pipeline.py --file 第8小节.txt --bgm "bgm/calm_piano.mp3" --bgm-volume 0.15
```

### 3. 生成指定文本段落并自动重新合并

```bash
python tts_pipeline.py --step retts --segments 1,3,5 --file 第8小节.txt
```

### 4. 仅执行各阶段步骤

```bash
python tts_pipeline.py --step split    # 仅分段
python tts_pipeline.py --step tts      # 仅全量语音合成
python tts_pipeline.py --step merge    # 仅音频合并（自动转为 320Kbps 立体声）
python tts_pipeline.py --step stereo   # 仅将已合并的音频转换为 320Kbps 双声道立体声
python tts_pipeline.py --step bgm --bgm "bgm/piano.mp3" --bgm-volume 0.15 --file 第8小节.txt  # 仅将已合并音频混入背景音乐
```

### 5. 立体声与码率高级参数

```bash
# 全流程生成并指定 320k 双声道立体声（默认已开启）
python tts_pipeline.py --file 第8小节.txt --stereo --bitrate 320k
```