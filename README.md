# 手语讲解视频字幕布局服务

输入字幕时间线、讲解者窗口与人工给定的重要画面区域，由 Python 计算候选位置，
React 预览与人工调整，最终由 FFmpeg 输出字幕视频并逐帧核验真实像素。

- **图形入口（手语字幕主入口）**：<http://127.0.0.1:8787>，启动方式见 [docs/USAGE.md](docs/USAGE.md)。
- **命令行**：仅作批量辅助，`python -m signsub.cli list|show|export`。

快速开始：

```bash
./scripts/setup.sh     # 安装依赖并构建前端
npm run serve          # 打开 http://127.0.0.1:8787
npm test               # 布局 + 导出像素核验 + 避让性能场景测试
```

避让查询优化（复用仍有效的几何信息、快速动作即时失效、成本分解）见
[docs/PERF.md](docs/PERF.md)；基准：`python3 scripts/perf_avoidance.py`。
