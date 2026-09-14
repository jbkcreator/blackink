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

// Payment-auth onboarding (Subtask 1.2.1) — deliberately NOT routed through
// apiFetch()/adminHeaders() above. Those assume an admin JWT session and
// redirect to /login on any 401; this flow authenticates via a one-time,
// short-lived onboarding_token (never the admin token) carried in the page
// URL, and its own 401 means "this onboarding link expired" -- a case the
// caller must handle on-page, not a reason to bounce to the internal
// dashboard login.
async function paymentAuthFetch(path, opts = {}) {
  const res = await fetch(path, opts);
  let data = null;
  try {
    data = await res.json();
  } catch {
    // A non-JSON body (rare -- e.g. a proxy error page) still surfaces as
    // ok=false with no detail, never a thrown exception the caller has to
    // catch separately from every other failure shape.
  }
  return { ok: res.ok, status: res.status, data };
}

export const paymentAuth = {
  createSetupIntents: (onboardingToken) =>
    paymentAuthFetch('/api/v1/onboarding/payment-auth/setup-intents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ onboarding_token: onboardingToken }),
    }),

  confirm: (onboardingToken, cardSetupIntentId, achSetupIntentId) =>
    paymentAuthFetch('/api/v1/onboarding/payment-auth/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        onboarding_token: onboardingToken,
        card_setup_intent_id: cardSetupIntentId,
        ach_setup_intent_id: achSetupIntentId,
      }),
    }),

  // Authorization: Bearer only -- never a query-string token, which
  // persists in browser history / proxy / observability logs (the same
  // rule the backend's own test suite enforces server-side).
  status: (onboardingToken) =>
    paymentAuthFetch('/api/v1/onboarding/payment-auth/status', {
      headers: { Authorization: `Bearer ${onboardingToken}` },
    }),
};
