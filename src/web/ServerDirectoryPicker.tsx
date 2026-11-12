import { useTranslation } from 'react-i18next';
import React, { useEffect, useState } from 'react';
import { WEB_DIRECTORY_PENDING } from '../shared/deployment';
import { webRequest } from './api';

interface DirectoryPage { path: string; root: string; directories: string[] }
export default function ServerDirectoryPicker({ value, onChange, allowAuto = false }: {
  value: string; onChange: (value: string) => void; allowAuto?: boolean;
}): React.ReactElement {
  const { t } = useTranslation();
  const [browse, setBrowse] = useState(!allowAuto || Boolean(value));
  const [page, setPage] = useState<DirectoryPage | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const load = async (path = ''): Promise<void> => {
    setLoading(true); setError('');
    try { setPage(await webRequest<DirectoryPage>(`workspaces?${new URLSearchParams({ path })}`)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : t('web.directory.failed')); }
    finally { setLoading(false); }
  };
  useEffect(() => { if (browse && !page) void load(value === WEB_DIRECTORY_PENDING ? '' : value); }, [browse]);
  return <div className="web-directory">
    {allowAuto && <select aria-label={t('web.directory.mode')} value={browse ? 'existing' : 'auto'} onChange={(event) => {
      const existing = event.target.value === 'existing';
      setBrowse(existing); onChange(existing ? WEB_DIRECTORY_PENDING : '');
    }}>
      <option value="auto">{t('web.directory.auto')}</option>
      <option value="existing">{t('web.directory.existing')}</option>
    </select>}
    {!browse ? <span className="web-directory-path">{t('web.directory.hint')}</span> : <>
      <span className="web-directory-path">{value && value !== WEB_DIRECTORY_PENDING ? t('web.directory.selected', { path: value }) : t('web.directory.choose')}</span>
      <div className="web-directory-browser" aria-busy={loading}>
        {page && <>
          <span className="web-directory-path">{page.path}</span>
          <button type="button" className="btn btn-primary" disabled={loading} onClick={() => onChange(page.path)}>{t('web.directory.use')}</button>
          {page.path !== page.root && <button type="button" className="btn btn-ghost" disabled={loading}
            onClick={() => { void load(page.path.slice(0, page.path.lastIndexOf('/'))); }}>{t('web.directory.up')}</button>}
          {page.directories.map((name) => <button type="button" className="btn btn-ghost" disabled={loading} key={name}
            onClick={() => { void load(`${page.path}/${name}`); }}>{name}/</button>)}
          {page.directories.length === 0 && <span className="web-directory-path">{t('web.directory.empty')}</span>}
        </>}
        {loading && <span role="status">{t('web.directory.loading')}</span>}
        {error && <><span className="web-error" role="alert">{error}</span><button type="button" className="btn btn-ghost" onClick={() => { void load(); }}>{t('web.retry')}</button></>}
      </div>
    </>}
  </div>;
}
