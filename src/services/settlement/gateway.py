"""StripeGateway ABC — the six Stripe calls charge.py is allowed to make
(Subtask 1.2.2).

Per the compliance_gate.py vendor-stub convention, tests subclass this ABC
rather than mocking HTTP or monkeypatching the `stripe` module. Confining
every money-moving call to this interface (used only from charge.py) is
part of what makes "exactly one module can move money" a checkable
property — see tests/test_no_upfront_charge_paths.py.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class InvoiceHandle:
	"""Wraps just the fields charge.py needs back from Stripe, so callers
	never have to know the shape of the real stripe.Invoice object."""

	stripe_invoice_id: str
	status: str  # Stripe's own invoice status: 'draft' | 'open' | 'paid' | 'void' | ...


@dataclass(frozen=True)
class PayOutcome:
	status: str  # 'paid' | 'processing' | 'requires_action' | 'open' (failed/declined)
	decline_code: Optional[str] = None
	error_code: Optional[str] = None
	error_message: Optional[str] = None
	is_transport_error: bool = False  # timeout / APIConnectionError / IdempotencyError — never a decline


class StripeGateway(ABC):
	@abstractmethod
	def create_invoice(
		self, *, stripe_customer_id: str, default_payment_method_id: Optional[str],
		metadata: dict, idempotency_key: str,
	) -> InvoiceHandle:
		"""default_payment_method_id=None means the customer has no payment
		method on file for this invoice — the implementation must fall back
		to Stripe's own send_invoice collection (Stripe emails/hosts the
		invoice for manual payment) rather than charge_automatically, which
		would otherwise fail with no payment method to charge."""

	@abstractmethod
	def find_invoice_by_metadata(
		self, *, stripe_customer_id: str, metadata_filter: dict,
	) -> Optional[InvoiceHandle]:
		"""Reconciliation, not creation: lists this customer's recent invoices
		and returns the one whose metadata matches every key/value in
		metadata_filter, or None. Uses Stripe's `invoices.list` (immediately
		consistent) rather than the Search API (eventually consistent — a
		just-created invoice can be briefly invisible to it), because this is
		called on every entry BEFORE create_invoice specifically to survive a
		crash between Stripe accepting create_invoice and this process
		committing the id it just got back — see charge.py/sit_invoice.py."""

	@abstractmethod
	def add_invoice_item(
		self, *, stripe_invoice_id: str, stripe_customer_id: str, amount_cents: int,
		description: str, idempotency_key: str,
	) -> str:
		"""Returns the created invoice item's own Stripe id (invoice_items.id),
		not the parent invoice id — a caller that needs to look up or dispute
		THIS specific line item (e.g. billing_credits.stripe_invoice_item_id)
		cannot do so from the invoice id alone once an invoice carries more
		than one item."""

	@abstractmethod
	def update_invoice_metadata(self, *, stripe_invoice_id: str, metadata: dict) -> None: ...

	@abstractmethod
	def finalize_invoice(self, *, stripe_invoice_id: str, idempotency_key: str) -> InvoiceHandle: ...

	@abstractmethod
	def pay_invoice(
		self, *, stripe_invoice_id: str, payment_method_id: Optional[str], idempotency_key: str,
	) -> PayOutcome: ...

	@abstractmethod
	def void_or_delete_invoice(self, *, stripe_invoice_id: str, invoice_status: str) -> None:
		"""void_invoice for a finalized invoice, delete for a draft — NEVER
		pay_invoice. Used only by the clawback path."""


class LiveStripeGateway(StripeGateway):
	"""Delegates to src.services.payment_auth._stripe_client() — the same
	Stripe client construction 1.2.1 uses, so STRIPE_SECRET_KEY /
	PaymentAuthNotConfiguredError behavior is identical."""

	def __init__(self) -> None:
		from src.services.payment_auth import _stripe_client

		self._client = _stripe_client()

	def create_invoice(self, *, stripe_customer_id, default_payment_method_id, metadata, idempotency_key):
		params = {
			"customer": stripe_customer_id,
			"auto_advance": False,
			"pending_invoice_items_behavior": "exclude",
			"metadata": metadata,
		}
		if default_payment_method_id:
			params["collection_method"] = "charge_automatically"
			params["default_payment_method"] = default_payment_method_id
		else:
			# No payment method on file — Stripe emails/hosts the invoice for
			# the customer to pay manually, instead of a charge attempt that
			# would fail outright with nothing to charge.
			params["collection_method"] = "send_invoice"
			params["days_until_due"] = 14
		invoice = self._client.invoices.create(
			params=params, options={"idempotency_key": idempotency_key},
		)
		return InvoiceHandle(stripe_invoice_id=invoice.id, status=invoice.status)

	def find_invoice_by_metadata(self, *, stripe_customer_id, metadata_filter):
		# Stripe's invoices.list has no metadata query param — list this
		# customer's recent invoices (newest first, Stripe's default order)
		# and match metadata client-side. Bounded to 100 (Stripe's own max
		# page size) — a customer's per-installment/per-appointment invoice
		# count is small, and this only runs at the start of a charge attempt.
		invoices = self._client.invoices.list(params={"customer": stripe_customer_id, "limit": 100})
		for invoice in invoices.data:
			meta = invoice.metadata.to_dict() if invoice.metadata else {}
			if all(meta.get(k) == v for k, v in metadata_filter.items()):
				return InvoiceHandle(stripe_invoice_id=invoice.id, status=invoice.status)
		return None

	def add_invoice_item(self, *, stripe_invoice_id, stripe_customer_id, amount_cents, description, idempotency_key):
		item = self._client.invoice_items.create(
			params={
				"customer": stripe_customer_id,
				"invoice": stripe_invoice_id,
				"amount": amount_cents,
				"currency": "usd",
				"description": description,
			},
			options={"idempotency_key": idempotency_key},
		)
		return item.id

	def update_invoice_metadata(self, *, stripe_invoice_id, metadata):
		self._client.invoices.update(stripe_invoice_id, params={"metadata": metadata})

	def finalize_invoice(self, *, stripe_invoice_id, idempotency_key):
		invoice = self._client.invoices.finalize_invoice(
			stripe_invoice_id, options={"idempotency_key": idempotency_key}
		)
		return InvoiceHandle(stripe_invoice_id=invoice.id, status=invoice.status)

	def pay_invoice(self, *, stripe_invoice_id, payment_method_id, idempotency_key):
		import stripe as stripe_sdk

		params = {}
		if payment_method_id:
			params["payment_method"] = payment_method_id
			params["off_session"] = True
		try:
			invoice = self._client.invoices.pay(
				stripe_invoice_id, params=params, options={"idempotency_key": idempotency_key}
			)
		except stripe_sdk.error.CardError as exc:
			return PayOutcome(
				status="open",
				decline_code=getattr(exc, "code", None),
				error_code=getattr(exc, "code", None),
				error_message=str(exc),
			)
		except (stripe_sdk.error.APIConnectionError, stripe_sdk.error.IdempotencyError) as exc:
			return PayOutcome(status="processing", is_transport_error=True, error_message=str(exc))
		if invoice.status == "paid":
			return PayOutcome(status="paid")
		# `.pay()` returned without raising — for ACH this is the NORMAL case,
		# never a decline: Stripe delivers every ACH failure asynchronously via
		# the invoice.payment_failed webhook (see stripe_webhook_router.py's
		# _handle_settlement_event), days later in production and never
		# synchronously from this call. A genuine synchronous decline (a card
		# payment method) already raised CardError above and returned before
		# reaching here. Verified against a real Stripe test-mode ACH payment
		# (re-review verification pass): `.pay()` returned the invoice still
		# `status="open"` while the charge was in flight; the SAME invoice
		# reached `status="paid"` moments later with no exception ever raised.
		# Treating that "open, no exception" result as a decline (the
		# pre-existing bug this comment replaces) would fire the card fallback
		# — or mark the installment FAILED — while the ACH charge was still
		# genuinely succeeding, a direct double-charge risk this repo's own
		# "under-billing, never double-billing" invariant forbids.
		return PayOutcome(status="processing")

	def void_or_delete_invoice(self, *, stripe_invoice_id, invoice_status):
		if invoice_status == "draft":
			self._client.invoices.delete(stripe_invoice_id)
		else:
			self._client.invoices.void_invoice(stripe_invoice_id)
