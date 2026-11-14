import React, { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { extensionRequest } from '../store/extensions';
import './extensions.css';

interface McpServer {
  id: string; name: string; transport: 'http' | 'sse' | 'stdio'; enabled: boolean; kind: string;
  url?: string; command?: string; args?: string[]; cwd?: string; timeout: number;
  header_names: string[]; env_names: string[]; allowed_tools: string[] | null;
  status: string; error: string; tool_count: number;
}
interface McpTool { name: string; description?: string; inputSchema: Record<string, unknown> }
interface FormState { id?: string; name: string; transport: 'http' | 'sse' | 'stdio'; url: string; command: string; args: string; cwd: string; secrets: string; timeout: number }
const emptyForm = (): FormState => ({ name: '', transport: 'http', url: '', command: '', args: '[]', cwd: '', secrets: '', timeout: 60 });

export function McpPanel({ scope, query = '' }: { scope: 'all' | 'system' | 'third_party'; query?: string }): React.ReactElement {
  const { t } = useTranslation();
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [form, setForm] = useState<FormState | null>(null);
  const [importing, setImporting] = useState(false);
  const [importText, setImportText] = useState('');
  const [tools, setTools] = useState<Record<string, McpTool[]>>({});
  const [confirmDelete, setConfirmDelete] = useState('');
  const refresh = useCallback(async (): Promise<void> => {
    const result = await extensionRequest<{ servers: McpServer[] }>('mcp');
    setServers(result.servers);
  }, []);
  useEffect(() => {
    void refresh().catch(e => setError(String(e))).finally(() => setLoading(false));
  }, [refresh]);
  useEffect(() => { setForm(null); setImporting(false); setConfirmDelete(''); }, [scope]);
  const act = async (operation: () => Promise<void>): Promise<void> => {
    setBusy(true); setError(''); setNotice('');
    try { await operation(); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const edit = (row: McpServer): void => {
    setImporting(false);
    setForm({ id: row.id, name: row.name, transport: row.transport, url: row.url || '', command: row.command || '',
      args: JSON.stringify(row.args || []), cwd: row.cwd || '', secrets: '', timeout: row.timeout });
  };
  const save = async (): Promise<void> => {
    if (!form) return;
    const { secrets, args, ...rest } = form;
    const payload = { ...rest, args: JSON.parse(args),
      ...(secrets.trim() ? { [form.transport === 'stdio' ? 'env' : 'headers']: JSON.parse(secrets) } : {}) };
    await extensionRequest('mcp/save', payload);
    setForm(null); setNotice(t('mcp.saved'));
  };
  const shown = servers.filter(row => (scope === 'all' || row.kind === scope)
    && `${row.name} ${row.url || row.command || ''}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const set = <K extends keyof FormState>(key: K, value: FormState[K]): void => setForm(current => current && ({ ...current, [key]: value }));
  return <section className="mcp-panel" aria-label={t('mcp.connections')} aria-busy={busy}>
    <div className="extension-heading">
      <p>{t('mcp.intro')}</p>
      <div className="extension-actions">
        <button type="button" className="kn-btn" disabled={busy} onClick={() => void act(async () => {})}>{t('mcp.refresh')}</button>
        {scope !== 'system' && <><button type="button" className="kn-btn" disabled={busy} onClick={() => { setImporting(value => !value); setForm(null); }}>{t('mcp.import')}</button>
          <button type="button" className="kn-btn ok" disabled={busy} onClick={() => { setForm(emptyForm()); setImporting(false); }}>{t('mcp.add')}</button></>}
      </div>
    </div>
    {error && <p className="extension-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {busy && <p role="status">{t('mcp.working')}</p>}
    {form && <form className="mcp-form" onSubmit={event => { event.preventDefault(); void act(save); }}>
      <h3>{t(form.id ? 'mcp.edit' : 'mcp.add')}</h3>
      <label>{t('mcp.name')}<input required autoFocus value={form.name} maxLength={80} onChange={event => set('name', event.target.value)} /></label>
      <label>{t('mcp.transport')}<select aria-label={t('mcp.transport')} value={form.transport} onChange={event => { set('transport', event.target.value as FormState['transport']); set('secrets', ''); }}>
        <option value="http">Streamable HTTP</option><option value="sse">SSE</option><option value="stdio">{t('mcp.stdio')}</option>
      </select></label>
      {form.transport === 'stdio' ? <>
        <p>{t('mcp.localHint')}</p>
        <label>{t('mcp.command')}<input required value={form.command} placeholder="npx" onChange={event => set('command', event.target.value)} /></label>
        <label>{t('mcp.args')}<textarea aria-label={t('mcp.args')} value={form.args} placeholder='["-y", "@example/mcp-server"]' onChange={event => set('args', event.target.value)} /></label>
        <label>{t('mcp.cwd')}<input value={form.cwd} onChange={event => set('cwd', event.target.value)} /></label>
      </> : <label>{t('mcp.url')}<input required type="url" value={form.url} placeholder="https://example.com/mcp" onChange={event => set('url', event.target.value)} /></label>}
      <label>{t(form.transport === 'stdio' ? 'mcp.env' : 'mcp.headers')}
        <textarea aria-label={t(form.transport === 'stdio' ? 'mcp.env' : 'mcp.headers')} autoComplete="off" spellCheck={false} value={form.secrets} placeholder={form.transport === 'stdio' ? '{"API_KEY": "…"}' : '{"Authorization": "Bearer …"}'} onChange={event => set('secrets', event.target.value)} /></label>
      <p>{t('mcp.secretHint')}</p>
      <label>{t('mcp.timeout')}<input type="number" min={5} max={300} value={form.timeout} onChange={event => set('timeout', Number(event.target.value))} /></label>
      <div className="extension-actions"><button className="kn-btn ok" disabled={busy} type="submit">{t('mcp.save')}</button>
        <button className="kn-btn" disabled={busy} type="button" onClick={() => setForm(null)}>{t('mcp.cancel')}</button></div>
    </form>}
    {importing && <form className="mcp-form" onSubmit={event => { event.preventDefault(); void act(async () => {
      await extensionRequest('mcp/import', JSON.parse(importText)); setImporting(false); setImportText(''); setNotice(t('mcp.imported'));
    }); }}>
      <label>{t('mcp.importHint')}<textarea aria-label={t('mcp.importHint')} rows={8} value={importText} autoComplete="off" spellCheck={false}
        placeholder={'{"mcpServers":{"example":{"url":"https://example.com/mcp"}}}'} onChange={event => setImportText(event.target.value)} /></label>
      <div className="extension-actions"><button type="submit" className="kn-btn ok" disabled={busy || !importText.trim()}>{t('mcp.import')}</button>
        <button type="button" className="kn-btn" disabled={busy} onClick={() => setImporting(false)}>{t('mcp.cancel')}</button></div>
    </form>}
    {loading ? <p role="status">{t('mcp.loading')}</p> : shown.length === 0 ? <div className="mcp-empty">
      <h3>{t(scope === 'system' ? 'mcp.noSystem' : 'mcp.noConnections')}</h3><p>{t(scope === 'system' ? 'mcp.noSystemHint' : 'mcp.emptyHint')}</p>
    </div> : <div className="mcp-list">{shown.map(row => <article className="mcp-row" key={row.id}>
      <div className="extension-heading"><div><h3>{row.name}</h3><p className="mcp-address">{row.transport === 'stdio' ? `${row.command} ${(row.args || []).join(' ')}` : row.url}</p></div>
        <span className={`chip ${row.status === 'connected' ? 'ok' : 'info'}`}>{t(`mcp.status.${row.status}`)}</span></div>
      {row.error && <p className="extension-error">{row.error}</p>}
      <div className="extension-actions">
        <button type="button" className="kn-btn" disabled={busy} onClick={() => void act(async () => {
          const result = await extensionRequest<{ tools: McpTool[] }>('mcp/test', { id: row.id });
          setTools(current => ({ ...current, [row.id]: result.tools })); setNotice(t('mcp.tested', { n: result.tools.length }));
        })}>{t('mcp.test')}</button>
        {row.kind !== 'system' && <><button type="button" className="kn-btn" disabled={busy} onClick={() => void act(async () => {
          await extensionRequest('mcp/save', { id: row.id, enabled: !row.enabled });
        })}>{t(row.enabled ? 'mcp.disable' : 'mcp.enable')}</button>
          <button type="button" className="kn-btn" disabled={busy} onClick={() => edit(row)}>{t('mcp.edit')}</button>
          <button type="button" className="kn-btn" disabled={busy} onClick={() => setConfirmDelete(row.id)}>{t('mcp.delete')}</button></>}
      </div>
      {confirmDelete === row.id && <div className="mcp-delete"><p>{t('mcp.deleteHint', { name: row.name })}</p>
        <div className="extension-actions">
          <button type="button" className="kn-btn danger" disabled={busy} onClick={() => void act(async () => { await extensionRequest('mcp/delete', { id: row.id }); setConfirmDelete(''); })}>{t('mcp.confirmDelete')}</button>
          <button type="button" className="kn-btn" disabled={busy} onClick={() => setConfirmDelete('')}>{t('mcp.cancel')}</button>
        </div></div>}
      {tools[row.id] && <div className="mcp-tools"><h4>{t('mcp.availableTools', { n: tools[row.id]!.length })}</h4>
        <label className="mcp-permission"><input type="checkbox" disabled={busy} checked={row.allowed_tools === null} onChange={event => void act(async () => {
          await extensionRequest('mcp/save', { id: row.id, allowed_tools: event.target.checked ? null : tools[row.id]!.map(tool => tool.name) });
        })} />{t('mcp.allowAll')}</label>
        <p>{t('mcp.permissionsHint')}</p>
        {tools[row.id]!.map(tool => <div key={tool.name} className="mcp-tool-row"><label className="mcp-permission"><input type="checkbox" disabled={busy}
          checked={row.allowed_tools === null || row.allowed_tools.includes(tool.name)} onChange={event => {
            const allowed = new Set(row.allowed_tools ?? tools[row.id]!.map(item => item.name));
            if (event.target.checked) allowed.add(tool.name); else allowed.delete(tool.name);
            void act(async () => { await extensionRequest('mcp/save', { id: row.id, allowed_tools: [...allowed] }); });
          }} />{tool.name}</label><details><summary>{t('mcp.toolDetails')}</summary><p>{tool.description}</p>
          <pre>{JSON.stringify(tool.inputSchema, null, 2)}</pre></details></div>)}
      </div>}
    </article>)}</div>}
  </section>;
}
