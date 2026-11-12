import i18n from '../i18n';
import type { AttachmentPick } from '../shared/bridge';

export async function webRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/web/${path}`, { ...init, credentials: 'same-origin' });
  if (response.status === 401 && path !== 'login') window.dispatchEvent(new Event('knowe:web-auth-required'));
  const body = await response.json().catch(() => { throw new Error(i18n.t('web.api.invalid')); }) as T & { error?: string };
  if (!response.ok) {
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return body;
}

export async function uploadFiles(files: File[]): Promise<AttachmentPick[]> {
  if (!files.length || files.length > 20) throw new Error(i18n.t('web.upload.count'));
  if (files.reduce((size, file) => size + file.size, 0) > 64 * 1024 * 1024) {
    throw new Error(i18n.t('web.upload.size'));
  }
  const form = new FormData();
  for (const file of files) form.append('files', file);
  const result = await webRequest<{ files: AttachmentPick[] }>('uploads', { method: 'POST', body: form });
  return result.files;
}

export async function logout(): Promise<void> {
  await webRequest('logout', { method: 'POST' });
  if (typeof BroadcastChannel !== 'undefined') {
    const channel = new BroadcastChannel('knowe-web-auth');
    channel.postMessage('logout');
    channel.close();
  }
  window.location.reload();
}
