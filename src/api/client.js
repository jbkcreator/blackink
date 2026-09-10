const TOKEN_KEY = 'bi_admin_token';

export const auth = {
  getToken: () => localStorage.getItem(TOKEN_KEY),
  setToken: (t) => localStorage.setItem(TOKEN_KEY, t),
  clearToken: () => localStorage.removeItem(TOKEN_KEY),
};

async function apiFetch(path, opts = {}) {
  const res = await fetch(path, opts);
  if (res.status === 401) {
    auth.clearToken();
    window.location.href = '/login';
    throw new Error('Unauthorized');
  }
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

function adminHeaders(extra = {}) {
  const token = auth.getToken();
  return token ? { Authorization: `Bearer ${token}`, ...extra } : extra;
}

export const api = {
  login: (username, password) =>
    apiFetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }),

  // Returns { summary: { total_companies, total_doors, total_meetings }, companies: [...] }
  sandboxCompanies: () => apiFetch('/api/sandbox/companies', { headers: adminHeaders() }),
  metricsDigest: () => apiFetch('/api/metrics/digest', { headers: adminHeaders() }),
  meetingOutcomes: () => apiFetch('/api/meetings/outcomes', { headers: adminHeaders() }),

  // Public — no auth
  requestOvsAudit: (body) => apiFetch('/api/ovs/request', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
};
