
import base64
import json
import os
import random
import time

from html.parser import HTMLParser
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError


DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_MAX_RESULTS = 100

INVOICE_QUERY_TERMS = (
    "{receipt invoice subscription renewal "
    '"payment confirmation" facture abonnement}'
)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"


def authenticate_gmail() -> Resource:
    """Authenticate the user and return a Gmail API service."""
    credentials = None

    if os.path.exists(TOKEN_FILE):
        credentials = Credentials.from_authorized_user_file(
            TOKEN_FILE,
            SCOPES,
        )

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        if not os.path.exists(CREDENTIALS_FILE):
            raise FileNotFoundError(
                f"{CREDENTIALS_FILE} is missing. "
                "Download your OAuth client credentials from Google Cloud."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            CREDENTIALS_FILE,
            SCOPES,
        )

        credentials = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w", encoding="utf-8") as token_file:
            token_file.write(credentials.to_json())

    return build("gmail", "v1", credentials=credentials)


def execute_with_retry(
    request: Any,
    max_retries: int = 5,
) -> dict[str, Any]:
    """Retry requests on temporary Gmail quota or server errors."""
    for attempt in range(max_retries):
        try:
            return request.execute()

        except HttpError as error:
            details = error.content.decode("utf-8", errors="replace")

            retryable = (
                error.resp.status == 429
                or (
                    error.resp.status == 403
                    and any(
                        reason in details
                        for reason in (
                            "rateLimitExceeded",
                            "userRateLimitExceeded",
                        )
                    )
                )
                or error.resp.status >= 500
            )

            if not retryable or attempt == max_retries - 1:
                raise

            delay = min(2 ** attempt, 32) + random.uniform(0, 1)
            time.sleep(delay)

    raise RuntimeError("Gmail request failed after retries.")


class HTMLTextExtractor(HTMLParser):
    """Convert basic HTML content into readable text."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag in {"script", "style"}:
            self._hidden_depth += 1
        elif tag in {"br", "p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._hidden_depth:
            self._hidden_depth -= 1
        elif tag in {"p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)

    def get_text(self) -> str:
        return "\n".join(
            line.strip()
            for line in "".join(self.parts).splitlines()
            if line.strip()
        )


def _decode_part(data: str) -> str:
    """Decode Gmail's URL-safe base64 message body."""
    decoded = base64.urlsafe_b64decode(
        data + "=" * (-len(data) % 4)
    )
    return decoded.decode("utf-8", errors="replace")


def _collect_body_parts(
    payload: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Collect plain-text and HTML parts from a MIME payload."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")

    if body_data and mime_type in {"text/plain", "text/html"}:
        decoded = _decode_part(body_data)

        if mime_type == "text/plain":
            plain_parts.append(decoded)
        else:
            html_parts.append(decoded)

    for part in payload.get("parts", []):
        plain, html = _collect_body_parts(part)
        plain_parts.extend(plain)
        html_parts.extend(html)

    return plain_parts, html_parts


def decode_email_body(payload: dict[str, Any]) -> str:
    """Extract readable text, preferring plain text over HTML."""
    plain_parts, html_parts = _collect_body_parts(payload)

    if plain_parts:
        return "\n".join(plain_parts).strip()

    if html_parts:
        extractor = HTMLTextExtractor()

        for html in html_parts:
            extractor.feed(html)

        return extractor.get_text()

    return ""


def fetch_invoice_emails(
    service: Resource,
    days: int = DEFAULT_LOOKBACK_DAYS,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> list[dict[str, str]]:
    """
    Fetch candidate invoice emails from Gmail.

    Args:
        service: Authenticated Gmail API service.
        days: Number of days to look back.
        max_results: Maximum number of messages to fetch.

    Returns:
        A list of normalized email dictionaries.
    """
    if days < 1:
        raise ValueError("days must be greater than 0")

    if max_results < 1:
        raise ValueError("max_results must be greater than 0")

    query = f"newer_than:{days}d {INVOICE_QUERY_TERMS}"

    emails: list[dict[str, str]] = []
    page_token: str | None = None

    while len(emails) < max_results:
        page_size = min(100, max_results - len(emails))

        params: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": page_size,
        }

        if page_token:
            params["pageToken"] = page_token

        response = execute_with_retry(
            service.users()
            .messages()
            .list(**params)
        )

        messages = response.get("messages", [])

        if not messages:
            break

        for message in messages:
            if len(emails) >= max_results:
                break

            email = execute_with_retry(
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message["id"],
                    format="full",
                )
            )

            payload = email.get("payload", {})
            headers = payload.get("headers", [])

            header_map = {
                header["name"].lower(): header["value"]
                for header in headers
            }

            emails.append(
                {
                    "id": email["id"],
                    "thread_id": email.get("threadId", ""),
                    "sender": header_map.get("from", ""),
                    "subject": header_map.get("subject", ""),
                    "date": header_map.get("date", ""),
                    "body": decode_email_body(payload),
                }
            )

            # Reduce request pressure on the Gmail API.
            time.sleep(0.15)

        page_token = response.get("nextPageToken")

        if not page_token:
            break

    return emails
