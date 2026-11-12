import { useTranslation } from 'react-i18next';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useKnoweStore } from '../store/store';
import { webRequest, logout } from './api';

interface Notice { id: number; project_id: string; project_name: string; kind: string; body: Record<string, string>; created_at: string; read: number; resolved: number }
interface NoticePage { items: Notice[]; unread: number; pending: number; latest_id: number; next_before: number | null }

function title(notice: Notice): string {
  if (notice.kind === 'approval_card') return notice.resolved ? 'web.notifications.approvalEnded' : 'web.notifications.approvalPending';
  if (notice.kind === 'error') return 'web.notifications.error';
  const statuses: Record<string, string> = { SUCCEEDED: 'web.notifications.succeeded', PARTIAL: 'web.notifications.partial', FAILED: 'web.notifications.taskFailed', BLOCKED: 'web.notifications.blocked', CANCELLED: 'web.notifications.cancelled', SYSTEM_ERROR: 'web.notifications.systemError', TIMED_OUT: 'web.notifications.timedOut', ROLLED_BACK: 'web.notifications.rolledBack', SUPERSEDED: 'web.notifications.superseded' };
  return statuses[notice.body.status || ''] || 'web.notifications.ended';
}

export default function NotificationCenter(): React.ReactElement {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<NoticePage | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const bell = useRef<HTMLButtonElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const summaryDue = useRef(true);
  const loading = useRef(false);
  const conn = useKnoweStore((state) => state.conn);

  const refresh = useCallback(async (): Promise<void> => {
    if (loading.current) return;
    loading.current = true;
    try {
      const next = await webRequest<NoticePage>('notifications');
      setPage(next); setError('');
      if (summaryDue.current) {
        summaryDue.current = false;
        if (next.unread) useKnoweStore.getState().addNotice(t('web.notifications.summary', { count: next.unread }) + (next.pending ? t('web.notifications.pendingSuffix', { count: next.pending }) : ''));
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : t('web.notifications.failed')); }
    finally { loading.current = false; }
  }, [t]);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, 15000);
    const visible = (): void => {
      if (document.hidden) summaryDue.current = true;
      else void refresh();
    };
    const changed = (): void => { void refresh(); };
    window.addEventListener('knowe:notifications-changed', changed);
    window.addEventListener('focus', visible);
    document.addEventListener('visibilitychange', visible);
    return () => { clearInterval(timer); window.removeEventListener('knowe:notifications-changed', changed); window.removeEventListener('focus', visible); document.removeEventListener('visibilitychange', visible); };
  }, [refresh]);
  useEffect(() => { if (conn === 'live') void refresh(); }, [conn, refresh]);
  useEffect(() => { if (open) close.current?.focus(); }, [open]);
  const dismiss = (): void => { setOpen(false); bell.current?.focus(); };
  const read = async (payload: { id?: number; through?: number }): Promise<void> => {
    setBusy(true);
    try {
      await webRequest('notifications/read', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : t('web.notifications.readFailed')); }
    finally { setBusy(false); }
  };

  return <>
    <button ref={bell} className="rail-btn web-rail-bell" aria-label={t('web.notifications.label') + (page?.unread ? t('web.notifications.unreadSuffix', { count: page.unread }) : '')}
      aria-expanded={open} aria-controls="web-notifications" data-tip={t('web.notifications.label')} onClick={() => { setOpen(!open); void refresh(); }}>
      <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" /></svg>
      {!!page?.unread && <span className="web-notice-count">{page.unread > 99 ? '99+' : page.unread}</span>}
    </button>
    {open && <aside id="web-notifications" className="web-notifications" aria-label={t('web.notifications.label')} onKeyDown={(event) => { if (event.key === 'Escape') dismiss(); }}>
      <header><h2>{t('web.notifications.label')}</h2><button type="button" className="btn btn-ghost" disabled={busy || !page?.unread}
        onClick={() => { if (page) void read({ through: page.latest_id }); }}>{t('web.notifications.allRead')}</button>
        <button ref={close} type="button" className="btn btn-ghost" onClick={dismiss}>{t('web.close')}</button></header>
      <div className="web-notifications-list" aria-live="polite">
        {error && <div role="alert"><p className="web-error">{error}</p><button className="btn btn-ghost" onClick={() => { void refresh(); }}>{t('web.retry')}</button></div>}
        {!page && !error && <p role="status">{t('web.notifications.loading')}</p>}
        {page?.items.length === 0 && <p className="web-notice-empty">{t('web.notifications.empty')}</p>}
        {page?.items.map((notice) => <article key={notice.id} className="web-notice-row" data-unread={!notice.read}>
          <strong>{t(title(notice))} · {notice.project_name}</strong>
          <p>{notice.kind === 'approval_card' ? (notice.resolved ? t('web.notifications.approvalResult') : t('web.notifications.approvalAction')) : (notice.body.message || notice.body.reason)}</p>
          <time dateTime={notice.created_at}>{new Date(notice.created_at).toLocaleString()}</time>
          <button className="btn btn-ghost" disabled={busy} onClick={() => {
            const store = useKnoweStore.getState();
            if (!store.convs[notice.project_id]) { store.addNotice(t('web.notifications.missingProject')); return; }
            store.setView('chats'); store.switchProject(notice.project_id);
            void read({ id: notice.id }); dismiss();
          }}>{t('web.notifications.conversation')}</button>
        </article>)}
        {!!page?.next_before && <button className="btn btn-ghost" disabled={busy} onClick={() => {
          setBusy(true);
          void webRequest<NoticePage>(`notifications?before=${page.next_before}`)
            .then((older) => setPage({ ...older, items: [...page.items, ...older.items] }))
            .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : t('web.notifications.loadFailed')))
            .finally(() => setBusy(false));
        }}>{t('web.notifications.more')}</button>}
      </div>
      <footer><button className="btn btn-ghost" disabled={busy} onClick={() => { void refresh(); }}>{t('web.refresh')}</button>
        <button className="btn btn-ghost" onClick={() => { void logout().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : t('web.logout.failed'))); }}>{t('web.logout')}</button></footer>
    </aside>}
  </>;
}
