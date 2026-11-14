import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { extensionRequest, type SkillInstallPreview } from '../store/extensions';
import './extensions.css';

export function SkillInstall({ onInstalled, onClose }: { onInstalled: () => void; onClose: () => void }): React.ReactElement {
  const { t } = useTranslation();
  const [url, setUrl] = useState('');
  const [directory, setDirectory] = useState('');
  const [preview, setPreview] = useState<SkillInstallPreview | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [replace, setReplace] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
    void extensionRequest<{ manual_directory: string }>('skills/settings')
      .then(result => setDirectory(result.manual_directory)).catch(() => undefined);
  }, []);

  const prepare = async (payload: unknown): Promise<void> => {
    setBusy(true); setError(''); setPreview(null); setReplace(false);
    try {
      const result = await extensionRequest<SkillInstallPreview>('skills/prepare', payload);
      setPreview(result);
      setSelected(result.skills.filter(row => !row.exists).map(row => row.path));
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const importZip = async (file: File): Promise<void> => {
    if (file.size > 32 * 1024 * 1024) { setError(t('extensions.zipLimit')); return; }
    setError('');
    try {
      const encoded = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
        reader.onerror = () => reject(new Error(t('extensions.fileReadFailed')));
        reader.readAsDataURL(file);
      });
      await prepare({ archive_base64: encoded });
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };
  const install = async (): Promise<void> => {
    if (!preview) return;
    setBusy(true); setError('');
    try {
      await extensionRequest('skills/install', { token: preview.token, selected, replace });
      onInstalled();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const conflicts = preview?.skills.some(row => row.exists && selected.includes(row.path));
  return <section className="extension-install" aria-labelledby="skill-install-title" aria-busy={busy}>
    <div className="extension-heading"><h3 id="skill-install-title">{t('extensions.installSkills')}</h3>
      <button type="button" className="kn-btn" disabled={busy} onClick={onClose}>{t('extensions.close')}</button></div>
    <p>{t('extensions.installHint')}</p>
    <form className="extension-source" onSubmit={event => { event.preventDefault(); void prepare({ url }); }}>
      <label htmlFor="skill-source">{t('extensions.source')}</label>
      <div className="extension-actions"><input id="skill-source" ref={inputRef} value={url} disabled={busy}
        placeholder="https://github.com/owner/repo" onChange={event => setUrl(event.target.value)} />
        <button type="submit" className="kn-btn ok" disabled={busy || !url.trim()}>{t(busy ? 'extensions.processing' : 'extensions.preview')}</button>
        <button type="button" className="kn-btn" disabled={busy} onClick={() => fileRef.current?.click()}>{t('extensions.importZip')}</button></div>
      <input type="file" ref={fileRef} hidden accept=".zip,application/zip" onChange={event => {
        const file = event.target.files?.[0]; event.target.value = ''; if (file) void importZip(file);
      }} />
    </form>
    {error && <p role="alert" className="extension-error">{error}</p>}
    {busy && <p role="status">{t('extensions.processingHint')}</p>}
    {preview && <div className="extension-candidates">
      <h4>{t('extensions.chooseSkills', { count: preview.skills.length })}</h4>
      {preview.skills.map(row => <label key={row.path} className="extension-candidate">
        <input type="checkbox" disabled={busy} checked={selected.includes(row.path)} onChange={event =>
          setSelected(current => event.target.checked ? [...current, row.path] : current.filter(path => path !== row.path))} />
        <span><strong>{row.name}</strong>{row.exists && <span className="chip info">{t('extensions.installed')}</span>}
          <span className="extension-description">{row.description}</span><small>{row.path}</small></span>
      </label>)}
      {preview.invalid.map(row => <p className="extension-error" key={row.path}>{row.path}: {row.error}</p>)}
      {conflicts && <label className="extension-replace"><input type="checkbox" disabled={busy} checked={replace}
        onChange={event => setReplace(event.target.checked)} />{t('extensions.replace')}</label>}
      <button type="button" className="kn-btn ok" disabled={busy || !selected.length || (!!conflicts && !replace)} onClick={() => void install()}>
        {t('extensions.installSelected', { count: selected.length })}</button>
    </div>}
    <details><summary>{t('extensions.manualInstall')}</summary><p>{t('extensions.manualHint')}</p><code>{directory}</code></details>
  </section>;
}
