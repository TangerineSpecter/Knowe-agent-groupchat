import jsep from 'jsep';
import { z } from 'zod';

const label = z.string().trim().min(1).max(180);
const number = z.number().finite();
const samples = z.array(number).min(1).max(500);
const range = z.object({ start: number, end: number, count: z.number().int().min(2).max(500).default(81) })
  .refine(r => r.end > r.start)
  .transform(({ start, end, count }) => Array.from({ length: count }, (_, i) =>
    i === count - 1 ? end : start + (end - start) * (i / (count - 1))));
const spec = z.object({
  type: z.enum(['line', 'bar', 'pie', 'doughnut', 'mixed']),
  title: label,
  xLabel: label.optional(),
  x: z.union([range, samples, z.array(label).min(1).max(500)]),
  series: z.array(z.object({
    type: z.enum(['line', 'bar']).optional(),
    name: label, unit: z.string().max(32).default(''), axis: z.enum(['left', 'right']).default('left'),
    values: samples.optional(), formula: z.string().min(1).max(180).optional(),
  })).min(1).max(6),
});

type Input = z.infer<typeof spec>;
type Series = Omit<Input['series'][number], 'formula' | 'values'> & { values: number[] };
export type ChartSpec = Omit<Input, 'series' | 'x'> & { x: (number | string)[]; series: Series[] };

export function isPartToWhole(type: ChartSpec['type']): boolean {
  return type === 'pie' || type === 'doughnut';
}

// Interpret only arithmetic over x. Formula text never becomes JavaScript.
const functions = new Map<string, (v: number) => number>([
  ['sin', Math.sin], ['cos', Math.cos], ['tan', Math.tan], ['sqrt', Math.sqrt],
  ['exp', Math.exp], ['log', Math.log], ['abs', Math.abs],
]);
const operators = new Map<string, (a: number, b: number) => number>([
  ['+', (a, b) => a + b], ['-', (a, b) => a - b], ['*', (a, b) => a * b], ['/', (a, b) => a / b],
]);

function evaluate(node: jsep.CoreExpression, x: number): number {
  switch (node.type) {
    case 'Literal': if (typeof node.value === 'number') return node.value; break;
    case 'Identifier':
      if (node.name === 'x' || node.name === 't') return x;
      if (node.name === 'pi' || node.name === 'π') return Math.PI;
      break;
    case 'UnaryExpression':
      if (node.operator === '-') return -evaluate(node.argument as jsep.CoreExpression, x);
      if (node.operator === '+') return evaluate(node.argument as jsep.CoreExpression, x);
      break;
    case 'BinaryExpression': {
      const op = operators.get(node.operator);
      if (op) return op(evaluate(node.left as jsep.CoreExpression, x), evaluate(node.right as jsep.CoreExpression, x));
      break;
    }
    case 'CallExpression': {
      if (node.callee.type !== 'Identifier' || node.arguments.length !== 1) break;
      const fn = functions.get(String(node.callee.name));
      if (fn) return fn(evaluate(node.arguments[0] as jsep.CoreExpression, x));
      break;
    }
  }
  throw new Error('Unsupported chart formula');
}

export function parseChart(source: string): ChartSpec | undefined {
  if (source.length > 100_000) return;
  try {
    const { series, x, ...chart } = spec.parse(JSON.parse(source));
    if (new Set<number | string>(x).size !== x.length || x.some(v => typeof v === 'number' && !Number.isFinite(v))) return;
    if (!isPartToWhole(chart.type) && x.length < 2) return;
    if (isPartToWhole(chart.type)) {
      const values = series[0]?.values;
      if (series.length !== 1 || !values || values.some(v => v < 0)) return;
      const total = values.reduce((sum, v) => sum + v, 0);
      if (!(total > 0 && Number.isFinite(total))) return;
    }
    if (chart.type === 'mixed') {
      const kinds = new Set(series.map(s => s.type));
      if (kinds.has(undefined) || !kinds.has('bar') || !kinds.has('line')) return;
    }
    if ((chart.type === 'line' || chart.type === 'mixed') && x.some((v, i) => typeof v === 'number' && i > 0 && v <= Number(x[i - 1]))) return;
    for (const side of ['left', 'right']) {
      if (new Set(series.filter(s => s.axis === side).map(s => s.unit)).size > 1) return;
    }
    const computed: Series[] = [];
    for (const { formula, values, ...s } of series) {
      let points = values;
      if (formula) {
        if (values || typeof x[0] !== 'number') return;
        const tree = jsep(formula) as jsep.CoreExpression;
        points = x.map(v => evaluate(tree, Number(v)));
      }
      if (!points || points.length !== x.length || !points.every(Number.isFinite)) return;
      computed.push({ ...s, values: points });
    }
    return { ...chart, x, series: computed };
  } catch { /* Preserve the ordinary code block if the input cannot be rendered. */ }
}
