import type { Candidate, CandidateResult, CueLayout, UnsolvedReport } from "../types";

const KIND_LABEL: Record<string, string> = {
  signer: "人物保护区",
  sign: "手语动作区",
  important: "重要画面",
  custom: "自定义区域",
};

function CostBars({ parts }: { parts: Record<string, number> }) {
  const labels: Record<string, string> = {
    position: "位置",
    horizontal: "水平",
    lines: "行数",
    size: "字号",
    stability: "稳定性",
    stickyBonus: "贴靠奖励",
    blocked: "遮挡",
    sticky: "保持位置",
  };
  const entries = Object.entries(parts).filter(([k]) => k !== "stickyBonus" || parts[k] !== 0);
  return (
    <div className="cost-bars">
      {entries.map(([k, v]) => (
        <span key={k} title={`代价分量 ${k}`}>
          {labels[k] ?? k} {v.toFixed(1)}
        </span>
      ))}
    </div>
  );
}

function CandidateCard({
  cand, index, selected, onSelect, onAdopt,
}: {
  cand: Candidate;
  index: number;
  selected: boolean;
  onSelect: () => void;
  onAdopt: () => void;
}) {
  return (
    <div
      className={`candidate ${cand.valid ? "" : "invalid"} ${selected ? "selected" : ""}`}
      onClick={onSelect}
    >
      <div className="cand-head">
        <strong>候选 {index + 1}</strong>
        {cand.sticky && <span className="tag accent">保持位置</span>}
        {cand.valid ? <span className="tag ok">无冲突</span> : <span className="tag bad">冲突</span>}
        <span className="tag">{cand.sticky ? "当前采用" : `代价 ${cand.cost.toFixed(1)}`}</span>
        <span className="grow" />
        <button
          className="small primary"
          disabled={!cand.valid}
          onClick={(e) => {
            e.stopPropagation();
            onAdopt();
          }}
        >
          采用
        </button>
      </div>
      <div className="tiny muted">
        位置 ({Math.round(cand.x)}, {Math.round(cand.y)}) · {Math.round(cand.w)}×{Math.round(cand.h)}px ·{" "}
        {Math.round(cand.fontPx)}px · {cand.lines.length} 行 · 来源 {cand.source}
      </div>
      <div className="cand-lines">{cand.lines.join("\n")}</div>
      <CostBars parts={cand.costParts} />
      {cand.note && <div className="tiny" style={{ color: "var(--warn)" }}>{cand.note}</div>}
      {cand.conflicts.length > 0 && (
        <div className="tiny" style={{ color: "var(--bad)" }}>
          冲突：{cand.conflicts.map((c) => `${KIND_LABEL[c.kind]}「${c.label}」覆盖 ${(c.coverageOfBox * 100).toFixed(0)}%`).join("；")}
        </div>
      )}
    </div>
  );
}

export function UnsolvedCard({ report, occupancy }: { report: UnsolvedReport; occupancy?: number }) {
  return (
    <div className="card bad stack">
      <div className="row">
        <span className="tag bad">无解</span>
        <strong>{report.reason}</strong>
      </div>
      <div className="tiny">{report.detail}</div>
      {typeof report.occupancy === "number" && (
        <div className="tiny muted">标注区域占用安全区 {(report.occupancy * 100).toFixed(0)}%</div>
      )}
      {report.blockers.length > 0 && (
        <div className="tiny">
          阻挡区域：
          {report.blockers.map((b) => (
            <div key={b.regionId} className="muted">
              · {KIND_LABEL[b.kind]}「{b.label}」{b.timeRange[0].toFixed(2)}–{b.timeRange[1].toFixed(2)}s
            </div>
          ))}
        </div>
      )}
      {report.attempted.length > 0 && (
        <details className="tiny">
          <summary className="muted">尝试记录（{report.attempted.length}）</summary>
          {report.attempted.map((a, i) => (
            <div key={i} className="mono muted">
              {a.fontPx}px / 宽 {a.widthCap}px → {a.result}
            </div>
          ))}
        </details>
      )}
      {!report.annotationPresent && (
        <div className="tiny" style={{ color: "var(--warn)" }}>
          注意：当前时段没有区域标注。系统不会自动识别关键画面，未标注不等于没有遮挡。
        </div>
      )}
    </div>
  );
}

export function CandidatePanel({
  result, selectedIndex, onSelect, onAdopt, layout,
}: {
  result: CandidateResult | null;
  selectedIndex: number;
  onSelect: (i: number) => void;
  onAdopt: (c: Candidate) => void;
  layout: CueLayout | undefined;
}) {
  if (!result) {
    return <div className="muted tiny">选择一个字幕以查看候选位置、代价与冲突解释。</div>;
  }
  if (!result.solved && !result.candidates.length) {
    return result.unsolved ? <UnsolvedCard report={result.unsolved} /> : <div className="muted">无候选。</div>;
  }
  return (
    <div className="stack">
      {result.partial && (
        <div className="card warn">
          <div className="row"><span className="tag warn">部分遮挡</span><strong>该字幕部分时段完全被遮挡</strong></div>
          <div className="tiny muted" style={{ marginTop: 4 }}>
            已按可解时段渲染；被遮挡时段不绘制，不缩小字号。
            {result.unsolved ? ` 原因：${result.unsolved.reason}。` : ""}
          </div>
          <div className="tiny mono" style={{ marginTop: 4 }}>
            {result.segments.map((s) => (
              <div key={s.start}>
                {s.start.toFixed(2)}–{s.end.toFixed(2)}s · {s.solved ? "已布局" : "无解（不渲染）"}
              </div>
            ))}
          </div>
        </div>
      )}
      {result.unsolved && result.solved && !result.partial && (
        <UnsolvedCard report={result.unsolved} />
      )}
      {result.candidates.map((c, i) => (
        <CandidateCard
          key={i}
          cand={c}
          index={i}
          selected={i === selectedIndex}
          onSelect={() => onSelect(i)}
          onAdopt={() => onAdopt(c)}
        />
      ))}
      {layout?.stale && (
        <div className="card bad tiny">已采用的布局与当前标注冲突，导出会被阻止，请重新选择。</div>
      )}
      {result.sticky && (
        <div className="tiny muted">当前采用稳定策略：优先保持上一条/已采用位置，避免轻微区域变化造成跳动。</div>
      )}
    </div>
  );
}

export { KIND_LABEL };
