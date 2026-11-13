import '@testing-library/jest-dom/vitest';
import React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import i18n from '../i18n';
import { useKnoweStore } from '../store/store';
import NotificationCenter from './NotificationCenter';
import ServerDirectoryPicker from './ServerDirectoryPicker';

const originalAddNotice = useKnoweStore.getState().addNotice;

beforeEach(() => {
  vi.stubEnv('MODE', 'web');
  useKnoweStore.setState({ addNotice: vi.fn() });
});

afterEach(async () => {
  cleanup();
  useKnoweStore.setState({ addNotice: originalAddNotice });
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  await i18n.changeLanguage('zh');
});

it.each([
  ['zh', 3, 1, '你有 3 条未读通知，其中 1 条待审批', '通知中心，3 条未读'],
  ['en', 5, 2, 'Unread notifications: 5 · Awaiting approval: 2', 'Notifications · 5 unread'],
  ['zh', 1, 0, '你有 1 条未读通知', '通知中心，1 条未读'],
  ['en', 1, 0, 'Unread notifications: 1', 'Notifications · 1 unread'],
] as const)('formats the %s notification summary and bell with %i unreads', async (language, unread, pending, summary, label) => {
  await i18n.changeLanguage(language);
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    items: [], unread, pending, latest_id: 5, next_before: null,
  }))));
  render(<NotificationCenter />);
  await screen.findByRole('button', { name: label });
  await waitFor(() => expect(useKnoweStore.getState().addNotice).toHaveBeenCalledWith(summary));
});

it('does not show an unread summary when there are no unread notifications', async () => {
  await i18n.changeLanguage('zh');
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    items: [], unread: 0, pending: 0, latest_id: 0, next_before: null,
  }))));
  render(<NotificationCenter />);
  fireEvent.click(screen.getByRole('button', { name: '通知中心' }));
  await screen.findByText('暂无通知。需要审批、任务完成或异常时，会显示在这里。');
  expect(useKnoweStore.getState().addNotice).not.toHaveBeenCalled();
});

it.each([
  ['zh', '已选择：/workspace/项目资料'],
  ['en', 'Selected: /workspace/项目资料'],
] as const)('formats the selected directory using the %s runtime translations', async (language, selected) => {
  await i18n.changeLanguage(language);
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    path: '/workspace/项目资料', root: '/workspace', directories: [],
  }))));
  render(<ServerDirectoryPicker value="/workspace/项目资料" onChange={() => {}} />);
  expect(screen.getByText(selected)).toBeVisible();
  await screen.findByRole('button', { name: language === 'zh' ? '使用当前文件夹' : 'Use this folder' });
});
