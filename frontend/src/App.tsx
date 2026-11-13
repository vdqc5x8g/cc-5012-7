import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import Stage, { geometryAt } from "./components/Stage";
import { EditorPanel } from "./components/EditorPanel";
import { InspectorPanel } from "./components/InspectorPanel";
import { CandidatePanel } from "./components/CandidatePanel";
import type { Candidate, CandidateResult, Geometry, Project, ProjectSummary } from "./types";

export default function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [activeCueId, setActiveCueId] = useState<string | null>(null);
  const [time, setTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [showRegions, setShowRegions] = useState(true);
  const [showSafe, setShowSafe] = useState(true);
  const [showCaption, setShowCaption] = useState(true);
  const [toast, setToast] = useState<{ msg: string; bad?: boolean } | null>(null);
  const [candidateResult, setCandidateResult] = useState<CandidateResult | null>(null);
  const [selectedCand, setSelectedCand] = useState(0);
  const [dragCueId, setDragCueId] = useState<string | null>(null);
  const [manualGeometry, setManualGeometry] = useState<Geometry | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const playRef = useRef<number | null>(null);
  const lastTick = useRef<number>(0);

  const notify = useCallback((msg: string, bad?: boolean) => {
    setToast({ msg, bad });
    window.setTimeout(() => setToast((t) => (t?.msg === msg ? null : t)), 3200);
  }, []);

  // Load project list on boot.
  useEffect(() => {
    (async () => {
      try {
        const { projects: list } = await api.listProjects();
        setProjects(list);
        if (list.length === 0) {
          await api.createSample();
          const again = await api.listProjects();
          setProjects(again.projects);
          if (again.projects.length) await load(again.projects[0].id);
        } else {
          await load(list[0].id);
        }
      } catch (e) {
        setErr(String(e));
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function load(id: string) {
    try {
      const p = await api.getProject(id);
      setProject(p);
      setErr(null);
      const first = p.cues[0];
      if (first) {
        setActiveCueId(first.id);
        setTime(first.start + 0.03);
      }
    } catch (e) {
      setErr(String(e));
    }
  }

  const onProject = useCallback((p: Project) => {
    setProject(p);
    setSelectedCand(0);
  }, []);

  // Refresh candidate info whenever the active cue or project changes.
  useEffect(() => {
    if (!project || !activeCueId) {
      setCandidateResult(null);
      return;
    }
    let alive = true;
    api
      .candidates(project.id, activeCueId)
      .then((r) => {
        if (alive) setCandidateResult(r);
      })
      .catch(() => {
        if (alive) setCandidateResult(null);
      });
    return () => {
      alive = false;
    };
  }, [project?.id, activeCueId, project?.revCounter, project?.layout]);

  // Playback clock (drives overlays even without native video controls).
  useEffect(() => {
    if (!playing) {
      if (playRef.current) window.clearInterval(playRef.current);
      playRef.current = null;
      return;
    }
    lastTick.current = performance.now();
    playRef.current = window.setInterval(() => {
      const now = performance.now();
      const dt = (now - lastTick.current) / 1000;
      lastTick.current = now;
      setTime((t) => {
        const nt = t + dt;
        if (project && nt >= project.video.duration) {
          setPlaying(false);
          return project.video.duration - 0.02;
        }
        return nt;
      });
    }, 40);
    return () => {
      if (playRef.current) window.clearInterval(playRef.current);
    };
  }, [playing, project?.video.duration]);

  const activeCue = useMemo(
    () => project?.cues.find((c) => c.id === activeCueId) ?? null,
    [project, activeCueId]
  );

  const adoptCandidate = async (cand: Candidate) => {
    if (!project || !activeCueId) return;
    try {
      const p = await api.adoptCandidate(project.id, activeCueId, cand);
      onProject(p);
      notify("已采用该候选位置，并保存为工程修订");
      setDragCueId(null);
      setManualGeometry(null);
    } catch (e) {
      notify(String(e), true);
    }
  };

  const onCueDrag = async (cueId: string, dx: number, dy: number, done: boolean) => {
    if (!project) return;
    const layout = project.layout.perCue[cueId];
    const geo = geometryAt(layout, time);
    if (!geo) return;
    const nx = Math.max(0, Math.min(project.video.width - geo.ink[2], geo.ink[0] + dx));
    const ny = Math.max(0, Math.min(project.video.height - geo.ink[3], geo.ink[1] + dy));
    const moved: Geometry = { ...geo, ink: [nx, ny, geo.ink[2], geo.ink[3]] };
    setDragCueId(cueId);
    setManualGeometry(moved);
    if (!done) return;
    const cue = project.cues.find((c) => c.id === cueId);
    if (!cue) return;
    try {
      const v = await api.validate(project.id, cueId, {
        x: nx, y: ny, fontPx: geo.fontPx, lines: geo.lines.map((l) => l.text),
      });
      if (!v.valid) {
        notify(v.problems.join("；"), true);
      }
      const p = await api.adopt(project.id, cueId, {
        x: nx, y: ny, fontPx: geo.fontPx, lines: geo.lines.map((l) => l.text),
      });
      onProject(p);
      notify(v.valid ? "字幕位置已手动调整并保存" : "已保存（存在冲突，请在结果区查看）", !v.valid);
    } catch (e) {
      notify(String(e), true);
    } finally {
      setDragCueId(null);
      setManualGeometry(null);
    }
  };

  const onRegionDrag = async (
    regionId: string,
    rect: [number, number, number, number],
    done: boolean
  ) => {
    if (!project) return;
    const clamped: [number, number, number, number] = [
      Math.round(Math.max(0, Math.min(project.video.width - 20, rect[0]))),
      Math.round(Math.max(0, Math.min(project.video.height - 20, rect[1]))),
      Math.round(Math.max(20, Math.min(project.video.width, rect[2]))),
      Math.round(Math.max(20, Math.min(project.video.height, rect[3]))),
    ];
    if (!done) {
      setProject((p) =>
        p
          ? { ...p, regions: p.regions.map((r) => (r.id === regionId ? { ...r, rect: clamped } : r)) }
          : p
      );
      return;
    }
    try {
      const p = await api.updateRegion(project.id, regionId, { rect: clamped });
      onProject(p);
    } catch (e) {
      notify(String(e), true);
    }
  };

  // Space toggles playback; arrow keys step frames.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.code === "Space") {
        e.preventDefault();
        setPlaying((p) => !p);
      } else if (e.code === "ArrowLeft" || e.code === "ArrowRight") {
        e.preventDefault();
        const dt = (e.shiftKey ? 1 : 1 / (project?.video.fps || 25)) * (e.code === "ArrowRight" ? 1 : -1);
        setTime((t) => Math.max(0, Math.min((project?.video.duration ?? 0) - 0.02, t + dt)));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [project?.video.duration, project?.video.fps]);

  const switchProject = async (id: string) => {
    setPlaying(false);
    await load(id);
  };

  const addCueAtPlayhead = async () => {
    if (!project) return;
    try {
      const p = await api.addCue(project.id, {
        start: Math.round(time * 100) / 100,
        end: Math.round((time + 2) * 100) / 100,
        text: "新字幕",
      });
      onProject(p);
      const last = p.cues[p.cues.length - 1];
      if (last) setActiveCueId(last.id);
    } catch (e) {
      notify(String(e), true);
    }
  };

  if (err) {
    return (
      <div style={{ padding: 40 }}>
        <h2>无法加载工程</h2>
        <div className="mono">{err}</div>
        <div style={{ marginTop: 16 }} className="muted tiny">
          确认后端已启动（npm run server）并生成验收样例（启动时自动创建）。
        </div>
      </div>
    );
  }

  if (!project) {
    return <div style={{ padding: 40 }} className="muted">加载中…</div>;
  }

  const cueIndex = activeCue ? project.cues.findIndex((c) => c.id === activeCue.id) + 1 : 0;

  return (
    <div className="app">
      <div className="topbar">
        <div className="brand-mark">手</div>
        <h1>手语字幕布局工作台</h1>
        <select
          value={project.id}
          onChange={(e) => switchProject(e.target.value)}
          style={{ marginLeft: 8 }}
        >
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}（{p.cueCount} 条字幕）
            </option>
          ))}
        </select>
        <span className="spacer" />
        <label className="row tiny muted" style={{ gap: 4 }}>
          <input type="checkbox" checked={showRegions} onChange={(e) => setShowRegions(e.target.checked)} />
          保护/重要区域
        </label>
        <label className="row tiny muted" style={{ gap: 4 }}>
          <input type="checkbox" checked={showSafe} onChange={(e) => setShowSafe(e.target.checked)} />
          安全边距
        </label>
        <label className="row tiny muted" style={{ gap: 4 }}>
          <input type="checkbox" checked={showCaption} onChange={(e) => setShowCaption(e.target.checked)} />
          字幕
        </label>
        <button className="small" onClick={addCueAtPlayhead}>＋ 在播放头加字幕</button>
      </div>

      <EditorPanel
        project={project}
        time={time}
        activeCueId={activeCueId}
        onSeek={setTime}
        onSelectCue={setActiveCueId}
        onProject={onProject}
        onToast={notify}
      />

      <div className="panel stage">
        <div className="panel-head">
          预览与手动调整
          <span className="spacer" />
          <span className="legend">
            <span className="swatch"><span className="dot" style={{ background: "var(--signer)" }} />人物保护区</span>
            <span className="swatch"><span className="dot" style={{ background: "var(--important)" }} />重要画面</span>
            <span className="swatch"><span className="dot" style={{ background: "var(--accent)" }} />字幕墨迹</span>
            <span className="swatch"><span className="dot" style={{ background: "var(--bad)" }} />冲突</span>
          </span>
        </div>
        <Stage
          project={project}
          time={time}
          activeCueId={activeCueId}
          showRegions={showRegions}
          showSafe={showSafe}
          showCaption={showCaption}
          dragCueId={dragCueId}
          manualGeometry={manualGeometry}
          onSelectCue={setActiveCueId}
          onCueDrag={onCueDrag}
          onRegionDrag={onRegionDrag}
          settings={project.settings}
        />
        <div className="transport">
          <button onClick={() => setPlaying((p) => !p)}>{playing ? "暂停" : "播放"}</button>
          <input
            type="range"
            min={0}
            max={Math.max(0.01, project.video.duration)}
            step={0.01}
            value={Math.min(time, project.video.duration)}
            onChange={(e) => {
              setPlaying(false);
              setTime(Number(e.target.value));
            }}
          />
          <span className="time">
            {fmt(time)} / {fmt(project.video.duration)}
          </span>
          <span className="tiny muted">
            {cueIndex ? `当前 #${cueIndex}` : "无字幕"} · {project.video.width}×{project.video.height} @{" "}
            {Math.round(project.video.fps)}fps
          </span>
        </div>
      </div>

      <div className="panel bottom">
        <div className="panel-head">
          候选位置与代价
          <span className="spacer" />
          {activeCue && <span className="tiny muted">#{cueIndex} · {activeCue.text.slice(0, 28)}</span>}
        </div>
        <div className="panel-body">
          <CandidatePanel
            result={candidateResult}
            selectedIndex={selectedCand}
            onSelect={setSelectedCand}
            onAdopt={adoptCandidate}
            layout={activeCueId ? project.layout.perCue[activeCueId] : undefined}
          />
        </div>
      </div>

      <InspectorPanel
        project={project}
        time={time}
        activeCueId={activeCueId}
        onSeek={setTime}
        onSelectCue={setActiveCueId}
        onProject={onProject}
        onToast={notify}
      />

      {toast && <div className={`toast ${toast.bad ? "bad" : ""}`}>{toast.msg}</div>}
    </div>
  );
}

function fmt(t: number) {
  const m = Math.floor(t / 60);
  const s = t % 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(2).padStart(5, "0")}`;
}
