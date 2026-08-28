# 运行命令

## 进入虚拟环境

```bash
source venv/bin/activate
```

## 准备文本（txt文件）放入input文件夹中

## 全文生成

```bash
python tts_pipeline.py
```

## 生成指定文本段落（用于调整某一段的语音，查看temp中某一段的txt）

```bash
python tts_pipeline.py --step retts --segments 段落序号
```

# 后期合成

## 生成立体声

## 合成背景音乐

 使用Audacity