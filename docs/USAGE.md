# 手语讲解视频字幕布局服务 — 使用说明

## 图形入口（手语字幕主入口）

手语字幕的编辑与验收以图形界面为主，命令行只做批处理辅助。

1. 安装与构建
   ```bash
   ./scripts/setup.sh          # 安装依赖 + 构建前端
   ```
2. 启动
   ```bash
   npm run serve               # 启动后端并在同一端口提供前端页面
   # 或：cd frontend && npm run serve
   ```
3. 打开 <http://127.0.0.1:8787>
   - 首次启动会自动生成「手语字幕验收样例」（移动人物窗口、两块互斥区域、长句、全画面占用）。
   - 页面为三栏：左「时间线与编辑」、中「预览与手动调整」、右「结果与检查」，底部为「候选位置与代价」。

### 手语字幕界面的操作

| 区域 | 能做什么 |
| --- | --- |
| 预览与手动调整 | 播放手语视频；直接拖动字幕块（含实际字形墨迹框）调整位置；拖动区域框移动/缩放人物保护区、手语动作区、重要画面。 |
| 时间线与编辑 | 选择/新增/删除字幕，修改文本与时间范围；调整字号、允许行数、每行字数、安全边距、稳定性容差、最小字号比例。 |
| 候选位置与代价 | 查看每个候选的位置、字号、行数、代价分量、冲突解释；点击「采用」即保存为工程修订。部分遮挡时展示可解/不可解时段。 |
| 结果与遮挡 | 当前时刻生效的人物保护区与遮挡来源；当前字幕冲突明细；无解报告（原因、占用率、阻挡区域、尝试记录）；全部字幕状态。 |
| 区域标注 | 新增/编辑/删除带时间范围的人工标注区域（坐标也可在画面上拖拽）。 |
| 逐帧检查 | 逐帧查看「源帧」与「按当前布局真实烧录后的合成帧」，核对字幕位置与遮挡。 |
| 导出核验 | 触发 FFmpeg 导出，并逐帧测量真实导出像素：越出安全区、侵入保护区的数量与明细。 |

### 布局规则摘要

- 不是「全部固定底部」：候选位置在安全区内全网格评估，以避开手语动作区 / 人物保护区 / 重要图文为硬约束。
- 使用真实字形墨迹尺寸（内置字体把 `upem` 归一到与 libass 字号一致，逐字 outline bbox 参与排版），并受允许行数与安全边距约束。
- 稳定性：优先沿用上一条或已采用的位置；轻微标注变化不会导致字幕上下跳动（`sticky_tolerance_px`）。
- 完全遮挡时报告「无解」，绝不缩成极小字号；部分遮挡时按可解时段分段渲染。
- 区域带 `[start,end)` 时间范围，过期框不再阻挡后续字幕。
- 关键画面全部来自人工标注；界面与 API 明确提示「未做自动识别」，不宣称覆盖所有关键帧。

## 命令行（辅助）

CLI 仅用于批量查看与导出，图形入口仍是上面的 React 应用。

```bash
PYTHONPATH=backend python3 -m signsub.cli list
PYTHONPATH=backend python3 -m signsub.cli show  <projectId>
PYTHONPATH=backend python3 -m signsub.cli export <projectId> [-o out.mp4] [--no-verify]
```

`show` 打印每条字幕的实际布局框、字号、行数、模式，以及分段渲染/无解情况；
`export` 生成带字幕 mp4 + ASS，并默认调用逐帧像素核验。

## 测试

```bash
npm test     # = python3 -m unittest discover -s backend/tests -v
```

- `backend/tests/test_layout.py`：移动人物窗口、两块互斥区域、长句换行、全画面占用无解、部分遮挡分段、过期区域、轻微区域变化稳定性、未标注提示。
- `backend/tests/test_export.py`：真实 FFmpeg 导出后用「同参数无字幕参考编码」做逐帧差异测量，断言越界 0、保护区侵入 0、完全遮挡时段无字幕像素。

## 目录

```
backend/signsub/   布局引擎（layout.py）、字形度量（fontmetrics.py）、ASS（ass.py）、FFmpeg 导出与核验（ffmpeg.py）、HTTP API（server.py）、辅助 CLI（cli.py）
frontend/src/      React 手语字幕编辑页（Stage / EditorPanel / InspectorPanel / CandidatePanel）
assets/fonts/      内置字体（字号 == em 像素，与 libass 度量一致）
media/             合成验收视频
data/projects/     工程 JSON（含修订记录）
data/exports/      导出视频与 ASS
scripts/           setup.sh 与 run-server.mjs
```
