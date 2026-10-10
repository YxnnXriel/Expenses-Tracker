from typing import TypedDict


class InvoiceData(TypedDict):
    is_subscription: bool
    service_name: str | None
    amount: float | None
    currency: str | None
    billing_date: str | None


def parse_invoice(email_content: str) -> InvoiceData:
    """Extract subscription information from an invoice email."""
    raise NotImplementedError("Gemini integration is not implemented yet.")
