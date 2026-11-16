import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MessageBubble } from '../components/MessageBubble';
import { Markdown } from '../components/markdown';
import { parseChart } from '../components/chartSpec';

vi.mock('../components/InlineChart', () => ({
  default: ({ spec }: { spec: { title: string } }) => <figure>{spec.title}</figure>,
}));
afterEach(cleanup);

const motion = {
  type: 'line', title: 'Motion', xLabel: 'Time (s)', x: { start: 0, end: 2, count: 81 },
  series: [
    { name: 'Angle', unit: '°', formula: '20*sin(pi*x)' },
    { name: 'Velocity', axis: 'right', unit: '°/s', formula: '20*pi*cos(pi*x)' },
  ],
};
const fence = (data: unknown) => '```knowe-chart\n' + JSON.stringify(data) + '\n```';

it('calculates both axes from formulas at the same physical instant', () => {
  const chart = parseChart(JSON.stringify(motion))!;
  expect(chart.x[20]).toBe(.5);
  expect(chart.series[0]!.values[20]).toBeCloseTo(20);
  expect(chart.series[1]!.values[20]).toBeCloseTo(0);
  expect(chart.series[1]!.values[0]).toBeCloseTo(20 * Math.PI);
});

it('accepts categorical measurements and rejects mismatched series or unsafe formulas', () => {
  const bars = { type: 'bar', title: 'Latency', x: ['A', 'B'], series: [{ name: 'ms', values: [20, 35] }] };
  expect(parseChart(JSON.stringify(bars))?.series[0]?.values).toEqual([20, 35]);
  expect(parseChart(JSON.stringify({ ...bars, x: ['A', 'B', 'C'] }))).toBeUndefined();
  for (const formula of ['globalThis.location', 'constructor(1)', '1/0']) {
    expect(parseChart(JSON.stringify({ ...motion, series: [{ name: 'invalid', formula }] }))).toBeUndefined();
  }
});

it('retains ordered prose and multiple charts only when rendering agent content', async () => {
  const text = 'Before\n\n' + fence(motion) + '\n\nBetween\n\n' + fence(motion) + '\n\nAfter';
  const { container, rerender } = render(<Markdown charts text={text} />);
  await screen.findAllByRole('figure');
  expect(Array.from(container.querySelectorAll('p, figure'), el => el.textContent))
    .toEqual(['Before', 'Motion', 'Between', 'Motion', 'After']);
  rerender(<Markdown text={text} />);
  expect(container.querySelector('figure')).toBeNull();
  expect(container.querySelectorAll('pre')).toHaveLength(2);
});

it('keeps literal examples and malformed blocks readable as code', () => {
  const { container } = render(<Markdown charts text={'````markdown\n' + fence(motion) + '\n````\n\n' + fence({})} />);
  expect(container.querySelector('figure')).toBeNull();
  expect(container.querySelectorAll('pre')).toHaveLength(2);
});

it.each(['pie', 'doughnut'])('accepts a single nonnegative breakdown with a positive total for %s', type => {
  const shares = (values: number[]) => JSON.stringify({
    type, title: 'Sales', x: values.map((_, i) => 'Region ' + i), series: [{ name: 'Sales', values }],
  });
  expect(parseChart(shares([20, 0, 80]))?.type).toBe(type);
  expect(parseChart(shares([15]))?.x).toHaveLength(1);
  expect(parseChart(shares([0, 0]))).toBeUndefined();
  expect(parseChart(shares([-10, 40]))).toBeUndefined();
});

it('requires both bars and lines in a mixed chart and preserves independent axes', () => {
  const bars = { type: 'bar', name: 'Orders', unit: 'orders', values: [120, 150] };
  const line = { type: 'line', name: 'Conversion', unit: '%', axis: 'right', values: [3, 4] };
  const mixed = { type: 'mixed', title: 'Sales', x: ['May', 'June'], series: [bars, line] };
  expect(parseChart(JSON.stringify(mixed))?.series.map(s => [s.type, s.axis]))
    .toEqual([['bar', 'left'], ['line', 'right']]);
  expect(parseChart(JSON.stringify({ ...mixed, series: [bars] }))).toBeUndefined();
  expect(parseChart(JSON.stringify({ ...mixed, series: [bars, { ...line, type: undefined }] }))).toBeUndefined();
});

it('preserves literal chart marker text in Markdown attributes around real charts', async () => {
  const title = 'before <!--knowe-chart-slot--> after';
  const link = '[Details](/details "' + title + '")';
  render(<Markdown charts text={[link, fence(motion), link].join('\n\n')} />);
  await screen.findByRole('figure');
  const links = screen.getAllByRole('link', { name: 'Details' });
  expect(links.map(el => el.title)).toEqual([title, title]);
  expect(links[0]!.compareDocumentPosition(screen.getByRole('figure'))).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
});

it('renders charts in forwarded Agent replies and keeps user chart examples as code', async () => {
  const forwarded = { comment: '', originalText: fence(motion), sourceName: 'Sender', markdown: false };
  const { container, rerender } = render(<MessageBubble kind="user" text="" forwarded={forwarded} />);
  expect(container.querySelector('.fwd-quote-b pre')).toBeTruthy();
  expect(screen.queryByRole('figure')).toBeNull();
  rerender(<MessageBubble kind="user" text="" forwarded={{ ...forwarded, markdown: true }} />);
  expect(await screen.findByRole('figure')).toHaveTextContent('Motion');
});

it('expands long Agent messages only for a valid top-level chart', async () => {
  const height = vi.spyOn(Element.prototype, 'scrollHeight', 'get').mockReturnValue(1500);
  try {
    const example = '````markdown\n' + fence(motion) + '\n````';
    const { container, rerender } = render(<MessageBubble kind="agent" text={example} />);
    await waitFor(() => expect(container.querySelector('.bubble.agent')).toHaveClass('folded'));
    rerender(<MessageBubble kind="agent" text={fence(motion)} />);
    await screen.findByRole('figure');
    expect(container.querySelector('.bubble.agent')).not.toHaveClass('folded');
    rerender(<MessageBubble kind="agent" text={fence({})} />);
    expect(container.querySelector('.bubble.agent')).toHaveClass('folded');
  } finally {
    height.mockRestore();
  }
});
