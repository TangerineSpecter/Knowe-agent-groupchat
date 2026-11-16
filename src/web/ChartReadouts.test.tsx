import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import InlineChart from '../components/InlineChart';
import { parseChart } from '../components/chartSpec';

vi.mock('chart.js', async importOriginal => ({
  ...await importOriginal<typeof import('chart.js')>(),
  Chart: class { static register() {} destroy() {} },
}));
afterEach(cleanup);

it.each(['line', 'doughnut'])('keeps each original value in %s keyboard readouts', type => {
  const source = JSON.stringify({ type, title: 'Counts', x: ['A', 'B', 'C', 'D'],
    series: [{ name: 'Count', unit: ' pcs', values: [100001, 100002, 1, 1e16] }] });
  render(<InlineChart spec={parseChart(source)!} source={source} />);
  for (const value of ['100001', '100002', '1', '10000000000000000']) {
    fireEvent.keyDown(screen.getByRole('img'), { key: 'ArrowRight' });
    expect(screen.getByRole('status')).toHaveTextContent(value + ' pcs');
  }
});
