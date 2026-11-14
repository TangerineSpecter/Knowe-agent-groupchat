import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { fileSize, libraryRequest, type LibraryFile, type LibraryPage } from '../store/library';
import { openPreviewWindow, revealFileInFolder } from '../store/filePreview';
import { isWebRuntime } from '../shared/deployment';
import './library.css';

export default function LibraryView({ onOpenReference }: { onOpenReference: (projectId: string, seq: number) => void }) {
  const { t } = useTranslation();
  const [page, setPage] = useState<LibraryPage | null>(null);
  const [query, setQuery] = useState('');
  const [project, setProject] = useState('');
  const [origin, setOrigin] = useState('');
  const [tab, setTab] = useState('all');
  const [sort, setSort] = useState('newest');
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pending, setPending] = useState<{ action: string; files: LibraryFile[] } | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const request = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true); setError('');
    try {
      const params = new URLSearchParams({ query, project, origin, sort, offset: String(offset),
        status: tab === 'trash' ? 'trash' : 'active', favorite: tab === 'favorites' ? '1' : '' });
      const result = await libraryRequest<LibraryPage>(`?${params}`, undefined, controller.signal);
      setPage(result); setSelected(new Set());
    } catch (e) {
      if (!controller.signal.aborted) setError(String(e instanceof Error ? e.message : e));
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }, [query, project, origin, sort, offset, tab]);
  useEffect(() => { const timer = window.setTimeout(() => { void refresh(); }, 180);
    return () => { window.clearTimeout(timer); request.current?.abort(); }; }, [refresh]);
  const act = async (action: string, files: LibraryFile[], confirm = false) => {
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await libraryRequest<{ succeeded: unknown[]; failed: { id: string; error: string }[] }>('/action', {
        action, files: files.map(({ id, version }) => ({ id, version })), confirm_references: confirm,
      });
      setPending(null);
      await refresh();
      if (result.failed.length) setError(result.failed.map(f => `${files.find(item => item.id === f.id)?.name}: ${f.error}`).join('\n'));
      if (result.succeeded.length) setNotice(t('library.done', { n: result.succeeded.length }));
    } catch (e) { setError(String(e instanceof Error ? e.message : e)); }
    finally { setBusy(false); }
  };
  const filePayload = (file: LibraryFile) => ({ path: `${file.id}/${file.name}`, name: file.name, ext: file.ext, bytes: file.size });
  const preview = async (file: LibraryFile, reveal = false) => {
    setError('');
    try { if (reveal) await revealFileInFolder('__library__', filePayload(file));
      else await openPreviewWindow(filePayload(file), '__library__'); }
    catch (e) { setError(String(e instanceof Error ? e.message : e)); }
  };
  const choose = (id: string) => setSelected(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const selectedFiles = page?.items.filter(file => selected.has(file.id)) || [];
  const filter = (change: () => void) => { change(); setOffset(0); setSelected(new Set()); setPending(null); };
  return <div className="stage library-stage"><div className="stage-card"><section className="library-view" aria-label={t('library.title')}>
    <header className="library-header">
      <div><h1>{t('library.title')}</h1><p>{t('library.subtitle')}</p></div>
      <button type="button" className="btn" disabled={loading || busy} onClick={() => void refresh()}>{t('library.refresh')}</button>
    </header>
    <div className="library-storage"><strong>{fileSize((page?.storage.active_bytes || 0) + (page?.storage.trash_bytes || 0))}</strong>
      <span>{t('library.used')}</span><span>{t('library.trashSize', { size: fileSize(page?.storage.trash_bytes || 0) })}</span>
      <p>{t('library.storageHelp')}</p>
    </div>
    <nav className="library-tabs" aria-label={t('library.views')}>
      {['all', 'favorites', 'trash'].map(key => <button type="button" key={key} aria-pressed={tab === key} onClick={() => filter(() => setTab(key))}>{t(`library.${key === 'trash' ? 'trashTab' : key}`)}</button>)}
    </nav>
    <div className="library-filters">
      <input type="search" aria-label={t('library.search')} placeholder={t('library.search')} value={query} onChange={e => filter(() => setQuery(e.target.value))} />
      <select aria-label={t('library.project')} value={project} onChange={e => filter(() => setProject(e.target.value))}>
        <option value="">{t('library.allProjects')}</option>{page?.projects.map(p => <option key={p.project_id} value={p.project_id}>{p.name}</option>)}
      </select>
      <select aria-label={t('library.source')} value={origin} onChange={e => filter(() => setOrigin(e.target.value))}>
        {['', 'upload', 'generated', 'external', 'unreferenced'].map(key => <option key={key} value={key}>{t(`library.origin.${key || 'all'}`)}</option>)}
      </select>
      <select aria-label={t('library.sort')} value={sort} onChange={e => filter(() => setSort(e.target.value))}>
        {['newest', 'largest', 'name'].map(key => <option key={key} value={key}>{t(`library.sort.${key}`)}</option>)}
      </select>
    </div>
    {error && <p role="alert" className="library-error">{error}</p>}
    {notice && <p role="status" className="library-notice">{notice}</p>}
    {pending && <div className="library-confirm" role="alertdialog" aria-label={t(`library.${pending.action}`)}>
      <strong>{t('library.confirmTitle', { action: t(`library.${pending.action}`), n: pending.files.length })}</strong>
      <p>{t(pending.action === 'purge' ? 'library.purgeHelp' : 'library.trashHelp')}</p>
      {pending.files.some(file => file.references.length) && <p>{t('library.referenceWarning')}</p>}
      {pending.files.some(file => !file.managed) && <p>{t('library.externalHelp')}</p>}
      <div><button type="button" className="btn" disabled={busy} onClick={() => setPending(null)}>{t('library.cancel')}</button>
        <button type="button" className="btn library-danger" disabled={busy} onClick={() => void act(pending.action, pending.files, true)}>{t(`library.${pending.action}`)}</button></div>
    </div>}
    <div className="library-selection">
      <label><input type="checkbox" disabled={loading || !page?.items.length} checked={!!page?.items.length && selected.size === page.items.length}
        onChange={e => setSelected(new Set(e.target.checked ? page?.items.map(file => file.id) : []))} />{t('library.selectPage')}</label>
      <span>{selected.size ? t('library.selected', { n: selected.size }) : t('library.count', { n: page?.total || 0 })}</span>
      {selected.size > 0 && <><button type="button" className="btn" disabled={busy || loading} onClick={() => setPending({ action: tab === 'trash' ? 'purge' : 'trash', files: selectedFiles })}>{t(tab === 'trash' ? 'library.purge' : 'library.trash')}</button>
        {tab === 'trash' && <button type="button" className="btn" disabled={busy || loading} onClick={() => void act('restore', selectedFiles)}>{t('library.restore')}</button>}</>}
    </div>
    <div className="library-files" aria-busy={loading}>
      {loading ? <p className="library-empty" role="status">{t('library.loading')}</p> : !page?.items.length ? <div className="library-empty"><h2>{t(tab === 'trash' ? 'library.emptyTrash' : 'library.empty')}</h2><p>{t(query || project || origin ? 'library.noMatch' : 'library.emptyHelp')}</p></div> : page.items.map(file =>
        <article className="library-file" key={file.id}>
          <input type="checkbox" checked={selected.has(file.id)} onChange={() => choose(file.id)} aria-label={t('library.selectFile', { name: file.name })} />
          <span className="library-filetype" aria-hidden="true">{file.ext.slice(0, 5).toUpperCase() || 'FILE'}</span>
          <div className="library-filebody"><button type="button" className="library-filename" disabled={file.status !== 'active'} onClick={() => void preview(file)}>{file.name}</button>
            <div className="library-filemeta"><span>{fileSize(file.size)}</span><span>{t(`library.origin.${file.origin}`)}</span>
              <time dateTime={new Date(file.mtime * 1000).toISOString()}>{new Date(file.mtime * 1000).toLocaleDateString()}</time>
              {file.status === 'missing' && <span>{t('library.missing')}</span>}</div>
            {file.references.length > 0 && <details className="library-references"><summary>{t('library.references', { n: file.references.length })}</summary>
              {file.references.map((ref, index) => <button type="button" key={index} onClick={() => {
                onOpenReference(ref.project_id, ref.seq);
              }}>{ref.project_name || ref.project_id}{ref.agent_id ? ` · ${ref.agent_id}` : ''}</button>)}
            </details>}
          </div>
          <div className="library-fileactions">
            {tab === 'trash' ? <><button type="button" disabled={busy} onClick={() => void act('restore', [file])}>{t('library.restore')}</button><button type="button" disabled={busy} onClick={() => setPending({ action: 'purge', files: [file] })}>{t('library.purge')}</button></> : <>
              <button type="button" disabled={busy} aria-pressed={!!file.favorite} onClick={() => void act('favorite', [file])}>{t(file.favorite ? 'library.unfavorite' : 'library.favorite')}</button>
              <button type="button" disabled={file.status !== 'active'} onClick={() => void preview(file, true)}>{t(isWebRuntime() ? 'library.download' : 'library.reveal')}</button>
              <button type="button" disabled={busy} onClick={() => setPending({ action: 'trash', files: [file] })}>{t('library.trash')}</button>
            </>}
          </div>
        </article>)}
    </div>
    <footer className="library-pagination"><button type="button" className="btn" disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - 60))}>{t('library.previous')}</button>
      <span>{t('library.page', { n: Math.floor(offset / 60) + 1 })}</span><button type="button" className="btn" disabled={loading || page?.next_offset == null} onClick={() => setOffset(page?.next_offset || 0)}>{t('library.next')}</button></footer>
  </section></div></div>;
}
