import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type {
  AvoidRegion,
  CueLayout,
  ExportJob,
  LayoutSettings,
  Project,
  RegionKind,
  Verification,
} from "../types";
import { KIND_LABEL, UnsolvedCard } from "./CandidatePanel";
import { activeRegionsAt, geometryAt } from "./Stage";

type TabKey = "result" | "regions" | "check" | "export";

const TABS: { key: TabKey; label: string; hint: string }[] = [
  { key: "result", label: "结果与遮挡", hint: "每个字幕的布局状态、人物保护区与遮挡" },
  { key: "regions", label: "区域标注", hint: "人物/手语/重要画面（带时间范围，人工标注）" },
  { key: "check", label: "逐帧检查", hint: "抽帧查看真实合成，逐帧核对越界与遮挡" },
  { key: "export", label: "导出核验", hint: "FFmpeg 导出并逐帧核验实际画面" },
];

const KIND_COLOR: Record<RegionKind, string> = {
  signer: "var(--signer)",
  sign: "var(--sign)",
  important: "var(--important)",
  custom: "var(--custom)",
};

function fmt(t: number) {
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(2).padStart(5, "0")}`;
}

export function InspectorPanel({
  project, time, activeCueId, onSeek, onSelectCue, onProject, onToast,
}: {
  project: Project;
  time: number;
  activeCueId: string | null;
  onSeek: (t: number) => void;
  onSelectCue: (id: string) => void;
  onProject: (p: Project) => void;
  onToast: (msg: string, bad?: boolean) => void;
}) {
  const [tab, setTab] = useState<TabKey>("result");
  return (
    <div className="panel right">
      <div className="panel-head">
        结果与检查
        <span className="spacer" />
        <span className="tiny muted">{fmt(time)}</span>
      </div>
      <div className="tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={tab === t.key ? "active" : ""}
            onClick={() => setTab(t.key)}
            title={t.hint}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="panel-body">
        {tab === "result" && (
          <ResultTab project={project} time={time} activeCueId={activeCueId} onSeek={onSeek} onSelectCue={onSelectCue} />
        )}
        {tab === "regions" && <RegionsTab project={project} time={time} onSeek={onSeek} onProject={onProject} onToast={onToast} />}
        {tab === "check" && <CheckTab project={project} time={time} onSeek={onSeek} />}
        {tab === "export" && <ExportTab project={project} onProject={onProject} onToast={onToast} />}
      </div>
    </div>
  );
}

function StatusTag({ layout }: { layout: CueLayout | undefined }) {
  if (!layout) return <span className="tag">未计算</span>;
  if (layout.stale) return <span className="tag bad">标注已变</span>;
  if (layout.partial) return <span className="tag warn">部分遮挡</span>;
  if (!layout.solved) return <span className="tag bad">无解</span>;
  const mode = layout.geometry?.mode;
  return (
    <span className="tag ok">
      {mode === "manual" ? "手动" : mode === "adopted" ? "已采用" : mode === "sticky" ? "稳定" : "自动"}
    </span>
  );
}

function ResultTab({
  project, time, activeCueId, onSeek, onSelectCue,
}: {
  project: Project;
  time: number;
  activeCueId: string | null;
  onSeek: (t: number) => void;
  onSelectCue: (id: string) => void;
}) {
  const active = activeRegionsAt(project.regions, time);
  const activeLayout = activeCueId ? project.layout.perCue[activeCueId] : undefined;
  const current = activeCueId ? project.cues.find((c) => c.id === activeCueId) : undefined;
  const currentAtTime =
    current && current.start <= time && time < current.end ? current : undefined;
  const geo = activeLayout && currentAtTime ? geometryAt(activeLayout, time) : null;
  const conflicts = geo?.conflicts ?? [];

  return (
    <div className="stack">
      <div className="section-title">当前时刻</div>
      <div className="card">
        <div className="row">
          <strong>人物保护区 / 遮挡</strong>
          <span className="grow" />
          <span className="tiny muted">{active.length} 个活动区域</span>
        </div>
        {active.length === 0 && <div className="tiny muted" style={{ marginTop: 4 }}>此刻没有生效的标注区域。</div>}
        {active.map((r: AvoidRegion) => (
          <div key={r.id} className="row tiny" style={{ marginTop: 4 }}>
            <span className="dot" style={{ background: KIND_COLOR[r.kind], width: 8, height: 8, borderRadius: 3 }} />
            <span>{KIND_LABEL[r.kind]}</span>
            <span className="muted">「{r.label}」</span>
            <span className="grow" />
            <span className="mono muted">
              [{Math.round(r.rect[0])},{Math.round(r.rect[1])},{Math.round(r.rect[2])},{Math.round(r.rect[3])}]
            </span>
          </div>
        ))}
        <div className="divider" />
        {current ? (
          <div className="stack" style={{ gap: 4 }}>
            <div className="row">
              <span className="tag accent">#{project.cues.findIndex((c) => c.id === current.id) + 1}</span>
              <span className="grow ctext">{current.text}</span>
              <StatusTag layout={activeLayout} />
            </div>
            <div className="tiny muted">
              {fmt(current.start)} – {fmt(current.end)} · 遮挡冲突 {conflicts.length} 处
            </div>
            {conflicts.map((c) => (
              <div key={c.regionId} className="tiny" style={{ color: "var(--bad)" }}>
                与「{c.label}」重叠 {c.overlapArea.toFixed(0)}px²，覆盖字幕 {(c.coverageOfBox * 100).toFixed(0)}%
              </div>
            ))}
            {!geo && activeLayout?.unsolved && !activeCueLayoutSolved(activeLayout, time) && (
              currentAtTime ? (
                <UnsolvedCard report={activeLayout.unsolved} />
              ) : (
                <div className="tiny muted">
                  当前播放时刻不在这条字幕的时间范围内（可通过时间轴定位到该字幕）。
                </div>
              )
            )}
          </div>
        ) : (
          <div className="tiny muted">未选中字幕。点击左侧时间线或画面中的字幕块。</div>
        )}
      </div>

      <div className="section-title">全部字幕状态</div>
      <div>
        {project.cues.map((c, i) => {
          const layout = project.layout.perCue[c.id];
          const g = geometryAt(layout, time);
          return (
            <div
              key={c.id}
              className={`cue-row ${c.id === activeCueId ? "active" : ""}`}
              onClick={() => {
                onSelectCue(c.id);
                onSeek(Math.max(c.start, Math.min(time, c.end - 0.05)));
              }}
            >
              <span className="cidx">#{i + 1}</span>
              <span className="ctext">{c.text}</span>
              {layout?.partial ? (
                <span className="cstat part">部分</span>
              ) : layout?.solved ? (
                <span className="cstat ok">{g ? "渲染" : "已解"}</span>
              ) : (
                <span className="cstat bad">无解</span>
              )}
            </div>
          );
        })}
      </div>
      <div className="card tiny muted" style={{ lineHeight: 1.5 }}>
        {project.layout.annotationNotice}
      </div>
      <div className="card tiny muted">
        安全边距 {project.settings.safe_margin}px · 字号 {project.settings.font_size}px · 至多{" "}
        {project.settings.max_lines} 行 · 每行 {project.settings.max_chars_per_line} 字
      </div>
    </div>
  );
}

function activeCueLayoutSolved(layout: CueLayout, time: number): boolean {
  return geometryAt(layout, time) !== null;
}

function RegionsTab({
  project, time, onSeek, onProject, onToast,
}: {
  project: Project;
  time: number;
  onSeek: (t: number) => void;
  onProject: (p: Project) => void;
  onToast: (m: string, bad?: boolean) => void;
}) {
  const [kind, setKind] = useState<RegionKind>("signer");
  const [label, setLabel] = useState("");
  const [start, setStart] = useState(time);
  const [end, setEnd] = useState(Math.min(project.video.duration, time + 3));
  const [rect, setRect] = useState<[string, string, string, string]>(["200", "150", "360", "320"]);

  useEffect(() => {
    setStart(time);
    setEnd(Math.min(project.video.duration, time + 3));
  }, [time, project.video.duration]);

  const add = async () => {
    const r: [number, number, number, number] = rect.map((v) => Number(v)) as [
      number, number, number, number,
    ];
    if (r.some((v) => !Number.isFinite(v)) || r[2] <= 0 || r[3] <= 0) {
      onToast("矩形无效", true);
      return;
    }
    try {
      const p = await api.addRegion(project.id, {
        kind, label: label || KIND_LABEL[kind], start, end, rect: r,
      });
      onProject(p);
      setLabel("");
      onToast("已新增区域，布局已重新计算");
    } catch (e) {
      onToast(String(e), true);
    }
  };

  const remove = async (id: string) => {
    try {
      onProject(await api.deleteRegion(project.id, id));
      onToast("区域已删除");
    } catch (e) {
      onToast(String(e), true);
    }
  };

  const updateTime = async (r: AvoidRegion, st: number, en: number) => {
    try {
      onProject(await api.updateRegion(project.id, r.id, { start: st, end: en }));
    } catch (e) {
      onToast(String(e), true);
    }
  };

  return (
    <div className="stack">
      <div className="card tiny muted" style={{ lineHeight: 1.5 }}>
        区域全部为人工标注，带时间范围。过期框不会继续阻挡后续字幕；未标注区域系统不会自动识别。
      </div>
      <div className="section-title">已标注区域</div>
      {project.regions.length === 0 && <div className="tiny muted">暂无标注。</div>}
      {project.regions.map((r) => {
        const live = r.start <= time && time < r.end;
        return (
          <div key={r.id} className="card" style={{ borderColor: live ? KIND_COLOR[r.kind] : undefined }}>
            <div className="row">
              <span className="dot" style={{ background: KIND_COLOR[r.kind], width: 9, height: 9, borderRadius: 3 }} />
              <strong>{r.label}</strong>
              <span className="tag">{KIND_LABEL[r.kind]}</span>
              {live ? <span className="tag ok">生效中</span> : <span className="tag">未生效</span>}
              <span className="grow" />
              <button className="small danger" onClick={() => remove(r.id)}>删除</button>
            </div>
            <div className="row tiny muted" style={{ marginTop: 5, flexWrap: "wrap" }}>
              <span>时间</span>
              <input
                type="number" step="0.05" value={r.start}
                style={{ width: 76 }} className="tiny"
                onChange={(e) => updateTime(r, Number(e.target.value), r.end)}
              />
              <span>–</span>
              <input
                type="number" step="0.05" value={r.end}
                style={{ width: 76 }}
                onChange={(e) => updateTime(r, r.start, Number(e.target.value))}
              />
              <span className="grow" />
              <span className="mono">[{r.rect.map((v) => Math.round(v)).join(", ")}]</span>
              <button className="small ghost" onClick={() => {
                onSeek(r.start);
              }}>跳到开始</button>
            </div>
          </div>
        );
      })}

      <div className="section-title">新增区域</div>
      <div className="card stack" style={{ gap: 7 }}>
        <div className="field">
          <label>类型</label>
          <select value={kind} onChange={(e) => setKind(e.target.value as RegionKind)}>
            <option value="signer">人物保护区</option>
            <option value="sign">手语动作区</option>
            <option value="important">重要画面（关键图文）</option>
            <option value="custom">自定义</option>
          </select>
        </div>
        <div className="field">
          <label>名称</label>
          <input type="text" value={label} placeholder={KIND_LABEL[kind]} onChange={(e) => setLabel(e.target.value)} />
        </div>
        <div className="field two">
          <label>时间范围</label>
          <input type="number" step="0.05" value={start} onChange={(e) => setStart(Number(e.target.value))} />
          <input type="number" step="0.05" value={end} onChange={(e) => setEnd(Number(e.target.value))} />
        </div>
        <div className="field two">
          <label>x / y</label>
          <input type="number" value={rect[0]} onChange={(e) => setRect([e.target.value, rect[1], rect[2], rect[3]])} />
          <input type="number" value={rect[1]} onChange={(e) => setRect([rect[0], e.target.value, rect[2], rect[3]])} />
        </div>
        <div className="field two">
          <label>宽 / 高</label>
          <input type="number" value={rect[2]} onChange={(e) => setRect([rect[0], rect[1], e.target.value, rect[3]])} />
          <input type="number" value={rect[3]} onChange={(e) => setRect([rect[0], rect[1], rect[2], e.target.value])} />
        </div>
        <div className="row">
          <button className="primary" onClick={add}>添加区域</button>
          <span className="tiny muted">也可在画面上拖动区域移动位置、拖右下角改大小。</span>
        </div>
      </div>
    </div>
  );
}

function CheckTab({
  project, time, onSeek,
}: {
  project: Project;
  time: number;
  onSeek: (t: number) => void;
}) {
  const cuesAt = project.cues.filter((c) => c.start <= time && time < c.end);
  // revCounter busts the cache: the composite changes whenever the layout is
  // re-solved at the same timestamp.
  const q = `?r=${project.revCounter}`;
  const srcUrl = `/api/projects/${project.id}/frames/${time.toFixed(2)}.jpg${q}`;
  const compUrl = `/api/projects/${project.id}/composite/${time.toFixed(2)}.png${q}`;

  return (
    <div className="stack">
      <div className="card tiny muted" style={{ lineHeight: 1.5 }}>
        逐帧检查使用当前时间点的源视频帧，以及把当前工程布局实际烧录后的合成帧。拖动时间轴或点击字幕可切换检查点。
      </div>
      <div className="row">
        <span className="tag accent">t = {fmt(time)}</span>
        <span className="grow" />
        {cuesAt.map((c) => (
          <button key={c.id} className="small" onClick={() => onSeek(c.start + 0.05)}>
            跳到 #{project.cues.findIndex((x) => x.id === c.id) + 1}
          </button>
        ))}
      </div>
      <div className="stack" style={{ gap: 8 }}>
        <div>
          <div className="section-title">源帧</div>
          <img src={srcUrl} style={{ width: "100%", borderRadius: 8, border: "1px solid var(--line)", display: "block" }} />
        </div>
        <div>
          <div className="section-title">烧录后（真实布局）</div>
          <img src={compUrl} style={{ width: "100%", borderRadius: 8, border: "1px solid var(--line)", display: "block" }} />
        </div>
      </div>
      {cuesAt.length === 0 && <div className="tiny muted">当前时刻没有字幕。</div>}
      {cuesAt.map((c, i) => {
        const layout = project.layout.perCue[c.id];
        const g = geometryAt(layout, time);
        return (
          <div key={c.id} className="card">
            <div className="row">
              <span className="tag">#{project.cues.findIndex((x) => x.id === c.id) + 1}</span>
              <span className="grow ctext">{c.text}</span>
              <StatusTag layout={layout} />
            </div>
            {g ? (
              <div className="tiny mono muted" style={{ marginTop: 4 }}>
                墨迹框 [{g.ink.map((v) => Math.round(v)).join(", ")}] · {Math.round(g.fontPx)}px ·{" "}
                {g.lines.length} 行
              </div>
            ) : (
              <div className="tiny" style={{ color: "var(--warn)", marginTop: 4 }}>
                此刻不渲染（无解或处于被完全遮挡的时段）。
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ExportTab({
  project, onProject, onToast,
}: {
  project: Project;
  onProject: (p: Project) => void;
  onToast: (m: string, bad?: boolean) => void;
}) {
  const [job, setJob] = useState<ExportJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);

  const blocked = useMemo(() => {
    const stale = Object.values(project.layout.perCue).filter((l) => l.stale);
    const unsolved = project.layout.unsolvedCueIds;
    return { stale, unsolved };
  }, [project.layout]);

  const start = async () => {
    setBusy(true);
    setJob(null);
    try {
      const { jobId } = await api.startExport(project.id);
      const poll = async (): Promise<void> => {
        const j = await api.job(jobId);
        setJob(j);
        if (j.state === "done" || j.state === "error") {
          setBusy(false);
          if (j.state === "done") {
            const out = j.result?.output ?? "";
            const name = out.split("/").pop() ?? "";
            setDownloadUrl(`/exports/${encodeURIComponent(name)}?download=1`);
            onToast(j.verification?.ok ? "导出完成，逐帧核验通过" : "导出完成，核验发现问题", !j.verification?.ok);
            onProject(await api.getProject(project.id));
          } else {
            onToast(j.message || "导出失败", true);
          }
          return;
        }
        setTimeout(poll, 700);
      };
      poll();
    } catch (e) {
      onToast(String(e), true);
      setBusy(false);
    }
  };

  const v: Verification | null = job?.verification ?? null;

  return (
    <div className="stack">
      {blocked.stale.length > 0 && (
        <div className="card bad tiny">
          有 {blocked.stale.length} 条已采用字幕与当前标注冲突，导出会被阻止或标记问题。请先重新选择布局。
        </div>
      )}
      {blocked.unsolved.length > 0 && (
        <div className="card warn tiny">
          {blocked.unsolved.length} 条字幕无解，不会渲染。请调整标注或字幕文本/设置。
        </div>
      )}
      <div className="row">
        <button className="primary" onClick={start} disabled={busy || blocked.stale.length > 0}>
          {busy ? "导出中…" : "导出字幕视频并核验"}
        </button>
        {downloadUrl && (
          <a href={downloadUrl} className="small" style={{ textDecoration: "underline" }}>下载 mp4</a>
        )}
      </div>
      {job && (
        <div className="stack">
          <div className="progress"><div style={{ width: `${Math.round(job.progress * 100)}%` }} /></div>
          <div className="tiny muted">{job.message}</div>
        </div>
      )}
      {v && (
        <div className={`card ${v.ok ? "ok" : "bad"} stack`}>
          <div className="row">
            <span className={`tag ${v.ok ? "ok" : "bad"}`}>{v.ok ? "核验通过" : "核验发现问题"}</span>
            <span className="grow" />
            <span className="tiny muted">阈值 {v.threshold}</span>
          </div>
          <div className="tiny muted">{v.method}</div>
          <div className="tiny muted">
            字幕 {v.summary.cues} 条 · 检测到像素 {v.summary.withPixels} 条 · 越出安全区{" "}
            {v.summary.outsideSafe} 条 · 侵入保护区 {v.summary.regionHits} 条
          </div>
          {Object.values(v.cues).map((c) => (
            <div key={c.cueId} className="checkrow">
              <span className={`dot ${c.ok ? "ok" : "bad"}`} />
              <span className="mono">{c.cueId.slice(-6)}</span>
              <span className="tiny muted">
                {c.renderedPixels ? `墨迹 [${c.bbox?.map((x) => Math.round(x)).join(", ")}]` : "未检测到字幕像素"}
              </span>
              {c.outsideSafe && <span className="tag bad">越界</span>}
              {c.regionHits.map((h) => (
                <span key={h.regionId} className="tag bad">侵入「{h.label}」</span>
              ))}
              <span className="grow" />
              {c.note && <span className="tiny" style={{ color: "var(--warn)" }}>{c.note}</span>}
            </div>
          ))}
        </div>
      )}
      <div className="section-title">工程修订</div>
      <div className="stack" style={{ gap: 3 }}>
        {[...project.revisionsSummary].reverse().map((r) => (
          <div key={r.n} className="tiny muted">
            <span className="mono">#{r.n}</span> [{r.kind}] {r.summary} · {r.actor}
          </div>
        ))}
      </div>
    </div>
  );
}
