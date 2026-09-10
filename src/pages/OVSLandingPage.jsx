import { useState, useRef } from 'react';
import { api } from '../api/client';

const CATEGORIES = [
  {
    icon: '🌐',
    name: 'Website Presence',
    pts: 25,
    desc: 'Speed, mobile-readiness, trust signals, and lead-capture quality of your primary web property.',
  },
  {
    icon: '⭐',
    name: 'Google Profile',
    pts: 25,
    desc: 'Completeness, review count, average rating, recency of responses on your Google Business Profile.',
  },
  {
    icon: '📋',
    name: 'License Compliance',
    pts: 20,
    desc: 'Active DBPR licensure, license-holder visibility, and disciplinary history via public state rolls.',
  },
  {
    icon: '⚡',
    name: 'Response Speed',
    pts: 15,
    desc: 'Estimated owner-inquiry response time benchmarked against county peers using public signals.',
  },
  {
    icon: '🏆',
    name: 'Reputation Reach',
    pts: 15,
    desc: 'Cross-platform review presence, social signals, and press/directory citations.',
  },
];

const STEPS = [
  { n: '01', title: 'Enter your domain', desc: 'We only need your company\'s web address to get started. No login required.' },
  { n: '02', title: 'We score automatically', desc: 'Our engine queries public sources — websites, Google, state rolls — and computes your score within 24 hours.' },
  { n: '03', title: 'Receive your report', desc: 'A branded 2-page PDF lands in your inbox: score, county rank, lowest-scoring observations, and lost-revenue estimate.' },
];

const US_STATES = [
  'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA',
  'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
  'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT',
  'VA','WA','WV','WI','WY',
];

function ScoreRing({ score = 72 }) {
  const r = 54;
  const circ = 2 * Math.PI * r;
  const fill = (score / 100) * circ;
  return (
    <svg width="140" height="140" viewBox="0 0 140 140" className="mx-auto">
      <circle cx="70" cy="70" r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth="10" />
      <circle
        cx="70" cy="70" r={r} fill="none"
        stroke="url(#scoreGrad)" strokeWidth="10"
        strokeDasharray={`${fill} ${circ}`}
        strokeLinecap="round"
        transform="rotate(-90 70 70)"
      />
      <defs>
        <linearGradient id="scoreGrad" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stopColor="#fbbf24" />
          <stop offset="100%" stopColor="#a855f7" />
        </linearGradient>
      </defs>
      <text x="70" y="65" textAnchor="middle" fill="#fff" fontSize="28" fontWeight="700" fontFamily="Inter,sans-serif">{score}</text>
      <text x="70" y="84" textAnchor="middle" fill="rgba(255,255,255,0.45)" fontSize="11" fontFamily="Inter,sans-serif">out of 100</text>
    </svg>
  );
}

export default function OVSLandingPage() {
  const formRef = useRef(null);
  const [form, setForm] = useState({ company_name: '', domain: '', contact_name: '', email: '', state: 'FL' });
  const [status, setStatus] = useState('idle'); // idle | loading | success | error
  const [errorMsg, setErrorMsg] = useState('');

  function scrollToForm(e) {
    e.preventDefault();
    formRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function set(field) {
    return e => setForm(f => ({ ...f, [field]: e.target.value }));
  }

  async function handleSubmit(e) {
    e.preventDefault();
    if (!form.company_name || !form.domain || !form.email) return;
    setStatus('loading');
    try {
      await api.requestOvsAudit(form);
      setStatus('success');
    } catch (err) {
      setErrorMsg(err.message);
      setStatus('error');
    }
  }

  const inputCls = 'w-full h-11 px-4 text-sm rounded-[var(--bi-radius-md)] bg-white/5 border border-white/10 text-white placeholder-white/30 focus:outline-none focus:border-[#fbbf24] transition-colors';
  const labelCls = 'block text-xs font-medium text-white/50 uppercase tracking-widest mb-1.5';

  return (
    <div className="min-h-screen" style={{ background: '#070b14', fontFamily: 'Inter, sans-serif' }}>

      {/* ── Nav ── */}
      <nav className="sticky top-0 z-50 flex items-center justify-between px-6 h-14"
        style={{ background: 'rgba(7,11,20,0.85)', backdropFilter: 'blur(12px)', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
        <span className="text-lg font-bold" style={{ background: 'linear-gradient(90deg,#fbbf24,#a855f7)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
          Blackink
        </span>
        <div className="hidden sm:flex items-center gap-6">
          <a href="#how-it-works" className="text-sm text-white/50 hover:text-white/90 transition-colors">How It Works</a>
          <a href="#what-we-measure" className="text-sm text-white/50 hover:text-white/90 transition-colors">What We Measure</a>
        </div>
        <a href="#audit-form" onClick={scrollToForm}
          className="btn-primary px-4 py-2 text-sm rounded-[var(--bi-radius-md)] font-semibold"
          style={{ background: 'linear-gradient(135deg,#fbbf24,#f59e0b)', color: '#0f172a' }}>
          Get Your Score →
        </a>
      </nav>

      {/* ── Hero ── */}
      <section className="relative overflow-hidden">
        {/* Background glow */}
        <div className="absolute inset-0 pointer-events-none">
          <div className="absolute top-0 left-1/2 -translate-x-1/2 w-[600px] h-[600px] rounded-full opacity-10"
            style={{ background: 'radial-gradient(circle, #fbbf24 0%, transparent 70%)' }} />
        </div>

        <div className="relative max-w-5xl mx-auto px-6 pt-24 pb-20 text-center">
          <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium mb-8"
            style={{ background: 'rgba(251,191,36,0.08)', border: '1px solid rgba(251,191,36,0.2)', color: '#fbbf24' }}>
            <span className="w-1.5 h-1.5 rounded-full bg-[#fbbf24] animate-pulse" />
            Free Certified Audit · No Credit Card Required
          </div>

          <h1 className="text-4xl sm:text-5xl lg:text-6xl font-bold leading-tight mb-6" style={{ color: '#fff' }}>
            How visible is your PM firm<br />
            <span style={{ background: 'linear-gradient(90deg,#fbbf24,#a855f7)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
              to prospective owners?
            </span>
          </h1>

          <p className="text-lg text-white/55 max-w-xl mx-auto mb-10 leading-relaxed">
            Your Owner Visibility Score (OVS) is a 100-point certified audit of your digital presence,
            benchmarked against every property management firm in your county.
            We score you in under 24 hours. Completely free.
          </p>

          <div className="flex flex-wrap justify-center gap-3 mb-12">
            {CATEGORIES.map(c => (
              <span key={c.name} className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium"
                style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', color: 'rgba(255,255,255,0.65)' }}>
                {c.icon} {c.name} <span style={{ color: '#fbbf24' }}>{c.pts}pts</span>
              </span>
            ))}
          </div>

          <a href="#audit-form" onClick={scrollToForm}
            className="inline-block btn-primary px-8 py-3.5 text-base rounded-[var(--bi-radius-lg)] font-bold"
            style={{ background: 'linear-gradient(135deg,#fbbf24,#f59e0b)', color: '#0f172a' }}>
            Request My Free OVS Report
          </a>
          <p className="text-xs text-white/25 mt-4">Takes 30 seconds · Delivered within 24 hours</p>
        </div>
      </section>

      {/* ── How it works ── */}
      <section id="how-it-works" className="max-w-5xl mx-auto px-6 py-20">
        <div className="text-center mb-14">
          <h2 className="text-2xl font-bold text-white mb-3">How it works</h2>
          <p className="text-white/40 text-sm">No logins. No installs. Just a domain name.</p>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-6">
          {STEPS.map(step => (
            <div key={step.n} className="glass-card p-6">
              <div className="text-3xl font-bold mb-4"
                style={{ background: 'linear-gradient(90deg,#fbbf24,#a855f7)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
                {step.n}
              </div>
              <h3 className="text-base font-semibold text-white mb-2">{step.title}</h3>
              <p className="text-sm text-white/45 leading-relaxed">{step.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* ── What we measure ── */}
      <section id="what-we-measure" className="max-w-5xl mx-auto px-6 py-12">
        <div className="text-center mb-14">
          <h2 className="text-2xl font-bold text-white mb-3">What we measure</h2>
          <p className="text-white/40 text-sm">Public data only. No pretext. No vendor access required.</p>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {CATEGORIES.map(cat => (
            <div key={cat.name} className="glass-card p-5 flex gap-4">
              <div className="text-2xl mt-0.5 shrink-0">{cat.icon}</div>
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-semibold text-white">{cat.name}</span>
                  <span className="text-xs font-bold px-2 py-0.5 rounded-full"
                    style={{ background: 'rgba(251,191,36,0.1)', color: '#fbbf24', border: '1px solid rgba(251,191,36,0.2)' }}>
                    {cat.pts} pts
                  </span>
                </div>
                <p className="text-xs text-white/40 leading-relaxed">{cat.desc}</p>
              </div>
            </div>
          ))}
          {/* Filler card for grid alignment */}
          <div className="glass-card p-5 flex gap-4 sm:col-span-2 lg:col-span-1">
            <div className="text-2xl mt-0.5 shrink-0">📊</div>
            <div>
              <div className="flex items-center gap-2 mb-1">
                <span className="text-sm font-semibold text-white">County Rank</span>
                <span className="text-xs font-bold px-2 py-0.5 rounded-full"
                  style={{ background: 'rgba(168,85,247,0.1)', color: '#c084fc', border: '1px solid rgba(168,85,247,0.2)' }}>
                  Bonus
                </span>
              </div>
              <p className="text-xs text-white/40 leading-relaxed">
                See how you rank against every licensed PM firm in your county. National benchmarks available from day one.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* ── Sample report preview ── */}
      <section className="max-w-5xl mx-auto px-6 py-16">
        <div className="glass-card p-8 sm:p-10"
          style={{ background: 'linear-gradient(135deg, rgba(15,23,42,0.8), rgba(7,11,20,0.95))', border: '1px solid rgba(251,191,36,0.15)' }}>
          <div className="text-center mb-8">
            <h2 className="text-2xl font-bold text-white mb-2">Sample report preview</h2>
            <p className="text-white/40 text-sm">This is what lands in your inbox</p>
          </div>
          <div className="flex flex-col sm:flex-row items-center gap-10">
            <div className="shrink-0">
              <ScoreRing score={72} />
              <p className="text-center text-xs text-white/30 mt-3">Sample score</p>
            </div>
            <div className="flex-1 space-y-4 w-full">
              <div className="flex justify-between items-center py-3" style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                <span className="text-sm text-white/50">County Rank</span>
                <span className="text-sm font-semibold text-white">#8 of 34 firms · Hillsborough, FL</span>
              </div>
              <div className="flex justify-between items-center py-3" style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                <span className="text-sm text-white/50">Lowest category</span>
                <span className="text-sm font-semibold" style={{ color: '#f87171' }}>Response Speed · 6/15 pts</span>
              </div>
              <div className="flex justify-between items-center py-3" style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                <span className="text-sm text-white/50">Estimated lost revenue</span>
                <span className="text-sm font-semibold" style={{ color: '#fbbf24' }}>$18,400 / year</span>
              </div>
              <div className="flex justify-between items-center py-3">
                <span className="text-sm text-white/50">Report format</span>
                <span className="text-sm font-semibold text-white">2-page branded PDF · emailed within 24h</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ── Form ── */}
      <section id="audit-form" ref={formRef} className="max-w-2xl mx-auto px-6 py-16">
        <div className="text-center mb-10">
          <h2 className="text-2xl font-bold text-white mb-3">Request your free OVS report</h2>
          <p className="text-white/40 text-sm">Takes 30 seconds. Delivered within 24 hours.</p>
        </div>

        {status === 'success' ? (
          <div className="glass-card p-10 text-center"
            style={{ border: '1px solid rgba(34,197,94,0.2)', background: 'rgba(34,197,94,0.04)' }}>
            <div className="text-4xl mb-4">✅</div>
            <h3 className="text-xl font-bold text-white mb-3">You're on the list.</h3>
            <p className="text-sm text-white/50 leading-relaxed">
              We're scoring <strong className="text-white">{form.company_name}</strong> now.
              Your certified OVS report will arrive at <strong className="text-white">{form.email}</strong> within 24 hours.
            </p>
            <p className="text-xs text-white/25 mt-6">
              Want to discuss your results with our team?{' '}
              <a href="https://calendly.com/heuai" className="underline text-white/40 hover:text-white/60 transition-colors">
                Book a free 20-min call →
              </a>
            </p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="glass-card p-8 space-y-5"
            style={{ border: '1px solid rgba(251,191,36,0.1)' }}>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
              <div>
                <label className={labelCls}>Company name <span style={{ color: '#fbbf24' }}>*</span></label>
                <input
                  type="text" required
                  placeholder="Suncoast Property Management"
                  value={form.company_name} onChange={set('company_name')}
                  className={inputCls}
                />
              </div>
              <div>
                <label className={labelCls}>Corporate domain <span style={{ color: '#fbbf24' }}>*</span></label>
                <input
                  type="text" required
                  placeholder="suncoastpm.com"
                  value={form.domain} onChange={set('domain')}
                  className={inputCls}
                />
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
              <div>
                <label className={labelCls}>Your name</label>
                <input
                  type="text"
                  placeholder="Jane Smith"
                  value={form.contact_name} onChange={set('contact_name')}
                  className={inputCls}
                />
              </div>
              <div>
                <label className={labelCls}>Email <span style={{ color: '#fbbf24' }}>*</span></label>
                <input
                  type="email" required
                  placeholder="jane@suncoastpm.com"
                  value={form.email} onChange={set('email')}
                  className={inputCls}
                />
              </div>
            </div>

            <div>
              <label className={labelCls}>State</label>
              <select value={form.state} onChange={set('state')}
                className={inputCls + ' cursor-pointer'} style={{ appearance: 'none' }}>
                {US_STATES.map(s => <option key={s} value={s} style={{ background: '#0f172a' }}>{s}</option>)}
              </select>
            </div>

            {status === 'error' && (
              <p className="text-sm text-red-400 px-1">{errorMsg}</p>
            )}

            <div className="pt-2">
              <button type="submit" disabled={status === 'loading'}
                className="w-full btn-primary h-12 text-base rounded-[var(--bi-radius-md)] font-bold disabled:opacity-60 disabled:cursor-not-allowed"
                style={{ background: 'linear-gradient(135deg,#fbbf24,#f59e0b)', color: '#0f172a' }}>
                {status === 'loading' ? 'Submitting…' : 'Request My Free OVS Report →'}
              </button>
            </div>

            <p className="text-center text-xs text-white/20 pt-1 leading-relaxed">
              By submitting, you consent to receiving your OVS report and related communications from Blackink.
              We never share your data. <a href="/privacy" className="underline hover:text-white/40 transition-colors">Privacy Policy</a>
              {' · '}<a href="/terms" className="underline hover:text-white/40 transition-colors">Terms</a>
            </p>
          </form>
        )}
      </section>

      {/* ── Footer ── */}
      <footer className="border-t mt-8" style={{ borderColor: 'rgba(255,255,255,0.06)' }}>
        <div className="max-w-5xl mx-auto px-6 py-10 flex flex-col sm:flex-row items-center justify-between gap-4">
          <div>
            <p className="text-sm font-semibold text-white/60">Blackink</p>
            <p className="text-xs text-white/25 mt-1">Tampa, FL</p>
          </div>
          <div className="flex gap-6 text-xs text-white/30">
            <a href="/privacy" className="hover:text-white/50 transition-colors">Privacy Policy</a>
            <a href="/terms" className="hover:text-white/50 transition-colors">Terms of Service</a>
            <a href="/unsubscribe" className="hover:text-white/50 transition-colors">Unsubscribe</a>
          </div>
        </div>
      </footer>

    </div>
  );
}
