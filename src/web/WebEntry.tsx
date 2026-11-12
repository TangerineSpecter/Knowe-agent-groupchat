import { useTranslation } from 'react-i18next';
import React, { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { useSettingsStore } from '../store/settings';
import { useKnoweStore } from '../store/store';
import { webRequest } from './api';
import type { PreviewOpenPayload } from '../shared/bridge';
import './web.css';

const WebPreview = lazy(async () => {
  await Promise.all([
    import('../styles/knowe-preview.css'), import('../styles/knowe-preview-window.css'),
    import('../styles/knowe-v0452-code-preview.css'),
  ]);
  return import('../preview/PreviewApp');
});

function previewPayload(): PreviewOpenPayload | undefined {
  const raw = new URLSearchParams(window.location.hash.slice(1)).get('preview');
  if (!raw) return undefined;
  try {
    const value = JSON.parse(raw) as PreviewOpenPayload;
    if (typeof value.projectId === 'string' && typeof value.sourceKey === 'string'
      && typeof value.file?.path === 'string' && typeof value.file.name === 'string') return value;
  } catch { /* Invalid links do not prevent opening the main application. */ }
  return undefined;
}

export default function WebEntry({ children }: { children: React.ReactNode }): React.ReactElement {
  const { t, i18n } = useTranslation();
  const language = useSettingsStore((state) => state.language);
  const [phase, setPhase] = useState<'loading' | 'login' | 'ready' | 'error'>('loading');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [preview] = useState(previewPayload);
  const bootstrapGeneration = useRef(0);
  useEffect(() => { void i18n.changeLanguage(language); }, [i18n, language]);
  const bootstrap = async (): Promise<void> => {
    const generation = ++bootstrapGeneration.current;
    setPhase('loading');
    setError('');
    try {
      const session = await webRequest<{ authenticated: boolean; instance_id: string }>('session');
      if (generation !== bootstrapGeneration.current) return;
      if (!session.authenticated) { setPhase('login'); return; }
      // A replacement server at the same origin must not inherit another instance's caches.
      const oldInstance = localStorage.getItem('knowe.web.instance');
      if (oldInstance && oldInstance !== session.instance_id) {
        for (const key of Object.keys(localStorage)) if (key.startsWith('knowe')) localStorage.removeItem(key);
        localStorage.setItem('knowe.web.instance', session.instance_id);
        window.location.reload();
        return;
      }
      localStorage.setItem('knowe.web.instance', session.instance_id);
      const result = await useSettingsStore.getState().reconcileFromBackend();
      if (generation !== bootstrapGeneration.current) return;
      if (!result.ok) throw new Error(result.message);
      setPhase('ready');
    } catch (reason) {
      if (generation !== bootstrapGeneration.current) return;
      setError(reason instanceof Error ? reason.message : t('web.connect.failed'));
      setPhase('error');
    }
  };

  useEffect(() => {
    document.documentElement.classList.add('web-runtime');
    void bootstrap();
    const expired = (): void => {
      bootstrapGeneration.current += 1;
      useKnoweStore.getState()._socket?.disconnect();
      setPassword('');
      setError(t('web.login.expired'));
      setPhase('login');
    };
    window.addEventListener('knowe:web-auth-required', expired);
    const channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('knowe-web-auth') : null;
    if (channel) channel.onmessage = (event) => { if (event.data === 'logout') expired(); };
    return () => {
      bootstrapGeneration.current += 1;
      window.removeEventListener('knowe:web-auth-required', expired);
      channel?.close();
    };
  }, []);

  if (phase === 'ready') return preview
    ? <Suspense fallback={<div className="web-loading" role="status">{t('web.preview.loading')}</div>}><WebPreview initialPayload={preview} /></Suspense>
    : <>{children}</>;

  return (
    <main className="web-entry">
      <form className="web-entry-form" onSubmit={(event) => {
        event.preventDefault();
        if (phase !== 'login' || busy) return;
        setBusy(true); setError('');
        void webRequest('login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }) })
          .then(() => window.location.reload())
          .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : t('web.login.failed')))
          .finally(() => setBusy(false));
      }}>
        <h1>Knowe 知知智能体</h1>
        {phase === 'loading' ? <p role="status">{t('web.login.loading')}</p> : phase === 'error' ? <>
          <p role="alert">{error}</p>
          <button className="btn btn-primary" type="button" onClick={() => { void bootstrap(); }}>{t('web.login.retry')}</button>
        </> : <>
          <p>{t('web.login.hint')}</p>
          <label htmlFor="web-password">{t('web.login.password')}</label>
          <input id="web-password" type="password" autoComplete="current-password" autoFocus required
            value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy} />
          {error && <p className="web-error" role="alert">{error}</p>}
          <button className="btn btn-primary" type="submit" disabled={busy}>{busy ? t('web.login.busy') : t('web.login.enter')}</button>
        </>}
      </form>
    </main>
  );
}
