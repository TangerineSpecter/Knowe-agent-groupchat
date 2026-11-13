import '@testing-library/jest-dom/vitest';
import React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import i18n from '../i18n';
import packageJson from '../../package.json';
import Rail from '../components/Rail';
import SettingsView from '../components/SettingsView';
import { resetStore } from '../components/__testkit';

const revision = '0123456789abcdef0123456789abcdef01234567';

beforeEach(async () => {
  resetStore();
  vi.stubEnv('MODE', 'web');
  delete window.knowe;
  vi.stubGlobal('__KNOWE_BUILD_COMMIT__', revision);
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    items: [], unread: 0, pending: 0, latest_id: 0, next_before: null,
  }))));
  await i18n.changeLanguage('zh');
});

afterEach(async () => {
  cleanup();
  resetStore();
  delete window.knowe;
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  await i18n.changeLanguage('zh');
});

it.each([
  ['zh', '关于', '构建提交', '单用户网页版'],
  ['en', 'About', 'Build commit', 'Single-user web edition'],
])('shows the same version in the %s main navigation and About pane', async (language, about, label, edition) => {
  await i18n.changeLanguage(language);
  const { container } = render(<><Rail /><SettingsView /></>);
  const footer = container.querySelector('.rail-version') as HTMLElement;
  expect(within(footer).getByText(`v${packageJson.version}`)).toBeVisible();
  expect(within(footer).getByText(revision.slice(0, 7))).toHaveAccessibleName(`${label} ${revision}`);
  fireEvent.click(screen.getByRole('button', { name: about, exact: true }));
  const pane = container.querySelector('.about-pane') as HTMLElement;
  expect(within(pane).getByText(`v${packageJson.version}`)).toBeVisible();
  expect(within(pane).getByText(edition)).toBeVisible();
  expect(within(pane).getByText(revision)).toBeVisible();
});

it('keeps the product version visible and marks a build without recorded commit metadata', () => {
  vi.stubGlobal('__KNOWE_BUILD_COMMIT__', undefined);
  const { container } = render(<><Rail /><SettingsView /></>);
  expect(container.querySelector('.rail-version')).toHaveTextContent(`v${packageJson.version}`);
  expect(container.querySelector('.rail-version code')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '关于', exact: true }));
  expect(screen.getByText('未记录')).toBeVisible();
});

it('identifies a build containing local source changes', () => {
  vi.stubGlobal('__KNOWE_BUILD_COMMIT__', `${revision}-dirty`);
  const { container } = render(<Rail />);
  const footer = container.querySelector('.rail-version') as HTMLElement;
  expect(footer).toHaveTextContent(`${revision.slice(0, 7)}*`);
  expect(footer).toHaveAttribute('title', `构建提交: ${revision}-dirty`);
});

it('continues displaying the product version supplied by Electron on desktop', () => {
  vi.stubEnv('MODE', 'production');
  window.knowe = { version: '9.8.7', isElectron: true } as Window['knowe'];
  const { container } = render(<><Rail /><SettingsView /></>);
  expect(container.querySelector('.rail-version')).toHaveTextContent('v9.8.7');
  fireEvent.click(screen.getByRole('button', { name: '关于', exact: true }));
  const pane = container.querySelector('.about-pane') as HTMLElement;
  expect(within(pane).getByText('v9.8.7')).toBeVisible();
  expect(within(pane).queryByText('构建提交')).toBeNull();
});
