import { useState, useEffect } from 'react';
import { api } from '../api/client';

const ATTENDANCE_BADGE = {
  Held: <span className="badge badge-ok">Held</span>,
  'No-Show': <span className="badge badge-warn">No-Show</span>,
  Rescheduled: <span className="badge badge-accent">Rescheduled</span>,
};

function formatDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-US', {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true,
  });
}

function DetailPanel({ outcome, onClose }) {
  if (!outcome) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        className="glass-strong relative w-full max-w-lg rounded-[var(--bi-radius-xl)] p-6 space-y-5"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-base font-bold text-[var(--bi-text-primary)]">{outcome.company_name}</h2>
            <p className="text-sm text-[var(--bi-text-muted)]">{outcome.contact_name}</p>
          </div>
          <button onClick={onClose} className="text-[var(--bi-text-muted)] hover:text-[var(--bi-text-primary)] transition-colors">✕</button>
        </div>

        <div className="grid grid-cols-2 gap-3 text-sm">
          {[
            ['Date', formatDate(outcome.meeting_occurred_at)],
            ['Status', ATTENDANCE_BADGE[outcome.attendance_status]],
            ['Software', outcome.pm_software ?? '—'],
            ['Doors', outcome.door_count_est ?? '—'],
            ['Client', outcome.client_id],
            ['Recorded by', outcome.recorded_by],
          ].map(([label, value]) => (
            <div key={label}>
              <p className="text-xs text-[var(--bi-text-dimmed)] mb-0.5">{label}</p>
              <div className="text-[var(--bi-text-secondary)]">{value}</div>
            </div>
          ))}
        </div>

        {outcome.objections?.length > 0 && (
          <div>
            <p className="text-xs text-[var(--bi-text-dimmed)] mb-2">Objections</p>
            <div className="flex flex-wrap gap-1.5">
              {outcome.objections.map(o => (
                <span key={o} className="badge badge-warn">{o}</span>
              ))}
            </div>
          </div>
        )}

        <div>
          <p className="text-xs text-[var(--bi-text-dimmed)] mb-1">Next Action</p>
          <p className="text-sm text-[var(--bi-text-secondary)]">{outcome.next_action || '—'}</p>
        </div>
      </div>
    </div>
  );
}

export default function MeetingOutcomesPage() {
  const [outcomes, setOutcomes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('all');
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    api.meetingOutcomes()
      .then(data => { setOutcomes(data); setLoading(false); })
      .catch(err => { setError(err.message); setLoading(false); });
  }, []);

  const filtered = outcomes.filter(o =>
    filter === 'all' || o.attendance_status === filter
  );

  const held = outcomes.filter(o => o.attendance_status === 'Held').length;
  const noShow = outcomes.filter(o => o.attendance_status === 'No-Show').length;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold text-[var(--bi-text-primary)]">Meeting Outcomes</h1>
        <p className="text-sm text-[var(--bi-text-muted)] mt-0.5">
          Recorded via Slack modal · {loading ? '…' : outcomes.length} total
        </p>
      </div>

      {error && (
        <div className="glass p-4 rounded-[var(--bi-radius-lg)] border border-[var(--bi-status-danger)]/30 text-sm text-[var(--bi-status-danger)]">
          Could not load outcomes — {error}
        </div>
      )}

      {/* Quick stats */}
      <div className="flex flex-wrap gap-3">
        <div className="glass px-4 py-2 rounded-[var(--bi-radius-md)] flex items-center gap-2">
          <span className="text-sm text-[var(--bi-text-muted)]">Held</span>
          <span className="font-bold text-[var(--bi-status-ok)]">{loading ? '…' : held}</span>
        </div>
        <div className="glass px-4 py-2 rounded-[var(--bi-radius-md)] flex items-center gap-2">
          <span className="text-sm text-[var(--bi-text-muted)]">No-Show</span>
          <span className="font-bold text-[var(--bi-status-warn)]">{loading ? '…' : noShow}</span>
        </div>
        <div className="glass px-4 py-2 rounded-[var(--bi-radius-md)] flex items-center gap-2">
          <span className="text-sm text-[var(--bi-text-muted)]">Show Rate</span>
          <span className="font-bold gradient-text">
            {loading ? '…' : outcomes.length > 0 ? `${Math.round((held / outcomes.length) * 100)}%` : 'n/a'}
          </span>
        </div>
      </div>

      {/* Filter tabs */}
      <div className="flex gap-1 bg-[var(--bi-bg-surface)] p-1 rounded-[var(--bi-radius-md)] w-fit border border-[var(--bi-border-default)]">
        {['all', 'Held', 'No-Show', 'Rescheduled'].map(f => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-3.5 py-1.5 text-sm font-medium rounded-[var(--bi-radius-sm)] transition-colors ${
              filter === f
                ? 'bg-[var(--bi-bg-surface-light)] text-[var(--bi-text-primary)]'
                : 'text-[var(--bi-text-muted)] hover:text-[var(--bi-text-secondary)]'
            }`}
          >
            {f === 'all' ? 'All' : f}
          </button>
        ))}
      </div>

      {/* Table */}
      <div className="glass-card overflow-x-auto">
        {loading ? (
          <div className="py-16 text-center text-[var(--bi-text-dimmed)] text-sm">Loading…</div>
        ) : (
          <table className="bi-table">
            <thead>
              <tr>
                <th>Company</th>
                <th>Contact</th>
                <th>Date</th>
                <th>Status</th>
                <th>Software</th>
                <th className="text-right">Doors</th>
                <th>Objections</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(o => (
                <tr key={o.id}>
                  <td className="font-medium text-[var(--bi-text-primary)] max-w-[200px] truncate">{o.company_name}</td>
                  <td>{o.contact_name}</td>
                  <td className="whitespace-nowrap">{formatDate(o.meeting_occurred_at)}</td>
                  <td>{ATTENDANCE_BADGE[o.attendance_status] ?? <span className="badge badge-neutral">{o.attendance_status}</span>}</td>
                  <td>{o.pm_software ?? '—'}</td>
                  <td className="text-right font-variant-numeric">{o.door_count_est ?? '—'}</td>
                  <td>
                    {o.objections?.length > 0 ? (
                      <div className="flex flex-wrap gap-1">
                        {o.objections.slice(0, 2).map(obj => (
                          <span key={obj} className="badge badge-warn">{obj}</span>
                        ))}
                        {o.objections.length > 2 && (
                          <span className="badge badge-neutral">+{o.objections.length - 2}</span>
                        )}
                      </div>
                    ) : (
                      <span className="text-[var(--bi-text-dimmed)]">—</span>
                    )}
                  </td>
                  <td>
                    <button
                      onClick={() => setSelected(o)}
                      className="text-xs text-[var(--bi-color-primary)] hover:text-[var(--bi-color-primary-hover)] font-medium transition-colors"
                    >
                      View
                    </button>
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={8} className="text-center py-10 text-[var(--bi-text-dimmed)]">
                    {outcomes.length === 0 ? 'No meeting outcomes recorded yet.' : 'No outcomes for this filter.'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      <DetailPanel outcome={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
