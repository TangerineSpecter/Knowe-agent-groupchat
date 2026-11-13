import '@testing-library/jest-dom/vitest';
import React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import i18n from '../i18n';
import ApprovalCard from '../components/ApprovalCard';
import { installSocketSpy, resetStore } from '../components/__testkit';
import { useKnoweStore } from '../store/store';

beforeEach(async () => {
  resetStore();
  vi.stubEnv('MODE', 'web');
  delete window.knowe;
  await i18n.changeLanguage('zh');
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  resetStore();
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

function renderProject(expiresAt: string) {
  return render(<ApprovalCard cardId="ap-web-create" projectId="platform" tool="create_project"
    card={{ status: 'pending_approval', expires_at: expiresAt, project_name: '写给猫咪的小故事' }}
    state="pending" expiresAt={expiresAt} members={[]} />);
}

it('keeps unlimited approvals actionable and sends project creation with an automatic server directory', () => {
  const spy = installSocketSpy();
  const createProject = vi.fn();
  useKnoweStore.getState()._socket!.createProject = createProject;
  const { container } = renderProject('9999-12-31T23:59:59Z');
  act(() => { vi.advanceTimersByTime(5000); });

  expect(screen.getByText('永不超时')).toBeVisible();
  expect(container.querySelector('.ap-progress')).toBeNull();
  const confirm = screen.getByRole('button', { name: '确认', exact: true });
  expect(confirm).toBeEnabled();
  expect(screen.getByRole('button', { name: '拒绝', exact: true })).toBeEnabled();
  fireEvent.click(confirm);

  expect(createProject).toHaveBeenCalledTimes(1);
  expect(createProject).toHaveBeenCalledWith(
    expect.stringMatching(/^project_\d{14}$/), '写给猫咪的小故事', '', 'ap-web-create', [],
  );
  expect(spy.approved).toEqual([{ id: 'ap-web-create', projectId: 'platform' }]);
  expect(screen.getByRole('button', { name: '已提交', exact: true })).toBeDisabled();
  expect(container.querySelector('.approval.settled')).toBeNull();
});

it('can reject an unlimited approval after the card has been open for a while', () => {
  const spy = installSocketSpy();
  renderProject('9999-12-31T23:59:59Z');
  act(() => { vi.advanceTimersByTime(5000); });
  fireEvent.click(screen.getByRole('button', { name: '拒绝', exact: true }));
  expect(spy.rejected).toEqual([{ id: 'ap-web-create', projectId: 'platform' }]);
  expect(spy.created).toEqual([]);
});

it('disables a finite approval at its actual deadline', () => {
  const spy = installSocketSpy();
  renderProject(new Date(Date.now() + 5000).toISOString());
  act(() => { vi.advanceTimersByTime(4000); });
  expect(screen.getByRole('button', { name: '确认', exact: true })).toBeEnabled();
  act(() => { vi.advanceTimersByTime(1000); });
  expect(screen.getByText('已超时')).toBeVisible();
  expect(screen.getByRole('button', { name: '确认', exact: true })).toBeDisabled();
  expect(spy.created).toEqual([]);
});

it.each([30, 400])('keeps a finite %i-day deadline finite and actionable without timer overflow', days => {
  renderProject(new Date(Date.now() + days * 86400_000).toISOString());
  act(() => { vi.advanceTimersByTime(5000); });
  expect(screen.getByRole('button', { name: '确认', exact: true })).toBeEnabled();
  expect(screen.queryByText('永不超时')).toBeNull();
  expect(screen.getByText(/^剩余 /)).toBeVisible();
});
