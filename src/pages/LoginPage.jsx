import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, auth } from '../api/client.js';

export default function LoginPage() {
  const navigate = useNavigate();
  const [form, setForm] = useState({ username: '', password: '' });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const res = await api.login(form.username, form.password);
      auth.setToken(res.access_token);
      navigate('/sandbox', { replace: true });
    } catch (err) {
      setError('Invalid username or password.');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4"
      style={{ background: '#070b14' }}>
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <span className="text-2xl font-bold"
            style={{ background: 'linear-gradient(90deg,#fbbf24,#a855f7)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
            Blackink
          </span>
          <p className="text-white/40 text-sm mt-2">Internal dashboard</p>
        </div>

        <form onSubmit={handleSubmit} className="glass-card p-8 flex flex-col gap-5">
          <div>
            <label className="block text-xs font-medium text-white/50 uppercase tracking-widest mb-1.5">
              Username
            </label>
            <input
              type="text"
              required
              autoComplete="username"
              value={form.username}
              onChange={e => setForm(f => ({ ...f, username: e.target.value }))}
              className="w-full rounded-lg px-4 py-2.5 text-sm text-white outline-none transition-colors"
              style={{
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid rgba(255,255,255,0.1)',
              }}
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-white/50 uppercase tracking-widest mb-1.5">
              Password
            </label>
            <input
              type="password"
              required
              autoComplete="current-password"
              value={form.password}
              onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
              className="w-full rounded-lg px-4 py-2.5 text-sm text-white outline-none transition-colors"
              style={{
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid rgba(255,255,255,0.1)',
              }}
            />
          </div>

          {error && (
            <p className="text-xs text-center" style={{ color: '#f87171' }}>{error}</p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="btn-primary w-full py-2.5 text-sm font-semibold rounded-lg mt-1"
            style={{ background: 'linear-gradient(135deg,#fbbf24,#f59e0b)', color: '#0f172a', opacity: loading ? 0.6 : 1 }}>
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}
