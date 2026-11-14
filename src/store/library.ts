import { runtimeFetch } from '../shared/runtimeFetch';
import { runtimeHttpBase } from '../shared/runtimeEndpoints';

export interface LibraryFile {
  id: string; name: string; ext: string; origin: string; managed: boolean; size: number;
  mtime: number; version: string; status: string; favorite: boolean;
  references: { project_id: string; project_name: string; seq: number; agent_id: string }[];
}
export interface LibraryPage {
  items: LibraryFile[]; total: number; next_offset: number | null;
  storage: { active_bytes: number; trash_bytes: number; active_count: number };
  projects: { project_id: string; name: string }[];
}
export async function libraryRequest<T>(path = '', body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await runtimeFetch(`${runtimeHttpBase()}/api/library${path}`, {
    method: body === undefined ? 'GET' : 'POST', signal,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json() as T & { ok?: boolean; error?: string };
  if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), 3);
  return `${(bytes / 1024 ** exponent).toFixed(1)} ${['B', 'KB', 'MB', 'GB'][exponent]}`;
}
