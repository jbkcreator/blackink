import { useState, useEffect } from 'react';
import { api } from '../api/client';

const METRIC_DEFS = [
  {
    key: 'scores_generated',
    label: 'OVS Scores Generated',
    unit: '',
    desc: 'Owner visibility scores computed (last 24h)',
  },
  {
    key: 'county_rank_reports_delivered',
    label: 'County Rank Reports',
    unit: '',
    desc: 'Distinct counties scored (last 24h)',
  },
  {
    key: 'cold_emails_dispatched',
    label: 'Cold Emails Dispatched',
    unit: '',
    desc: 'Outbound email touches sent (last 24h)',
  },
  {
    key: 'open_rate_pct',
    label: 'Open Rate',
    unit: '%',
    desc: 'Unique open rate (last 24h)',
    good: v => v >= 30,
  },
  {
    key: 'click_rate_pct',
    label: 'Click Rate',
    unit: '%',
    desc: 'Unique click-through rate (last 24h)',
    good: v => v >= 5,
  },
  {
    key: 'reply_rate_pct',
    label: 'Reply Rate',
    unit: '%',
    desc: 'Positive + neutral reply rate (last 24h)',
    good: v => v >= 2,
  },
  {
    key: 'appointments_booked',
    label: 'Appointments Booked',
    unit: '',
    desc: 'Discovery calls scheduled (last 24h)',
  },
];

function DeltaIndicator({ current, prev }) {
  if (prev == null || current == null) return null;
  const delta = current - prev;
  if (delta === 0) return null;
  const positive = delta > 0;
  return (
    <span className={`text-xs font-semibold ${positive ? 'text-[var(--bi-status-ok)]' : 'text-[var(--bi-status-danger)]'}`}>
      {positive ? '▲' : '▼'} {Math.abs(delta).toFixed(1)}
    </span>
  );
}

function MetricCard({ def, value, loading }) {
  const isRate = def.unit === '%';
  const numVal = value != null ? parseFloat(value) : null;
  const status = def.good && numVal != null ? (def.good(numVal) ? 'ok' : 'warn') : null;

  const displayValue = loading
    ? '…'
    : numVal == null
    ? 'n/a'
    : isRate
    ? numVal.toFixed(1)
    : Math.round(numVal).toLocaleString();

  return (
    <div className="glass-card stat-card p-5 flex flex-col gap-3">
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs font-semibold uppercase tracking-widest text-[var(--bi-text-muted)]">
          {def.label}
        </p>
        {status && (
          <span className={`badge ${status === 'ok' ? 'badge-ok' : 'badge-warn'}`}>
            {status === 'ok' ? 'On Track' : 'Low'}
          </span>
        )}
      </div>

      <div className="flex items-end gap-2">
        <span className={`text-3xl font-bold leading-none font-variant-numeric ${isRate ? 'gradient-text' : 'text-[var(--bi-text-primary)]'}`}>
          {displayValue}
          {!loading && numVal != null && def.unit && (
            <span className="text-xl ml-0.5">{def.unit}</span>
          )}
        </span>
      </div>

      <p className="text-xs text-[var(--bi-text-dimmed)] leading-relaxed">{def.desc}</p>
    </div>
  );
}

export default function PipelineMetricsPage() {
  const [metrics, setMetrics] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.metricsDigest()
      .then(data => { setMetrics(data); setLoading(false); })
      .catch(err => { setError(err.message); setLoading(false); });
  }, []);

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-bold text-[var(--bi-text-primary)]">Pipeline Metrics</h1>
          <p className="text-sm text-[var(--bi-text-muted)] mt-0.5">24-hour rolling KPIs · all clients</p>
        </div>
        <button
          onClick={() => { setLoading(true); setError(null); api.metricsDigest().then(d => { setMetrics(d); setLoading(false); }).catch(e => { setError(e.message); setLoading(false); }); }}
          className="text-xs text-[var(--bi-text-muted)] hover:text-[var(--bi-text-secondary)] transition-colors"
        >
          ↻ Refresh
        </button>
      </div>

      {error && (
        <div className="glass p-4 rounded-[var(--bi-radius-lg)] border border-[var(--bi-status-danger)]/30 text-sm text-[var(--bi-status-danger)]">
          Could not load metrics — {error}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
        {METRIC_DEFS.map(def => (
          <MetricCard
            key={def.key}
            def={def}
            value={metrics?.[def.key]}
            loading={loading}
          />
        ))}
      </div>

      <div className="glass p-4 rounded-[var(--bi-radius-lg)] flex flex-wrap gap-6 text-sm">
        <p className="text-[var(--bi-text-muted)]">
          <span className="text-[var(--bi-text-secondary)] font-medium">Benchmarks</span>
          &ensp;Open ≥ 30% · Click ≥ 5% · Reply ≥ 2%
        </p>
        <p className="text-[var(--bi-text-dimmed)]">
          Source: <code className="text-[10px] font-mono bg-[var(--bi-bg-surface)] px-1 py-0.5 rounded">events</code> table via <code className="text-[10px] font-mono bg-[var(--bi-bg-surface)] px-1 py-0.5 rounded">/api/metrics/digest</code>
        </p>
      </div>
    </div>
  );
}
