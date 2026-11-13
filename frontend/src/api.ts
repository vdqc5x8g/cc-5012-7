import type {
  Candidate,
  CandidateResult,
  ExportJob,
  LayoutPayload,
  Project,
  ProjectSummary,
} from "./types";

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.error) msg = body.error;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => req<{ ok: boolean; ffmpeg: string }>("/api/health"),
  listProjects: () => req<{ projects: ProjectSummary[] }>("/api/projects"),
  getProject: (id: string) => req<Project>(`/api/projects/${id}`),
  createSample: () => req<{ ok: boolean }>("/api/projects/sample", { method: "POST" }),
  createProject: (payload: Record<string, unknown>) =>
    req<Project>("/api/projects", { method: "POST", body: JSON.stringify(payload) }),
  updateProject: (id: string, payload: Record<string, unknown>) =>
    req<Project>(`/api/projects/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteProject: (id: string) => req<{ ok: boolean }>(`/api/projects/${id}`, { method: "DELETE" }),
  layout: (id: string) => req<LayoutPayload>(`/api/projects/${id}/layout`),
  candidates: (id: string, cueId: string) =>
    req<CandidateResult>(`/api/projects/${id}/cues/${cueId}/candidates`),
  adopt: (id: string, cueId: string, payload: Record<string, unknown>) =>
    req<Project>(`/api/projects/${id}/cues/${cueId}/adopt`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  adoptCandidate: (id: string, cueId: string, candidate: Candidate) =>
    req<Project>(`/api/projects/${id}/cues/${cueId}/adopt`, {
      method: "POST",
      body: JSON.stringify({
        x: candidate.x,
        y: candidate.y,
        fontPx: candidate.fontPx,
        lines: candidate.lines,
      }),
    }),
  validate: (id: string, cueId: string, payload: Record<string, unknown>) =>
    req<{
      geometry: import("./types").Geometry;
      conflicts: import("./types").Conflict[];
      problems: string[];
      valid: boolean;
    }>(`/api/projects/${id}/cues/${cueId}/validate`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  addRegion: (id: string, payload: Record<string, unknown>) =>
    req<Project>(`/api/projects/${id}/regions`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateRegion: (id: string, regionId: string, payload: Record<string, unknown>) =>
    req<Project>(`/api/projects/${id}/regions/${regionId}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  deleteRegion: (id: string, regionId: string) =>
    req<Project>(`/api/projects/${id}/regions/${regionId}`, { method: "DELETE" }),
  addCue: (id: string, payload: Record<string, unknown>) =>
    req<Project>(`/api/projects/${id}/cues`, { method: "POST", body: JSON.stringify(payload) }),
  deleteCue: (id: string, cueId: string) =>
    req<Project>(`/api/projects/${id}/cues/${cueId}`, { method: "DELETE" }),
  startExport: (id: string) => req<{ jobId: string }>(`/api/projects/${id}/export`, { method: "POST" }),
  job: (jobId: string) => req<ExportJob>(`/api/jobs/${jobId}`),
  revisions: (id: string) =>
    req<{ revisions: import("./types").Revision[] }>(`/api/projects/${id}/revisions`),
};
