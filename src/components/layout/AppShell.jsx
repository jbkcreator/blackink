import { NavLink, Outlet } from 'react-router-dom';

const NAV = [
  { to: '/sandbox', label: 'Demo Sandbox' },
  { to: '/metrics', label: 'Pipeline Metrics' },
  { to: '/meetings', label: 'Meeting Outcomes' },
];

export default function AppShell() {
  return (
    <div
      className="min-h-screen flex flex-col"
      style={{ background: 'var(--bi-gradient-bg-dashboard)' }}
    >
      <header className="glass-strong sticky top-0 z-30 flex items-center justify-between px-6 h-14 border-b border-[var(--bi-border-default)]">
        <div className="flex items-center gap-3">
          <span className="text-base font-bold tracking-tight gradient-text">Blackink</span>
          <span className="text-[10px] font-semibold uppercase tracking-widest text-[var(--bi-text-muted)] bg-[var(--bi-bg-surface)] border border-[var(--bi-border-subtle)] px-2 py-0.5 rounded-full">
            Internal
          </span>
        </div>

        <nav className="flex items-center gap-1">
          {NAV.map(({ to, label }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `px-3.5 py-1.5 rounded-[var(--bi-radius-md)] text-sm font-medium transition-colors ${
                  isActive
                    ? 'bg-[var(--bi-bg-card-hover)] text-[var(--bi-text-primary)] border border-[var(--bi-border-emphasis)]'
                    : 'text-[var(--bi-text-muted)] hover:text-[var(--bi-text-secondary)] hover:bg-[var(--bi-bg-card)]'
                }`
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
      </header>

      <main className="flex-1 px-6 py-8 max-w-screen-xl mx-auto w-full">
        <Outlet />
      </main>
    </div>
  );
}
