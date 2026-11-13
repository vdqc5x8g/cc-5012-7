import { useEffect, useState } from "react";
import { api } from "../api";
import type { CaptionCue, Project } from "../types";
import { geometryAt } from "./Stage";

function fmt(t: number) {
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(2).padStart(5, "0")}`;
}

export function EditorPanel({
  project, time, activeCueId, onSeek, onSelectCue, onProject, onToast,
}: {
  project: Project;
  time: number;
  activeCueId: string | null;
  onSeek: (t: number) => void;
  onSelectCue: (id: string) => void;
  onProject: (p: Project) => void;
  onToast: (m: string, bad?: boolean) => void;
}) {
  const cue = activeCueId ? project.cues.find((c) => c.id === activeCueId) : undefined;
  const [text, setText] = useState("");
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(2);

  useEffect(() => {
    if (cue) {
      setText(cue.text);
      setStart(cue.start);
      setEnd(cue.end);
    }
  }, [cue?.id, cue?.text, cue?.start, cue?.end]);

  const saveCue = async () => {
    if (!cue) return;
    if (end <= start) {
      onToast("结束时间必须大于开始时间", true);
      return;
    }
    const cues: CaptionCue[] = project.cues.map((c) =>
      c.id === cue.id ? { ...c, text, start, end } : c
    );
    try {
      onProject(await api.updateProject(project.id, { cues }));
      onToast("字幕已保存，布局已重新计算");
    } catch (e) {
      onToast(String(e), true);
    }
  };

  const addCue = async () => {
    try {
      const p = await api.addCue(project.id, {
        start: Math.round(time * 100) / 100,
        end: Math.round((time + 2) * 100) / 100,
        text: "新字幕",
      });
      onProject(p);
      const last = p.cues[p.cues.length - 1];
      if (last) onSelectCue(last.id);
      onToast("已新增字幕");
    } catch (e) {
      onToast(String(e), true);
    }
  };

  const removeCue = async () => {
    if (!cue) return;
    if (!confirm(`删除字幕「${cue.text.slice(0, 20)}」？`)) return;
    try {
      onProject(await api.deleteCue(project.id, cue.id));
      onToast("字幕已删除");
    } catch (e) {
      onToast(String(e), true);
    }
  };

  const updateSetting = async (patch: Record<string, unknown>) => {
    try {
      onProject(await api.updateProject(project.id, { settings: { ...project.settings, ...patch } }));
    } catch (e) {
      onToast(String(e), true);
    }
  };

  return (
    <div className="panel left">
      <div className="panel-head">
        时间线与编辑
        <span className="spacer" />
        <button className="small primary" onClick={addCue}>＋ 字幕</button>
      </div>
      <div className="panel-body stack">
        <div className="stack" style={{ gap: 1 }}>
          {project.cues.length === 0 && <div className="tiny muted">还没有字幕，点击右上角新增。</div>}
          {project.cues.map((c, i) => {
            const layout = project.layout.perCue[c.id];
            const geo = geometryAt(layout, time);
            const state = layout?.stale
              ? "bad"
              : layout?.partial
                ? "part"
                : layout?.solved
                  ? "ok"
                  : "bad";
            return (
              <div
                key={c.id}
                className={`cue-row ${c.id === activeCueId ? "active" : ""}`}
                onClick={() => {
                  onSelectCue(c.id);
                  if (!(c.start <= time && time < c.end)) onSeek(c.start + 0.03);
                }}
              >
                <span className="cidx">#{i + 1}</span>
                <span className="ctext" title={c.text}>{c.text}</span>
                <span className={`cstat ${state}`}>
                  {layout?.stale ? "标注变" : layout?.partial ? "部分" : layout?.solved ? (geo ? "渲染" : "已解") : "无解"}
                </span>
              </div>
            );
          })}
        </div>

        <div className="divider" />
        {!cue ? (
          <div className="tiny muted">选择一条字幕进行编辑。</div>
        ) : (
          <div className="stack" style={{ gap: 7 }}>
            <div className="section-title">编辑 #{project.cues.findIndex((c) => c.id === cue.id) + 1}</div>
            <textarea
              rows={4}
              value={text}
              onChange={(e) => setText(e.target.value)}
              style={{ resize: "vertical", width: "100%" }}
              placeholder="字幕文本"
            />
            <div className="field two">
              <label>时间范围</label>
              <input type="number" step="0.05" value={start} onChange={(e) => setStart(Number(e.target.value))} />
              <input type="number" step="0.05" value={end} onChange={(e) => setEnd(Number(e.target.value))} />
            </div>
            <div className="row">
              <button className="primary" onClick={saveCue}>保存</button>
              <button className="ghost" onClick={() => onSeek(start + 0.03)}>跳到开始</button>
              <span className="grow" />
              <button className="small danger" onClick={removeCue}>删除</button>
            </div>
            <div className="tiny muted">
              {fmt(start)} – {fmt(end)} · 人工调整后会保存为工程修订。
            </div>
          </div>
        )}

        <div className="divider" />
        <div className="section-title">排版设置</div>
        <div className="field">
          <label>字号 (px)</label>
          <input
            type="number" min={16} max={160} value={project.settings.font_size}
            onChange={(e) => updateSetting({ font_size: Number(e.target.value) })}
          />
        </div>
        <div className="field">
          <label>允许行数</label>
          <input
            type="number" min={1} max={4} value={project.settings.max_lines}
            onChange={(e) => updateSetting({ max_lines: Number(e.target.value) })}
          />
        </div>
        <div className="field">
          <label>每行字数</label>
          <input
            type="number" min={4} max={40} value={project.settings.max_chars_per_line}
            onChange={(e) => updateSetting({ max_chars_per_line: Number(e.target.value) })}
          />
        </div>
        <div className="field">
          <label>安全边距</label>
          <input
            type="number" min={0} max={200} value={project.settings.safe_margin}
            onChange={(e) => updateSetting({ safe_margin: Number(e.target.value) })}
          />
        </div>
        <div className="field">
          <label>稳定性容差</label>
          <input
            type="number" min={0} max={200} value={project.settings.sticky_tolerance_px}
            onChange={(e) => updateSetting({ sticky_tolerance_px: Number(e.target.value) })}
          />
        </div>
        <div className="field">
          <label>最小字号比例</label>
          <input
            type="number" min={0.3} max={1} step={0.05} value={project.settings.min_font_ratio}
            onChange={(e) => updateSetting({ min_font_ratio: Number(e.target.value) })}
          />
        </div>
        <div className="tiny muted" style={{ lineHeight: 1.5 }}>
          低于最小可读字号时不会继续缩小，而是报告无解。稳定性容差越大越不容易因轻微区域变化而跳动。
        </div>
      </div>
    </div>
  );
}
