import { useEffect, useRef, useState } from 'react';
import { Chart, ArcElement, DoughnutController, PieController, BarController, BarElement, CategoryScale, LineController, LineElement,
  LinearScale, PointElement, Tooltip, type ChartOptions } from 'chart.js';
import { useCachedT } from '../i18n';
import { isPartToWhole, type ChartSpec } from './chartSpec';
import './inline-chart.css';

Chart.register(ArcElement, PieController, DoughnutController, BarController, BarElement, CategoryScale, LineController, LineElement, LinearScale, PointElement, Tooltip);
const formatTick = (v: number) => Number(v.toPrecision(5)).toString();

function formatSample(s: ChartSpec['series'][number], index: number) {
  return String(s.values[index]!) + s.unit;
}

const cssVariable = (name: string) => 'var(' + name + ')';

function swatch(index: number, token: (name: string) => string): string {
  if (index < 6) return token('--series-' + index);
  return `hsl(${Math.round(index * 137.508) % 360} 48% ${token('--slice-lightness')})`;
}

function shareText(spec: ChartSpec, index: number, total: number): string {
  const series = spec.series[0]!;
  const percent = Number((series.values[index]! / total * 100).toFixed(1));
  return formatSample(series, index) + ' (' + percent + '%)';
}

function cartesianChart(el: HTMLCanvasElement, spec: ChartSpec, colors: string[], css: CSSStyleDeclaration) {
  const ink = css.getPropertyValue('--ink-2').trim() || '#625f59';
  const grid = css.getPropertyValue('--hairline').trim() || '#e8e7e4';
  const { series, x } = spec;
  const mixed = spec.type === 'mixed';
  const numeric = (spec.type === 'line' || mixed) && typeof x[0] === 'number';
  const scales: NonNullable<ChartOptions<'line' | 'bar'>['scales']> = {
    x: { type: numeric ? 'linear' : 'category', offset: mixed || spec.type === 'bar', title: { display: !!spec.xLabel, text: spec.xLabel, color: ink }, grid: { color: grid },
      ticks: { color: ink, maxTicksLimit: 9, maxRotation: 0, autoSkip: true } },
  };
  for (const axis of ['left', 'right'] as const) {
    const first = series.findIndex(s => s.axis === axis);
    if (first < 0) continue;
    const unit = series[first]!.unit;
    const values = series.filter(s => s.axis === axis).flatMap(s => s.values);
    const min = Math.min(...values), max = Math.max(...values);
    const symmetric = spec.type === 'line' && min < 0 && max > 0 && Math.abs(max + min) < max * .02;
    const limit = Number((Math.max(-min, max) * 1.4).toPrecision(2));
    scales[axis] = { type: 'linear', position: axis, beginAtZero: spec.type === 'bar' || (mixed && series.some(s => s.axis === axis && s.type === 'bar')),
      min: symmetric ? -limit : undefined, max: symmetric ? limit : undefined,
      ticks: { color: axis === 'right' ? colors[first] : ink, maxTicksLimit: 6, callback: v => formatTick(Number(v)) + unit },
      grid: { color: grid, drawOnChartArea: axis === 'left' || series.every(s => s.axis === 'right') } };
  }
  return new Chart(el, {
    type: spec.type === 'line' ? 'line' : 'bar',
    data: { labels: x, datasets: series.map((s, i) => ({
      type: mixed ? s.type : spec.type === 'line' ? 'line' : 'bar',
      order: mixed && s.type === 'bar' ? 1 : 0,
      label: s.name, yAxisID: s.axis, borderColor: colors[i], backgroundColor: colors[i],
      borderWidth: 2, borderDash: s.axis === 'right' ? [6, 4] : [], pointRadius: 0, pointHoverRadius: 4,
      data: numeric ? s.values.map((y, j) => ({ x: Number(x[j]), y })) : s.values,
    })) },
    options: { responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false }, scales,
      plugins: { tooltip: { callbacks: {
        title: items => String(x[items[0]!.dataIndex]),
        label: item => item.dataset.label + '  ' + formatSample(series[item.datasetIndex]!, item.dataIndex),
      } } },
    },
  });
}

function shareChart(el: HTMLCanvasElement, spec: ChartSpec, total: number, totalLabel: string, colors: string[], surface: string) {
  const series = spec.series[0]!;
  return new Chart(el, {
    type: spec.type === 'pie' ? 'pie' : 'doughnut',
    plugins: spec.type === 'doughnut' ? [{
      id: 'total',
      afterDatasetsDraw(chart) {
        const arc = chart.getDatasetMeta(0).data[0];
        if (!(arc instanceof ArcElement) || !arc.innerRadius) return;
        const ctx = chart.ctx;
        const css = getComputedStyle(el);
        const width = arc.innerRadius * 1.7;
        ctx.save();
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = css.color;
        ctx.font = '12px ' + css.fontFamily;
        ctx.fillText(totalLabel, arc.x, arc.y - 10, width);
        ctx.font = '600 17px ' + css.fontFamily;
        ctx.fillText(String(total) + series.unit, arc.x, arc.y + 10, width);
        ctx.restore();
      },
    }] : [],
    data: { labels: spec.x, datasets: [{ label: series.name, data: series.values, backgroundColor: colors, borderColor: surface, borderWidth: 2 }] },
    options: { responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { tooltip: { callbacks: {
        title: items => String(spec.x[items[0]!.dataIndex]),
        label: item => series.name + '  ' + shareText(spec, item.dataIndex, total),
      } } },
    },
  });
}

export default function InlineChart({ spec, source }: { spec: ChartSpec; source: string }) {
  const { t } = useCachedT();
  const canvas = useRef<HTMLCanvasElement>(null);
  const [code, setCode] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const { x, series } = spec;
  const radial = isPartToWhole(spec.type);
  const total = radial ? series[0]!.values.reduce((sum, v) => sum + v, 0) : 0;
  const totalLabel = t('chart.total');
  const axisName = (axis: string) => t(axis === 'right' ? 'chart.right' : 'chart.left');

  useEffect(() => {
    const el = canvas.current;
    if (!el || code) return;
    const draw = () => {
      const css = getComputedStyle(el);
      const colors = (radial ? x : series).map((_, i) => swatch(i, name => css.getPropertyValue(name).trim()));
      const surface = css.getPropertyValue('--surface').trim() || '#fff';
      return radial ? shareChart(el, spec, total, totalLabel, colors, surface) : cartesianChart(el, spec, colors, css);
    };
    let chart = draw();
    const observer = new MutationObserver(() => {
      chart.destroy();
      chart = draw();
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => { observer.disconnect(); chart.destroy(); };
  }, [spec, x, series, code, radial, total, totalLabel]);

  const legend = radial ? x.map(String) : series.map(s =>
    s.name + (s.unit || series.some(item => item.axis === 'right')
      ? ' (' + [axisName(s.axis), s.unit].filter(Boolean).join(', ') + ')' : ''));
  return <figure className="inline-chart" aria-label={spec.title}>
    <header>
      <div className="chart-legend">{legend.map((label, i) => <span key={i}>
        <svg width="14" height="10" aria-hidden="true">
          {spec.type === 'mixed' && series[i]?.type === 'line'
            ? <path d="M0 5h14" stroke={swatch(i, cssVariable)} strokeWidth="2" strokeDasharray={series[i]?.axis === 'right' ? '4 2' : undefined} />
            : <rect x="2" width="10" height="10" rx="2" fill={swatch(i, cssVariable)} />}
        </svg>{label}
      </span>)}</div>
      <button type="button" aria-pressed={code} onClick={() => setCode(v => !v)}>
        {t(code ? 'chart.show' : 'chart.source')}
      </button>
    </header>
    {code ? <pre tabIndex={0}><code>{source}</code></pre> : <>
      <div className="chart-plot">
        <canvas ref={canvas} role="img" aria-label={spec.title} tabIndex={0}
          onBlur={() => setSelected(null)} onKeyDown={ev => {
            if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
            ev.preventDefault();
            setSelected(i => Math.max(0, Math.min(x.length - 1, (i ?? -1) + (ev.key === 'ArrowRight' ? 1 : -1))));
          }} />
      </div>
      {selected !== null && <output aria-live="polite">{x[selected]} · {radial ? shareText(spec, selected, total) : series.map(s =>
        s.name + ' ' + formatSample(s, selected)).join(' · ')}</output>}
    </>}
  </figure>;
}
