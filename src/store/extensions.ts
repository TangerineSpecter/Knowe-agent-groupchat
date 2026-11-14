import { runtimeHttpBase } from '../shared/runtimeEndpoints';
import { runtimeFetch } from '../shared/runtimeFetch';
import { PLATFORM_PROJECT_ID } from './avatar';

export async function extensionRequest<T>(path: string, payload?: unknown): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 120_000);
  try {
    const response = await runtimeFetch(`${runtimeHttpBase()}/api/extensions/${path}`, {
      method: payload === undefined ? 'GET' : 'POST',
      headers: payload === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: payload === undefined ? undefined : JSON.stringify(payload),
      signal: controller.signal,
    });
    const result = await response.json() as T & { ok?: boolean; error?: string };
    if (!response.ok || result.ok === false) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  } finally {
    window.clearTimeout(timer);
  }
}

export interface SkillCandidate {
  name: string;
  description: string;
  path: string;
  exists: boolean;
}

export interface SkillInstallPreview {
  token: string;
  source: string;
  skills: SkillCandidate[];
  invalid: { path: string; error: string }[];
}

export interface CallableSkill {
  pack_id: string;
  name: string;
  description: string;
  status: string;
  project_id?: string;
  user_invocable?: boolean;
}

export async function callableSkills(projectId: string): Promise<CallableSkill[]> {
  if (projectId === PLATFORM_PROJECT_ID) return [];
  const groupId = projectId.startsWith('dm:') ? (projectId.split(':')[1] || projectId) : projectId;
  const response = await runtimeFetch(`${runtimeHttpBase()}/api/knowledge/skillpacks?project=${encodeURIComponent(groupId)}`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const result = await response.json() as Record<string, CallableSkill[]>;
  return ['system_builtin', 'project_experience', 'third_party'].flatMap(key => result[key] || [])
    .filter(row => row.status === 'active' && row.user_invocable !== false);
}
