import { useEffect, useRef, useState } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { loadStripe } from '@stripe/stripe-js';
import { paymentAuth } from '../api/client.js';

// Subtask 1.2.1 -- Zero-Deposit Card Auth & ACH Mandate Capture.
//
// Reached via a one-time, short-lived onboarding_token carried in the URL
// (?token=...) -- there is no client-portal login/session system anywhere
// in this repo yet, so a signed token IS the auth for this page, the same
// pattern src/api/unsubscribe_router.py's one-click links and
// src/services/calendar_oauth.py's connect links already use on the
// backend. The token is minted today only by
// scripts/dev_mint_payment_auth_token.py -- the future authenticated
// onboarding portal (Blueprint Section 3.3.1, State 2) is what is expected to
// mint/deliver this link in production; this page is built so that portal
// adopts it as its State 2, not so it gets replaced by it.
//
// No dollar amount from the pricing catalogue is shown anywhere on this
// page -- the Source-of-Truth's Week-2 pricing rule ("No dollar amount
// from the pricing catalogue may be stored in a visible field or
// displayed on any client-facing surface... applies to all screens,
// emails, and exports") is honored here even though this isn't itself an
// offer sheet. The one dollar figure disclosed is the literal $1
// authorization hold, described honestly: it is cancelled once both
// rails verify, not "immediately released" as the original blueprint
// text claims -- see the backend's own create_dollar_auth_hold()/
// cancel_auth_hold() for why.
//
// ACH uses Stripe's real two-step flow --
// stripe.collectBankAccountForSetup() (which needs billing_details.name/
// email) to attach a bank account to the ACH SetupIntent, THEN
// stripe.confirmUsBankAccountSetup() to confirm it. A single-step confirm
// has no bank account to confirm against and cannot succeed.

const STATUS_POLL_MS = 5000;

export default function PaymentAuthPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  const [token, setToken] = useState(null);
  const [holderName, setHolderName] = useState('');
  const [holderEmail, setHolderEmail] = useState('');
  const [cardMounted, setCardMounted] = useState(false);
  const [bankConnected, setBankConnected] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [connectingBank, setConnectingBank] = useState(false);
  const [message, setMessage] = useState('');
  const [messageKind, setMessageKind] = useState(''); // '' | 'error' | 'pending' | 'success'
  const [loadError, setLoadError] = useState('');

  const stripeRef = useRef(null);
  const cardElementRef = useRef(null);
  const cardClientSecretRef = useRef(null);
  const achClientSecretRef = useRef(null);
  const achSetupIntentIdRef = useRef(null);
  const cardMountRef = useRef(null);
  const pollTimerRef = useRef(null);

  // Read the token once, then scrub it out of the visible URL -- it must
  // never persist in browser history/proxy/observability logs beyond
  // this first load. Every subsequent call this page makes uses
  // Authorization: Bearer instead (see api/client.js's paymentAuth.status).
  useEffect(() => {
    const t = searchParams.get('token');
    if (t) {
      setToken(t);
      navigate(window.location.pathname, { replace: true });
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!token) return;
    let cancelled = false;

    (async () => {
      const result = await paymentAuth.createSetupIntents(token);
      if (cancelled) return;

      if (!result.ok) {
        if (result.status === 403) {
          setLoadError('Payment authorization is not currently enabled for this account. Please contact us if you believe this is an error.');
        } else if (result.status === 401) {
          setLoadError('This payment link has expired. Please request a new link.');
        } else if (result.status === 503) {
          setLoadError('Payment authorization is temporarily unavailable. Please try again shortly.');
        } else {
          setLoadError(result.data?.detail || 'Unable to start payment setup.');
        }
        return;
      }

      const { card_client_secret, ach_client_secret, publishable_key } = result.data;
      achClientSecretRef.current = ach_client_secret;

      const stripe = await loadStripe(publishable_key);
      if (cancelled) return;
      stripeRef.current = stripe;

      const elements = stripe.elements();
      const cardElement = elements.create('card', {
        style: {
          base: { fontSize: '16px', color: '#fff', '::placeholder': { color: 'rgba(255,255,255,0.3)' } },
          invalid: { color: '#f87171' },
        },
      });
      cardElement.mount(cardMountRef.current);
      cardElementRef.current = cardElement;
      cardElement.on('ready', () => setCardMounted(true));
      cardClientSecretRef.current = card_client_secret;
    })();

    return () => { cancelled = true; };
  }, [token]);

  useEffect(() => {
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    };
  }, []);

  async function handleConnectBank() {
    if (!holderName.trim() || !holderEmail.trim()) {
      setMessage('Please enter the bank account holder name and email before connecting.');
      setMessageKind('error');
      return;
    }
    setConnectingBank(true);
    setMessage('Opening secure bank connection…');
    setMessageKind('pending');

    const stripe = stripeRef.current;
    const result = await stripe.collectBankAccountForSetup({
      clientSecret: achClientSecretRef.current,
      params: {
        payment_method_type: 'us_bank_account',
        payment_method_data: {
          billing_details: { name: holderName.trim(), email: holderEmail.trim() },
        },
      },
    });

    if (result.error) {
      setMessage(result.error.message);
      setMessageKind('error');
      setConnectingBank(false);
      return;
    }

    achSetupIntentIdRef.current = result.setupIntent.id;
    setBankConnected(true);
    setMessage('');
    setMessageKind('');
    setConnectingBank(false);
  }

  async function handleSubmit() {
    if (!achSetupIntentIdRef.current) {
      setMessage('Please connect your bank account first.');
      setMessageKind('error');
      return;
    }
    setSubmitting(true);
    setMessage('Verifying card…');
    setMessageKind('pending');

    const stripe = stripeRef.current;
    const cardResult = await stripe.confirmCardSetup(cardClientSecretRef.current, {
      payment_method: { card: cardElementRef.current },
    });
    if (cardResult.error) {
      setMessage(cardResult.error.message);
      setMessageKind('error');
      setSubmitting(false);
      return;
    }

    setMessage('Verifying bank account…');
    const achResult = await stripe.confirmUsBankAccountSetup(achClientSecretRef.current);
    if (achResult.error) {
      setMessage(achResult.error.message);
      setMessageKind('error');
      setSubmitting(false);
      return;
    }

    setMessage('Confirming…');
    const confirmResult = await paymentAuth.confirm(
      token,
      cardResult.setupIntent.id,
      achResult.setupIntent.id,
    );

    if (!confirmResult.ok) {
      if (confirmResult.status === 402) {
        setMessage(confirmResult.data?.detail?.message || 'Your card was declined. Please try a different card.');
      } else {
        setMessage(confirmResult.data?.detail || 'Payment authorization failed.');
      }
      setMessageKind('error');
      setSubmitting(false);
      return;
    }

    if (confirmResult.data.status === 'ach_pending') {
      setMessage("Verifying your bank account -- this can take a moment. We'll update this page automatically.");
      setMessageKind('pending');
      startPolling();
    } else {
      setMessage('Payment authorization complete. You may close this page.');
      setMessageKind('success');
      setSubmitting(false);
    }
  }

  function startPolling() {
    if (pollTimerRef.current) return;
    pollTimerRef.current = setInterval(async () => {
      const result = await paymentAuth.status(token);
      if (result.status === 401) {
        clearInterval(pollTimerRef.current);
        setMessage('This payment link has expired. Please request a new link to check your status.');
        setMessageKind('error');
        return;
      }
      if (result.ok && result.data?.payment_auth_completed) {
        clearInterval(pollTimerRef.current);
        setMessage('Payment authorization complete. You may close this page.');
        setMessageKind('success');
      }
    }, STATUS_POLL_MS);
  }

  const inputCls = 'w-full h-11 px-4 text-sm rounded-[var(--bi-radius-md)] bg-white/5 border border-white/10 text-white placeholder-white/30 focus:outline-none focus:border-[#fbbf24] transition-colors';
  const labelCls = 'block text-xs font-medium text-white/50 uppercase tracking-widest mb-1.5';

  const messageColor = messageKind === 'error' ? '#f87171' : messageKind === 'success' ? '#4ade80' : messageKind === 'pending' ? '#fbbf24' : 'rgba(255,255,255,0.6)';

  return (
    <div className="min-h-screen flex items-center justify-center px-4 py-12" style={{ background: '#070b14', fontFamily: 'Inter, sans-serif' }}>
      <div className="w-full max-w-md">
        <div className="text-center mb-6">
          <span className="text-xl font-bold" style={{ background: 'linear-gradient(90deg,#fbbf24,#a855f7)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
            Blackink
          </span>
        </div>

        <div className="glass-card p-8" style={{ border: '1px solid rgba(251,191,36,0.1)' }}>
          <h1 className="text-lg font-bold text-white mb-2">Payment authorization</h1>
          <p className="text-xs text-white/40 leading-relaxed mb-6">
            A temporary $1 authorization may appear briefly as pending on your statement -- it is never charged, and is
            released automatically once your card and bank account are both verified.
          </p>

          {!token && (
            <p className="text-sm" style={{ color: '#f87171' }}>
              This payment link is missing its token. Please use the link exactly as provided.
            </p>
          )}

          {token && loadError && (
            <p className="text-sm" style={{ color: '#f87171' }}>{loadError}</p>
          )}

          {token && !loadError && (
            <div className="space-y-5">
              <div>
                <h2 className="text-xs font-semibold text-white/70 uppercase tracking-widest mb-3">Bank account holder details</h2>
                <label className={labelCls}>Name on the bank account</label>
                <input
                  type="text" autoComplete="name" value={holderName}
                  onChange={(e) => setHolderName(e.target.value)}
                  className={inputCls + ' mb-3'}
                />
                <label className={labelCls}>Email</label>
                <input
                  type="email" autoComplete="email" value={holderEmail}
                  onChange={(e) => setHolderEmail(e.target.value)}
                  className={inputCls}
                />
              </div>

              <div>
                <h2 className="text-xs font-semibold text-white/70 uppercase tracking-widest mb-3">Card</h2>
                <div ref={cardMountRef} className="px-4 py-3 rounded-[var(--bi-radius-md)]" style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)' }} />
              </div>

              <div>
                <h2 className="text-xs font-semibold text-white/70 uppercase tracking-widest mb-3">Bank account (ACH)</h2>
                {!bankConnected ? (
                  <button
                    type="button"
                    disabled={!cardMounted || connectingBank}
                    onClick={handleConnectBank}
                    className="w-full h-11 rounded-[var(--bi-radius-md)] font-semibold text-sm disabled:opacity-60 disabled:cursor-not-allowed"
                    style={{ background: 'rgba(255,255,255,0.08)', border: '1px solid rgba(255,255,255,0.15)', color: '#fff' }}
                  >
                    {connectingBank ? 'Connecting…' : 'Connect bank account'}
                  </button>
                ) : (
                  <p className="text-sm" style={{ color: '#4ade80' }}>Bank account connected.</p>
                )}
              </div>

              <button
                type="button"
                disabled={!bankConnected || submitting}
                onClick={handleSubmit}
                className="w-full btn-primary h-12 text-base rounded-[var(--bi-radius-md)] font-bold disabled:opacity-60 disabled:cursor-not-allowed"
                style={{ background: 'linear-gradient(135deg,#fbbf24,#f59e0b)', color: '#0f172a' }}
              >
                {submitting ? 'Processing…' : 'Authorize payment methods'}
              </button>

              {message && (
                <p role="status" aria-live="polite" className="text-sm text-center" style={{ color: messageColor }}>
                  {message}
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
