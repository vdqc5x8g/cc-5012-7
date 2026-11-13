import { useEffect, useMemo, useRef, useState } from "react";
import type {
  AvoidRegion,
  CueLayout,
  Geometry,
  LayoutSettings,
  Project,
  RegionKind,
} from "../types";

const KIND_LABEL: Record<RegionKind, string> = {
  signer: "人物保护区",
  sign: "手语动作区",
  important: "重要画面",
  custom: "自定义区域",
};

export interface DragState {
  cueId: string | null;
  regionId: string | null;
  mode: "move" | "resize" | null;
}

interface StageProps {
  project: Project;
  time: number;
  activeCueId: string | null;
  showRegions: boolean;
  showSafe: boolean;
  showCaption: boolean;
  dragCueId: string | null;
  manualGeometry: Geometry | null;
  onSelectCue: (id: string) => void;
  onCueDrag: (cueId: string, dx: number, dy: number, done: boolean) => void;
  onRegionDrag: (regionId: string, rect: [number, number, number, number], done: boolean) => void;
  settings: LayoutSettings;
}

/** Map of cueId -> geometry visible at the current time (segments aware). */
export function geometryAt(layout: CueLayout | undefined, t: number): Geometry | null {
  if (!layout) return null;
  if (layout.segments && layout.segments.length) {
    for (const seg of layout.segments) {
      if (seg.solved && seg.geometry && t >= seg.start && t < seg.end) return seg.geometry;
    }
    return null;
  }
  return layout.solved ? layout.geometry : null;
}

export function activeRegionsAt(regions: AvoidRegion[], t: number): AvoidRegion[] {
  return regions.filter((r) => r.start <= t && t < r.end);
}

export default function Stage(props: StageProps) {
  const {
    project, time, activeCueId, showRegions, showSafe, showCaption,
    dragCueId, manualGeometry, onSelectCue, onCueDrag, onRegionDrag, settings,
  } = props;
  const wrapRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [scale, setScale] = useState(0.5);
  const [drag, setDrag] = useState<DragState | null>(null);
  const dragOrigin = useRef<{ x: number; y: number; rect?: [number, number, number, number] } | null>(null);

  const W = project.video.width;
  const H = project.video.height;

  // Fit stage into available space (letterbox, maintaining aspect).
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const pad = 24;
      const aw = el.clientWidth - pad;
      const ah = el.clientHeight - pad;
      setScale(Math.max(0.05, Math.min(aw / W, ah / H)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [W, H]);

  // Keep video element in sync with the requested timeline position.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    if (Math.abs(v.currentTime - time) > 0.06) {
      try {
        v.currentTime = time;
      } catch {
        /* seeking before metadata */
      }
    }
  }, [time]);

  const activeRegions = useMemo(() => activeRegionsAt(project.regions, time), [project.regions, time]);

  const visible: { cueId: string; geo: Geometry; layout: CueLayout }[] = useMemo(() => {
    const out: { cueId: string; geo: Geometry; layout: CueLayout }[] = [];
    for (const cue of project.cues) {
      if (!(cue.start <= time && time < cue.end)) continue;
      const layout = project.layout.perCue[cue.id];
      if (!layout) continue;
      let geo = geometryAt(layout, time);
      if (dragCueId === cue.id && manualGeometry) geo = manualGeometry;
      if (geo) out.push({ cueId: cue.id, geo, layout });
    }
    return out;
  }, [project.cues, project.layout, time, dragCueId, manualGeometry]);

  const activeCue = project.cues.find((c) => c.id === activeCueId);
  const activeCueAtTime =
    activeCue && activeCue.start <= time && time < activeCue.end ? activeCue : null;
  const activeCueLayout = activeCueAtTime ? project.layout.perCue[activeCueAtTime.id] : null;
  const activeUnsolved =
    activeCueLayout && !geometryAt(activeCueLayout, time) ? activeCueLayout : null;

  const beginCueDrag = (e: React.PointerEvent, cueId: string, geo: Geometry) => {
    e.stopPropagation();
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    dragOrigin.current = { x: e.clientX, y: e.clientY };
    setDrag({ cueId, regionId: null, mode: "move" });
    onSelectCue(cueId);
  };
  const beginRegionDrag = (
    e: React.PointerEvent,
    region: AvoidRegion,
    mode: "move" | "resize"
  ) => {
    e.stopPropagation();
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    dragOrigin.current = {
      x: e.clientX,
      y: e.clientY,
      rect: [...region.rect] as [number, number, number, number],
    };
    setDrag({ cueId: null, regionId: region.id, mode });
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag || !dragOrigin.current) return;
    const dx = (e.clientX - dragOrigin.current.x) / scale;
    const dy = (e.clientY - dragOrigin.current.y) / scale;
    if (drag.cueId) {
      onCueDrag(drag.cueId, dx, dy, false);
    } else if (drag.regionId && dragOrigin.current.rect) {
      const [x, y, w, h] = dragOrigin.current.rect;
      let rect: [number, number, number, number];
      if (drag.mode === "move") {
        rect = [x + dx, y + dy, w, h];
      } else {
        rect = [x, y, Math.max(20, w + dx), Math.max(20, h + dy)];
      }
      onRegionDrag(drag.regionId, rect, false);
    }
  };

  const onPointerUp = (e: React.PointerEvent) => {
    if (!drag) return;
    const dx = dragOrigin.current ? (e.clientX - dragOrigin.current.x) / scale : 0;
    const dy = dragOrigin.current ? (e.clientY - dragOrigin.current.y) / scale : 0;
    if (drag.cueId) onCueDrag(drag.cueId, dx, dy, true);
    else if (drag.regionId && dragOrigin.current?.rect) {
      const [x, y, w, h] = dragOrigin.current.rect;
      const rect: [number, number, number, number] =
        drag.mode === "move"
          ? [x + dx, y + dy, w, h]
          : [x, y, Math.max(20, w + dx), Math.max(20, h + dy)];
      onRegionDrag(drag.regionId, rect, true);
    }
    setDrag(null);
    dragOrigin.current = null;
  };

  const safe = [
    settings.safe_margin,
    settings.safe_margin,
    W - settings.safe_margin * 2,
    H - settings.safe_margin * 2,
  ] as const;

  return (
    <div className="stage-wrap" ref={wrapRef}>
      <div
        className="stage-frame"
        style={{ width: W * scale, height: H * scale }}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <video
          ref={videoRef}
          src={project.videoUrl}
          muted
          playsInline
          preload="auto"
          onLoadedMetadata={(e) => {
            const v = e.currentTarget;
            try {
              v.currentTime = time;
            } catch {
              /* ignore */
            }
          }}
        />
        <div className="overlay">
          {/* safe margin guide */}
          {showSafe && (
            <div
              className="safe-guide"
              style={{
                left: safe[0] * scale,
                top: safe[1] * scale,
                width: safe[2] * scale,
                height: safe[3] * scale,
              }}
              title="安全边距"
            />
          )}

          {/* regions */}
          {showRegions &&
            project.regions.map((r: AvoidRegion) => {
              const active = activeRegions.some((a) => a.id === r.id);
              const [x, y, w, h] = r.rect;
              const isDragging = drag?.regionId === r.id;
              return (
                <div
                  key={r.id}
                  className={`region-box ${r.kind} ${active ? "" : "inactive"}`}
                  style={{ left: x * scale, top: y * scale, width: w * scale, height: h * scale }}
                  title={`${KIND_LABEL[r.kind]} · ${r.label} · ${r.start.toFixed(2)}–${r.end.toFixed(2)}s`}
                >
                  <div
                    style={{ position: "absolute", inset: 0, cursor: "move", pointerEvents: "auto" }}
                    onPointerDown={(e) => beginRegionDrag(e, r, "move")}
                  />
                  <span className="region-tag">
                    {r.label}
                    {!active && r.end > time ? ` · ${r.start.toFixed(1)}s起` : ""}
                  </span>
                  <div
                    style={{
                      position: "absolute",
                      right: -1, bottom: -1, width: 12, height: 12,
                      cursor: "nwse-resize", background: "currentColor",
                      opacity: isDragging ? 0.9 : 0.6, borderRadius: "3px 0 3px 0",
                      pointerEvents: "auto",
                    }}
                    onPointerDown={(e) => beginRegionDrag(e, r, "resize")}
                  />
                </div>
              );
            })}

          {/* caption boxes for visible cues */}
          {showCaption &&
            visible.map(({ cueId, geo, layout }) => {
              const [ix, iy, iw, ih] = geo.ink;
              const isManual = geo.mode === "manual" || dragCueId === cueId;
              const conflicted = geo.conflicts.length > 0 || layout.stale;
              return (
                <div key={cueId}>
                  <div
                    className={`caption-block ${conflicted ? "conflict" : ""} ${isManual ? "manual" : ""}`}
                    style={{
                      left: ix * scale,
                      top: iy * scale,
                      width: iw * scale,
                      height: ih * scale,
                      pointerEvents: "auto",
                      cursor: "grab",
                    }}
                    onPointerDown={(e) => beginCueDrag(e, cueId, geo)}
                    title={`${cueId} · ${Math.round(geo.fontPx)}px · ${isManual ? "手动" : "自动"}`}
                  />
                  {geo.lines.map((ln, i) => (
                    <div
                      key={i}
                      className="caption-text"
                      style={{
                        left: ln.penX * scale,
                        top: (ln.baselineY - 0.88 * geo.fontPx) * scale,
                        fontSize: geo.fontPx * scale,
                        transform: "none",
                      }}
                    >
                      {ln.text}
                    </div>
                  ))}
                </div>
              );
            })}

          {activeUnsolved && !activeUnsolved.partial && (
            <div className="unsolved-badge">当前字幕无解：{activeUnsolved.unsolved?.reason}</div>
          )}
          {activeUnsolved?.partial && (
            <div className="unsolved-badge" style={{ top: 44, background: "rgba(240,178,60,.92)", color: "#1a1408" }}>
              部分时段完全遮挡，按可解时段渲染
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
