# 参考命令

### 1. 合成多段音频并在中间添加0.5秒静音

```bash
ffmpeg -i "第二小节_00000.mp3" `
       -i "第二小节_00001.mp3" `
       -i "第二小节_00002.mp3" `
       -i "第二小节_00005.mp3" `
       -i "第二小节_00007.mp3" `
       -i "第二小节_00008.mp3" `
       -filter_complex '[0:a]apad=pad_dur=0.5[a0];[1:a]apad=pad_dur=0.5[a1];[2:a]apad=pad_dur=0.5[a2];[3:a]apad=pad_dur=0.5[a3];[4:a]apad=pad_dur=0.5[a4];[5:a]apad=pad_dur=0.5[a5];[a0][a1][a2][a3][a4][a5][6:a]concat=n=7:v=0:a=1[out]' `
       -map '[out]' 第二小节.mp3
```
