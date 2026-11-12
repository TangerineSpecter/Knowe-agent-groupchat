// @vitest-environment jsdom
import React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useSettingsStore } from '../store/settings';
import FirstRunModelGate from './FirstRunModelGate';

vi.mock('../shared/runtimeFetch', () => ({ runtimeFetch: (...args: unknown[]) => fetch(...args as Parameters<typeof fetch>) }));
vi.mock('./PrimaryLanguageModule', () => ({ default: () => null }));
vi.mock('react-i18next', async (original) => ({
  ...await original<typeof import('react-i18next')>(),
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock('./ModelBindingModule', () => ({ default: (props: {
  autoTest?: boolean;
  onSave: (b: { provider: string; model: string; apiKey: string }) => void;
}) => <button onClick={() => {
  expect(props.autoTest).toBe(false);
  props.onSave({ provider: 'deepseek', model: 'deepseek-v4-flash', apiKey: 'new-key' });
}}>save</button> }));

const ack = {
  ok: true, settings_revision: 1, applied_fingerprint: 'fp', zinnia_compatible: true,
  effective: { main_model: { provider: 'deepseek', model: 'deepseek-v4-flash', has_api_key: true } },
};
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200 });

beforeEach(() => {
  localStorage.clear();
  useSettingsStore.setState({ mainModel: null, auxModel: null, agentModels: {}, modelProfiles: [], mainPoolIdxs: [], auxPoolIdxs: [], settingsRevision: 0,
    modelApplyState: 'idle', modelApplyError: null, appliedFingerprint: null, zinniaCompatible: false });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('runs one probe and applies the same fingerprint; redaction does not clear success', async () => {
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    calls.push(url);
    const body = JSON.parse(init.body as string);
    if (url.endsWith('/settings/test')) {
      expect(body.binding.api_key).toBe('new-key');
      return json({ ok: true, message: 'connected', tested_fingerprint: 'fp' });
    }
    expect(body.expected_fingerprint).toBe('fp');
    expect(body.main_model.api_key).toBe('new-key');
    expect(body.model_profiles[0].api_key).toBe('new-key');
    expect(body.main_pool).toEqual([0]);
    return json(ack);
  }));
  render(<FirstRunModelGate />);
  fireEvent.click(screen.getByText('save'));
  await waitFor(() => expect((screen.getByText('first.run.model.gate.enter') as HTMLButtonElement).disabled).toBe(false));
  expect(calls.filter(url => url.endsWith('/settings/test'))).toHaveLength(1);
  expect(useSettingsStore.getState().mainModel?.apiKey).toBe('');
});

it('does not replace a newly entered key with an older save acknowledgement', async () => {
  let finish!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve; })));
  const store = useSettingsStore.getState();
  store.saveMainModel({ provider: 'deepseek', model: 'deepseek-v4-flash', apiKey: 'old-key' });
  const pending = store.pushToBackend();
  await waitFor(() => expect(finish).toBeDefined());
  store.saveMainModel({ provider: 'deepseek', model: 'deepseek-v4-flash', apiKey: 'new-key' });
  await act(async () => { finish(json(ack)); await pending; });
  expect(useSettingsStore.getState().mainModel?.apiKey).toBe('new-key');
  // Flush the scheduled save so no timer leaks into the next test.
  vi.stubGlobal('fetch', vi.fn(async () => json(ack)));
  await store.pushToBackend();
});

it('keeps entry disabled on authentication failure and allows a successful retry', async () => {
  let attempts = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (!url.endsWith('/settings/test')) return json(ack);
    attempts += 1;
    return json(attempts === 1
      ? { ok: false, message: 'HTTP 401' }
      : { ok: true, message: 'connected', tested_fingerprint: 'fp' });
  }));
  render(<FirstRunModelGate />);
  fireEvent.click(screen.getByText('save'));
  await screen.findByText('HTTP 401');
  expect((screen.getByText('first.run.model.gate.enter') as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByText('first.run.model.gate.testApply'));
  await waitFor(() => expect((screen.getByText('first.run.model.gate.enter') as HTMLButtonElement).disabled).toBe(false));
  expect(screen.queryByText('HTTP 401')).toBeNull();
});
