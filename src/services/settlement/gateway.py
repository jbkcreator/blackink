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
		self, *, stripe_customer_id: str, default_payment_method_id: str,
		metadata: dict, idempotency_key: str,
	) -> InvoiceHandle: ...

	@abstractmethod
	def add_invoice_item(
		self, *, stripe_invoice_id: str, stripe_customer_id: str, amount_cents: int,
		description: str, idempotency_key: str,
	) -> None: ...

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
		invoice = self._client.invoices.create(
			params={
				"customer": stripe_customer_id,
				"collection_method": "charge_automatically",
				"default_payment_method": default_payment_method_id,
				"auto_advance": False,
				"pending_invoice_items_behavior": "exclude",
				"metadata": metadata,
			},
			options={"idempotency_key": idempotency_key},
		)
		return InvoiceHandle(stripe_invoice_id=invoice.id, status=invoice.status)

	def add_invoice_item(self, *, stripe_invoice_id, stripe_customer_id, amount_cents, description, idempotency_key):
		self._client.invoice_items.create(
			params={
				"customer": stripe_customer_id,
				"invoice": stripe_invoice_id,
				"amount": amount_cents,
				"currency": "usd",
				"description": description,
			},
			options={"idempotency_key": idempotency_key},
		)

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
		return PayOutcome(status=invoice.status)

	def void_or_delete_invoice(self, *, stripe_invoice_id, invoice_status):
		if invoice_status == "draft":
			self._client.invoices.delete(stripe_invoice_id)
		else:
			self._client.invoices.void_invoice(stripe_invoice_id)
