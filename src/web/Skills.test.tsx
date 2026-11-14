import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { EditorContent, useEditor } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { SkillInstall } from '../components/SkillInstall';
import { useSkillPicker } from '../components/useSkillPicker';
import { useSettingsStore } from '../store/settings';
import { UserMessageCmdSchema } from '../contract/envelope';

beforeEach(() => { useSettingsStore.setState({ language: 'zh' }); vi.stubEnv('MODE', 'web'); delete window.knowe; });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

it('previews a repository and explicitly confirms replacement before installing', async () => {
  const requests: { path: string; body: unknown }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    const path = String(input); const body = init?.body ? JSON.parse(init.body) : null;
    requests.push({ path, body });
    return new Response(JSON.stringify(path.endsWith('settings') ? { manual_directory: '/data/skills' }
      : path.endsWith('prepare') ? { token: 'token', source: 'GitHub', invalid: [], skills: [
        { name: 'report', description: '汇总报告', path: 'repo/report', exists: true },
      ] } : { ok: true }));
  }));
  const installed = vi.fn();
  render(<SkillInstall onInstalled={installed} onClose={() => {}} />);
  fireEvent.change(screen.getByLabelText('GitHub 仓库或技能目录'), { target: { value: 'owner/repo' } });
  fireEvent.click(screen.getByRole('button', { name: '预览技能' }));
  await screen.findByText('汇总报告');
  fireEvent.click(screen.getByRole('checkbox', { name: /report/ }));
  expect(screen.getByRole('button', { name: '安装所选 1 个技能' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: /替换所选/ }));
  fireEvent.click(screen.getByRole('button', { name: '安装所选 1 个技能' }));
  await waitFor(() => expect(installed).toHaveBeenCalledOnce());
  expect(requests.find(row => row.path.endsWith('install'))?.body).toEqual({ token: 'token', selected: ['repo/report'], replace: true });
});

function PickerHarness({ project = 'p1' }: { project?: string }) {
  const editor = useEditor({ extensions: [StarterKit], content: '', editorProps: { attributes: { 'aria-label': 'message' } } });
  const picker = useSkillPicker(editor, project);
  return <><button onClick={() => editor?.commands.setContent('<p>/rep</p>')}>Type slash</button>
    <button onClick={() => editor?.commands.clearContent()}>Clear editor</button>
    <button onClick={() => editor?.commands.setTextSelection(5)}>Move cursor</button>
    <button onClick={() => picker.keyDown(new KeyboardEvent('keydown', { key: 'Enter' }))}>Select by keyboard</button>
    <output>{picker.selected.map(row => row.pack_id).join(',')}</output><EditorContent editor={editor} />{picker.ui}</>;
}

it('selects slash skills by keyboard and keeps selections isolated between chats', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ third_party: [
    { pack_id: 'third:report', name: 'report', description: '报告', status: 'active' },
    { pack_id: 'third:hidden', name: 'report-hidden', status: 'active', user_invocable: false },
    { pack_id: 'third:retired', name: 'report-retired', status: 'retired' },
  ] }))));
  const { rerender } = render(<PickerHarness />);
  fireEvent.click(screen.getByText('Type slash')); fireEvent.click(screen.getByText('Move cursor'));
  await screen.findByRole('option', { name: /report/ });
  expect(screen.getAllByRole('option')).toHaveLength(1);
  fireEvent.click(screen.getByText('Select by keyboard'));
  expect(screen.getByRole('status')).toHaveTextContent('third:report');
  expect(screen.getByLabelText('message')).not.toHaveTextContent('/rep');
  rerender(<PickerHarness project="p2" />);
  expect(screen.getByRole('status')).toBeEmptyDOMElement();
  rerender(<PickerHarness project="p1" />);
  expect(screen.getByRole('status')).toHaveTextContent('third:report');
});

it('keeps selected skill IDs in the outgoing protocol and enforces a bounded selection', () => {
  const payload = { type: 'user_message', project_id: 'p', content: '/report', client_msg_id: 'm', skills: ['skill'] };
  expect(UserMessageCmdSchema.parse(payload).skills).toEqual(['skill']);
  expect(UserMessageCmdSchema.safeParse({ ...payload, skills: Array(9).fill('skill') }).success).toBe(false);
});

it('does not offer slash skills in Zinnia and restores the picker in project chats', async () => {
  const fetch = vi.fn(async () => new Response(JSON.stringify({ third_party: [
    { pack_id: 'third:report', name: 'report', description: '报告', status: 'active' },
  ] })));
  vi.stubGlobal('fetch', fetch);
  const { rerender } = render(<PickerHarness project="__platform__" />);
  fireEvent.click(screen.getByText('Type slash')); fireEvent.click(screen.getByText('Move cursor'));
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
  expect(screen.getByLabelText('message')).toHaveTextContent('/rep');
  rerender(<PickerHarness project="p1" />);
  fireEvent.click(screen.getByText('Clear editor'));
  fireEvent.click(screen.getByText('Type slash')); fireEvent.click(screen.getByText('Move cursor'));
  await screen.findByRole('option', { name: /report/ });
  fireEvent.click(screen.getByText('Select by keyboard'));
  rerender(<PickerHarness project="__platform__" />);
  expect(screen.getByRole('status')).toBeEmptyDOMElement();
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
});
