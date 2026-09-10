import { useState, useMemo, useEffect } from 'react';
import { api } from '../api/client';

const COUNTY_LABELS = {
  hillsborough_fl: 'Hillsborough',
  pinellas_fl: 'Pinellas',
};

const STATUS_BADGE = {
  meeting_booked: <span className="badge badge-ok">Meeting Booked</span>,
  nurturing: <span className="badge badge-accent">Nurturing</span>,
  prospecting: <span className="badge badge-neutral">Prospecting</span>,
};

function statusOf(row) {
  if (row.meetings_booked > 0) return 'meeting_booked';
  if (row.touches_sent >= 4) return 'nurturing';
  return 'prospecting';
}

export default function SandboxDashboardPage() {
  const [summary, setSummary] = useState(null);
  const [companies, setCompanies] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [countyFilter, setCountyFilter] = useState('all');
  const [softwareFilter, setSoftwareFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const PER_PAGE = 15;

  useEffect(() => {
    api.sandboxCompanies()
      .then(({ summary, companies }) => {
        setSummary(summary);
        setCompanies(companies);
        setLoading(false);
      })
      .catch(err => { setError(err.message); setLoading(false); });
  }, []);

  const softwares = useMemo(
    () => [...new Set(companies.map(c => c.current_pm_software).filter(Boolean))].sort(),
    [companies]
  );

  // Filtering is still client-side (data already in memory) — no re-fetch needed
  const filtered = useMemo(() => {
    return companies.filter(c => {
      if (countyFilter !== 'all' && c.county_slug !== countyFilter) return false;
      if (softwareFilter !== 'all' && c.current_pm_software !== softwareFilter) return false;
      if (search && !c.company_name?.toLowerCase().includes(search.toLowerCase())) return false;
      return true;
    });
  }, [companies, countyFilter, softwareFilter, search]);

  const pageCount = Math.ceil(filtered.length / PER_PAGE);
  const rows = filtered.slice((page - 1) * PER_PAGE, page * PER_PAGE);

  // Summary tiles use pre-computed backend values when no filter is active,
  // otherwise recompute over the filtered slice (client-side, already in memory)
  const isFiltered = countyFilter !== 'all' || softwareFilter !== 'all' || search;
  const displaySummary = isFiltered
    ? {
        total_companies: filtered.length,
        total_doors: filtered.reduce((s, c) => s + (c.door_count_est || 0), 0),
        total_meetings: filtered.filter(c => c.meetings_booked > 0).length,
      }
    : summary;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold text-[var(--bi-text-primary)]">Demo Sandbox</h1>
        <p className="text-sm text-[var(--bi-text-muted)] mt-0.5">
          Hillsborough + Pinellas · {loading ? '…' : summary?.total_companies} seeded prospects
          {!loading && <span className="ml-2 text-[var(--bi-text-dimmed)] text-xs">(cached 5 min)</span>}
        </p>
      </div>

      {error && (
        <div className="glass p-4 rounded-[var(--bi-radius-lg)] border border-[var(--bi-status-danger)]/30 text-sm text-[var(--bi-status-danger)]">
          Could not load sandbox data — {error}
        </div>
      )}

      {/* Summary tiles — sourced from backend SQL aggregates */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        {[
          { label: 'Companies', value: loading ? '…' : displaySummary?.total_companies },
          { label: 'Total Doors', value: loading ? '…' : displaySummary?.total_doors?.toLocaleString() },
          { label: 'Meetings', value: loading ? '…' : displaySummary?.total_meetings, accent: true },
          { label: 'Showing', value: loading ? '…' : `${rows.length} / ${filtered.length}` },
        ].map(({ label, value, accent }) => (
          <div key={label} className="glass-card stat-card p-4">
            <p className="text-xs text-[var(--bi-text-muted)] uppercase tracking-widest mb-1">{label}</p>
            <p className={`text-2xl font-bold font-variant-numeric ${accent ? 'gradient-text' : 'text-[var(--bi-text-primary)]'}`}>
              {value}
            </p>
          </div>
        ))}
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3">
        <input
          type="text"
          placeholder="Search company…"
          value={search}
          onChange={e => { setSearch(e.target.value); setPage(1); }}
          className="h-8 px-3 text-sm rounded-[var(--bi-radius-md)] bg-[var(--bi-bg-surface)] border border-[var(--bi-border-default)] text-[var(--bi-text-primary)] placeholder-[var(--bi-text-dimmed)] focus:outline-none focus:border-[var(--bi-color-primary)] w-48"
        />
        <select
          value={countyFilter}
          onChange={e => { setCountyFilter(e.target.value); setPage(1); }}
          className="h-8 px-3 text-sm rounded-[var(--bi-radius-md)] bg-[var(--bi-bg-surface)] border border-[var(--bi-border-default)] text-[var(--bi-text-secondary)] focus:outline-none focus:border-[var(--bi-color-primary)]"
        >
          <option value="all">All Counties</option>
          <option value="hillsborough_fl">Hillsborough</option>
          <option value="pinellas_fl">Pinellas</option>
        </select>
        <select
          value={softwareFilter}
          onChange={e => { setSoftwareFilter(e.target.value); setPage(1); }}
          className="h-8 px-3 text-sm rounded-[var(--bi-radius-md)] bg-[var(--bi-bg-surface)] border border-[var(--bi-border-default)] text-[var(--bi-text-secondary)] focus:outline-none focus:border-[var(--bi-color-primary)]"
        >
          <option value="all">All Software</option>
          {softwares.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
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
                <th>County</th>
                <th>Software</th>
                <th className="text-right">Doors</th>
                <th className="text-right">Touches</th>
                <th className="text-right">Meetings</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(c => (
                <tr key={c.company_id}>
                  <td className="font-medium text-[var(--bi-text-primary)]">{c.company_name}</td>
                  <td>{COUNTY_LABELS[c.county_slug] ?? c.county_slug}</td>
                  <td>{c.current_pm_software ?? '—'}</td>
                  <td className="text-right font-variant-numeric">{c.door_count_est ?? '—'}</td>
                  <td className="text-right font-variant-numeric">{c.touches_sent ?? 0}</td>
                  <td className="text-right font-variant-numeric">{c.meetings_booked ?? 0}</td>
                  <td>{STATUS_BADGE[statusOf(c)]}</td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={7} className="text-center py-10 text-[var(--bi-text-dimmed)]">
                    No companies match your filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination */}
      {pageCount > 1 && (
        <div className="flex items-center justify-between text-sm">
          <span className="text-[var(--bi-text-muted)]">Page {page} of {pageCount}</span>
          <div className="flex gap-2">
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page === 1}
              className="px-3 py-1.5 rounded-[var(--bi-radius-sm)] border border-[var(--bi-border-default)] text-[var(--bi-text-secondary)] hover:border-[var(--bi-border-emphasis)] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              Prev
            </button>
            <button
              onClick={() => setPage(p => Math.min(pageCount, p + 1))}
              disabled={page === pageCount}
              className="px-3 py-1.5 rounded-[var(--bi-radius-sm)] border border-[var(--bi-border-default)] text-[var(--bi-text-secondary)] hover:border-[var(--bi-border-emphasis)] disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
