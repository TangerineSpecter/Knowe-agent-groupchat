// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { runtimeHttpBase, runtimeWsUrl } from '../shared/runtimeEndpoints';
import { useSettingsStore } from '../store/settings';
import ServerDirectoryPicker from './ServerDirectoryPicker';
import WebEntry from './WebEntry';
import { webRequest } from './api';

beforeEach(() => { useSettingsStore.setState({ language: 'zh' }); localStorage.clear(); vi.stubEnv('MODE', 'web'); delete window.knowe; });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

it('rejects an invalid success body instead of treating a proxy HTML page as application state', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>proxy error</html>')));
  await expect(webRequest('session')).rejects.toThrow();
});

it('uses same-origin HTTP and WebSocket in web mode', () => {
  expect(runtimeHttpBase()).toBe(`${location.origin}/api/runtime`);
  expect(runtimeWsUrl()).toBe(`ws://${location.host}/api/runtime/ws`);
});

it('keeps desktop/browser-dev endpoints unchanged outside web mode', () => {
  vi.stubEnv('MODE', 'development');
  expect(runtimeHttpBase()).toBe('http://127.0.0.1:8081');
});

it('defaults to automatic directory creation and can choose an existing server directory', async () => {
  const onChange = vi.fn();
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ path: '/workspace', root: '/workspace', directories: ['资料'] })));
  vi.stubGlobal('fetch', fetcher);
  render(<ServerDirectoryPicker value="" onChange={onChange} allowAuto />);
  expect(fetcher).not.toHaveBeenCalled();
  expect(screen.getByText(/自动分配独立文件夹/)).toBeVisible();
  fireEvent.change(screen.getByLabelText('项目文件夹创建方式'), { target: { value: 'existing' } });
  expect(onChange).toHaveBeenCalledWith('web:choose-directory');
  await screen.findByRole('button', { name: '资料/' });
  fireEvent.click(screen.getByRole('button', { name: '使用当前文件夹' }));
  expect(onChange).toHaveBeenCalledWith('/workspace');
});

it('shows validation error and disables directory controls when requested', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ path: '/workspace', root: '/workspace', directories: [] }))));
  const { rerender } = render(<ServerDirectoryPicker value="web:choose-directory" onChange={() => {}} allowAuto showError />);
  expect(screen.getByLabelText('项目文件夹创建方式')).toHaveClass('needs-pick');
  expect(screen.getByRole('alert')).toBeVisible();
  await screen.findByRole('button', { name: '使用当前文件夹' });
  rerender(<ServerDirectoryPicker value="web:choose-directory" onChange={() => {}} allowAuto disabled />);
  expect(screen.getByLabelText('项目文件夹创建方式')).toBeDisabled();
  expect(screen.getByRole('button', { name: '使用当前文件夹' })).toBeDisabled();
});

it('reads server settings before mounting the application, avoiding a false first-run gate', async () => {
  let resolve!: (value: Awaited<ReturnType<ReturnType<typeof useSettingsStore.getState>['reconcileFromBackend']>>) => void;
  vi.spyOn(useSettingsStore.getState(), 'reconcileFromBackend').mockImplementation(() => new Promise(done => { resolve = done; }));
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ authenticated: true, instance_id: 'server-one' }))));
  render(<WebEntry><div>application mounted</div></WebEntry>);
  await waitFor(() => expect(resolve).toBeDefined());
  expect(screen.queryByText('application mounted')).toBeNull();
  resolve({ ok: true, restartRequired: false, settingsRevision: 1, appliedFingerprint: 'fp', zinniaCompatible: true, zinniaBindingSource: 'main', welcomeState: 'ready', message: '' });
  await screen.findByText('application mounted');
});

it('does not mount application content before login', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ authenticated: false }))));
  render(<WebEntry><div>private application</div></WebEntry>);
  await screen.findByLabelText('访问密码');
  expect(screen.queryByText('private application')).toBeNull();
});

it('does not mount late settings after the session expires during bootstrap', async () => {
  let resolve!: (value: Awaited<ReturnType<ReturnType<typeof useSettingsStore.getState>['reconcileFromBackend']>>) => void;
  vi.spyOn(useSettingsStore.getState(), 'reconcileFromBackend').mockImplementation(() => new Promise(done => { resolve = done; }));
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ authenticated: true, instance_id: 'server-one' }))));
  render(<WebEntry><div>private application</div></WebEntry>);
  await waitFor(() => expect(resolve).toBeDefined());
  window.dispatchEvent(new Event('knowe:web-auth-required'));
  await screen.findByLabelText('访问密码');
  resolve({ ok: true, restartRequired: false, settingsRevision: 1, appliedFingerprint: 'fp', zinniaCompatible: true, zinniaBindingSource: 'main', welcomeState: 'ready', message: '' });
  await waitFor(() => expect(screen.getByLabelText('访问密码')).toBeVisible());
  expect(screen.queryByText('private application')).toBeNull();
});

it('preserves unlimited global and per-project approval timeouts when reconciling', async () => {
  useSettingsStore.setState({ approvalTimeoutS: 300, groupApprovalTimeouts: {} });
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    settings_revision: 1, main_model: null, model_profiles: [], main_pool: [], aux_pool: [],
    approval_timeout_s: null, group_approval_timeouts: { 'project-one': null, 'project-two': 60 },
  }))));
  await useSettingsStore.getState().reconcileFromBackend();
  expect(useSettingsStore.getState().approvalTimeoutS).toBe(0);
  expect(useSettingsStore.getState().groupApprovalTimeouts).toEqual({ 'project-one': 0, 'project-two': 60 });
});


it('uses the selected language on the login screen and server directory controls', async () => {
  useSettingsStore.setState({ language: 'en' });
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ authenticated: false }))));
  render(<WebEntry><div>private application</div></WebEntry>);
  await screen.findByLabelText('Access password');
  expect(screen.getByRole('button', { name: 'Open workspace' })).toBeVisible();
  render(<ServerDirectoryPicker value="" onChange={() => {}} allowAuto />);
  expect(screen.getByLabelText('Project folder setup')).toHaveValue('auto');
  expect(screen.getByText('Each project gets its own folder. Files are stored on the server.')).toBeVisible();
});
