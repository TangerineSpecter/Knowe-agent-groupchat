import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import LibraryView from '../components/LibraryView';
import { McpPanel } from '../components/McpPanel';
import { useSettingsStore } from '../store/settings';

beforeEach(() => { useSettingsStore.setState({ language: 'zh' }); vi.stubEnv('MODE', 'web'); delete window.knowe; });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

it('requires review before trashing referenced files and renders partial failures', async () => {
  const actions: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    if (String(input).endsWith('/action')) {
      actions.push(JSON.parse(init.body));
      return new Response(JSON.stringify({ ok: true, succeeded: [], failed: [{ id: 'f', error: '文件已发生变化，请刷新后重试' }] }));
    }
    return new Response(JSON.stringify({ items: [{ id: 'f', name: 'story.md', ext: 'md', size: 128,
      origin: 'generated', managed: true, status: 'active', mtime: 1, version: 'v1', favorite: false,
      references: [{ project_id: 'p', project_name: '写作', seq: 7, agent_id: 'writer' }] }],
      total: 1, next_offset: null, projects: [], storage: { active_bytes: 128, trash_bytes: 0, active_count: 1 } }));
  }));
  render(<LibraryView onOpenReference={() => {}} />);
  await screen.findByRole('button', { name: 'story.md' });
  fireEvent.click(screen.getByRole('button', { name: '移入回收站' }));
  expect(actions).toHaveLength(0);
  expect(screen.getByRole('alertdialog')).toHaveTextContent('原聊天中的文件卡片将无法打开');
  fireEvent.click(screen.getAllByRole('button', { name: '移入回收站' })[0]!);
  await waitFor(() => expect(actions).toEqual([{ action: 'trash', files: [{ id: 'f', version: 'v1' }], confirm_references: true }]));
  expect(await screen.findByRole('alert')).toHaveTextContent('文件已发生变化');
});

it('keeps local references distinguished from managed storage', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ items: [{ id: 'local', name: 'original.pdf', ext: 'pdf', size: 999,
    origin: 'external', managed: false, status: 'active', mtime: 1, version: 'v1', favorite: false, references: [] }],
    total: 1, next_offset: null, projects: [], storage: { active_bytes: 0, trash_bytes: 0, active_count: 1 } }))));
  render(<LibraryView onOpenReference={() => {}} />);
  await screen.findByRole('button', { name: 'original.pdf' });
  fireEvent.click(screen.getByRole('button', { name: '移入回收站' }));
  expect(screen.getByRole('alertdialog')).toHaveTextContent('电脑上的原始文件会保留');
});

it('edits MCP tool permissions after discovering actual schemas', async () => {
  const writes: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    if (String(input).endsWith('mcp/test')) return new Response(JSON.stringify({ tools: [{ name: 'lookup', description: '查询', inputSchema: { type: 'object' } }] }));
    if (String(input).endsWith('mcp/save')) { writes.push(JSON.parse(init.body)); return new Response('{"ok":true}'); }
    return new Response(JSON.stringify({ servers: [{ id: 's', name: 'Example', transport: 'http', url: 'https://example.com/mcp',
      enabled: true, kind: 'third_party', allowed_tools: null, status: 'disconnected', error: '', header_names: ['Authorization'], env_names: [], timeout: 60 }] }));
  }));
  render(<McpPanel scope="all" />);
  await screen.findByText('Example');
  fireEvent.click(screen.getByRole('button', { name: '测试连接 / 查看工具' }));
  await screen.findByRole('checkbox', { name: 'lookup' });
  fireEvent.click(screen.getByRole('checkbox', { name: 'lookup' }));
  await waitFor(() => expect(writes).toEqual([{ id: 's', allowed_tools: [] }]));
});
