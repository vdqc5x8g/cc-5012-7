export type Rect = [number, number, number, number]; // x, y, w, h

export interface VideoInfo {
  path: string;
  width: number;
  height: number;
  fps: number;
  duration: number;
  hasAudio?: boolean;
}

export interface CaptionCue {
  id: string;
  start: number;
  end: number;
  text: string;
}

export type RegionKind = "signer" | "sign" | "important" | "custom";

export interface AvoidRegion {
  id: string;
  label: string;
  kind: RegionKind;
  start: number;
  end: number;
  rect: Rect;
  source: string;
  note: string;
}

export interface LayoutSettings {
  font_size: number;
  bold: boolean;
  max_lines: number;
  max_chars_per_line: number;
  safe_margin: number;
  outline: number;
  line_spacing: number;
  sticky_tolerance_px: number;
  min_font_ratio: number;
  grid_step: number;
  verify_threshold: number;
}

export interface Conflict {
  regionId: string;
  label: string;
  kind: RegionKind;
  rect: Rect;
  timeRange: [number, number];
  overlapArea: number;
  coverageOfBox: number;
  coverageOfRegion: number;
}

export interface LineBox {
  text: string;
  penX: number;
  baselineY: number;
  ink: Rect;
}

export interface Geometry {
  cueId: string;
  solved: boolean;
  ink: Rect;
  padded: Rect;
  fontPx: number;
  align: string;
  lines: LineBox[];
  conflicts: Conflict[];
  mode: string;
}

export interface Candidate {
  x: number;
  y: number;
  w: number;
  h: number;
  fontPx: number;
  lines: string[];
  align: string;
  valid: boolean;
  cost: number;
  costParts: Record<string, number>;
  conflicts: Conflict[];
  source: string;
  sticky: boolean;
  note: string;
}

export interface UnsolvedReport {
  reason: string;
  detail: string;
  blockers: {
    regionId: string;
    label: string;
    kind: RegionKind;
    rect: Rect;
    timeRange: [number, number];
  }[];
  occupancy: number;
  attempted: { fontPx: number; widthCap: number; result: string }[];
  annotationPresent: boolean;
  conflicts?: Conflict[];
}

export interface LayoutSegment {
  start: number;
  end: number;
  solved: boolean;
  geometry: Geometry | null;
  unsolved: UnsolvedReport | null;
}

export interface CueLayout {
  cueId: string;
  solved: boolean;
  partial: boolean;
  stale: boolean;
  geometry: Geometry | null;
  segments: LayoutSegment[] | null;
  unsolved: UnsolvedReport | null;
}

export interface LayoutPayload {
  perCue: Record<string, CueLayout>;
  unsolvedCueIds: string[];
  problems: { cueId: string; problems: string[]; conflicts: Conflict[] }[];
  annotationNotice: string;
  settings: LayoutSettings;
  frame: { width: number; height: number; fps: number; duration: number };
}

export interface Revision {
  n: number;
  kind: string;
  at: number;
  actor: string;
  summary: string;
  payload: Record<string, unknown>;
}

export interface Project {
  id: string;
  name: string;
  video: VideoInfo;
  cues: CaptionCue[];
  regions: AvoidRegion[];
  settings: LayoutSettings;
  font_path?: string;
  placements: Record<string, unknown>;
  layout: LayoutPayload;
  videoUrl: string;
  revisionsSummary: Revision[];
  revCounter: number;
}

export interface CandidateResult {
  cueId: string;
  solved: boolean;
  chosen: Candidate | null;
  candidates: Candidate[];
  geometry: Geometry | null;
  unsolved: UnsolvedReport | null;
  activeRegions: AvoidRegion[];
  segments: LayoutSegment[];
  partial: boolean;
  sticky: boolean;
}

export interface RegionHit {
  regionId: string;
  label: string;
  bbox: Rect | null;
  framesHit: number;
  firstTime: number | null;
}

export interface CueCheck {
  cueId: string;
  solved: boolean;
  renderedPixels: boolean;
  bbox: Rect | null;
  outsideSafe: boolean;
  regionHits: RegionHit[];
  ok: boolean;
  note: string;
}

export interface Verification {
  ok: boolean;
  threshold: number;
  safeRect: Rect;
  cues: Record<string, CueCheck>;
  summary: {
    cues: number;
    withPixels: number;
    outsideSafe: number;
    regionHits: number;
    failed: string[];
  };
  method: string;
}

export interface ExportJob {
  state: "pending" | "running" | "done" | "error";
  progress: number;
  message: string;
  result: {
    output: string;
    assPath: string;
    skippedCues: string[];
    renderedCues: number;
    drawnSegments: [string, number, number][];
  } | null;
  verification: Verification | null;
}

export interface ProjectSummary {
  id: string;
  name: string;
  video: VideoInfo;
  cueCount: number;
  regionCount: number;
  revCounter: number;
  unsolvedCount: number;
}
