"""
Wrapper for HCSS Identity + HeavyJob v1 APIs used by Construction Operations API.
"""

import logging
import os
import threading
import warnings
from datetime import datetime, timedelta
from typing import Any

import requests
from dotenv import load_dotenv

# Suppress noisy dependency warning in some managed environments.
warnings.filterwarnings(
    "ignore",
    message=r".*doesn't match a supported version.*",
    module=r"requests",
)

load_dotenv()

logger = logging.getLogger(__name__)


class HCSSClient:
    """
    Wrapper for HCSS Identity + HeavyJob v1 APIs used by Construction Operations API.
    
    Token management is thread-safe to prevent race conditions when multiple
    requests arrive simultaneously.
    """
    
    # Class-level token cache shared across all instances (thread-safe)
    _token_lock = threading.Lock()
    _shared_token: str | None = None
    _shared_token_expiry: datetime | None = None

    def __init__(self) -> None:
        self.client_id = os.getenv("HCSS_CLIENT_ID")
        self.client_secret = os.getenv("HCSS_CLIENT_SECRET")
        self.id_url = "https://api.hcssapps.com/identity/connect/token"
        self.heavyjob_base = "https://api.hcssapps.com/heavyjob/api/v1"

        self._session = requests.Session()
        self._session.trust_env = False

        from requests.adapters import HTTPAdapter

        adapter = HTTPAdapter(max_retries=0, pool_connections=10, pool_maxsize=20)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        
        # Timeout for HCSS API requests — can be overridden via HCSS_TIMEOUT_SECONDS env var
        self._timeout_s = int(os.getenv("HCSS_TIMEOUT_SECONDS", "120"))
        logger.debug("HCSSClient initialised (client_id=%s timeout=%ds)", (self.client_id or "")[:8] + "***", self._timeout_s)

    def _request(self, method: str, url: str, *, params: dict[str, Any] | None = None, json: Any | None = None) -> Any:
        import time

        # Retry on HTTP 429 (rate limit) with backoff so timecards are never
        # silently dropped during bulk runs (backfills / busy days).
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            start = time.perf_counter()
            logger.debug("HCSS request: %s %s params=%s", method, url, params)

            resp = self._session.request(
                method,
                url,
                headers=self._get_headers(),
                params=params,
                json=json,
                timeout=self._timeout_s,
            )
            duration = time.perf_counter() - start
            logger.info("HCSS response: %s %s → HTTP %s (%.2fs)", method, url, resp.status_code, duration)

            if resp.status_code == 429 and attempt < max_attempts:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else attempt * 5.0
                except ValueError:
                    wait = attempt * 5.0
                logger.warning(
                    "HCSS 429 rate limited on %s %s — waiting %.0fs (attempt %d/%d)",
                    method, url, wait, attempt, max_attempts,
                )
                time.sleep(wait)
                continue
            break

        resp.raise_for_status()
        
        # Check for empty response body
        response_text = resp.text
        if not response_text or not response_text.strip():
            logger.error("HCSS API returned empty response body for %s %s (HTTP %s)", method, url, resp.status_code)
            raise RuntimeError(f"HCSS API returned empty response for {method} {url}")
        
        try:
            json_data = resp.json()
        except ValueError as e:
            logger.error("HCSS API returned invalid JSON for %s %s: %s", method, url, response_text[:500])
            raise RuntimeError(f"HCSS API returned invalid JSON: {e}") from e
        
        return json_data

    @staticmethod
    def _extract_results_and_cursor(data: Any) -> tuple[list[dict[str, Any]], str | None]:
        if not isinstance(data, dict):
            return data, None  # type: ignore[return-value]
        results = data.get("results", []) or []
        metadata = data.get("metadata") or {}
        next_cursor = metadata.get("nextCursor")
        return results, next_cursor

    def _get_token(self) -> str:
        """
        Get a valid HCSS access token, using cached token if still valid.
        Thread-safe to prevent race conditions during token refresh.
        """
        # Quick check without lock (optimization for common case)
        with HCSSClient._token_lock:
            if HCSSClient._shared_token and HCSSClient._shared_token_expiry:
                remaining = (HCSSClient._shared_token_expiry - datetime.now()).total_seconds()
                if remaining > 30:  # Use cached token if >30s remaining
                    logger.debug("Using cached HCSS token (expires in %.0f seconds)", remaining)
                    return HCSSClient._shared_token
                else:
                    logger.info("Cached token expires soon (%.0fs remaining), refreshing", remaining)

            # Need to fetch new token (still holding lock to prevent concurrent fetches)
            if not self.client_id or not self.client_secret:
                raise RuntimeError("Missing HCSS credentials. Set HCSS_CLIENT_ID and HCSS_CLIENT_SECRET in .env.")

            logger.info("Requesting new HCSS access token")

            payload = {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "heavyjob:read timecards:read",
            }
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "gould-apm-time-log/1.0",
            }

            response = self._session.post(
                self.id_url,
                data=payload,
                headers=headers,
                allow_redirects=False,
                timeout=self._timeout_s,
            )

            if response.status_code != 200:
                try:
                    err_body = response.json()
                    msg = (
                        err_body.get("error_description")
                        or err_body.get("error")
                        or str(err_body)
                    )
                except ValueError:
                    msg = (response.text or "")[:800] or "(empty body)"
                raise RuntimeError(
                    f"HCSS token request failed (HTTP {response.status_code}): {msg}"
                )

            if not (response.text or "").strip():
                raise RuntimeError(
                    "HCSS token endpoint returned HTTP 200 with an empty body. "
                    "Check network, VPN, or firewall/proxy blocking api.hcssapps.com."
                )

            try:
                data = response.json()
            except ValueError:
                snippet = (response.text or "")[:800].replace("\n", " ")
                raise RuntimeError(
                    "HCSS token endpoint returned HTTP 200 but the body was not JSON. "
                    f"First part of response: {snippet!r}"
                ) from None

            token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            
            if not token:
                raise RuntimeError("HCSS token response did not include access_token.")
            
            # Cache token at class level (shared across all instances)
            HCSSClient._shared_token = token
            HCSSClient._shared_token_expiry = datetime.now() + timedelta(seconds=expires_in - 60)
            
            logger.info("HCSS token acquired and cached (expires in %ss)", expires_in)
            return token

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
            "User-Agent": "gould-apm-time-log/1.0",
        }

    def fetch_timecards(
        self,
        *,
        start_date: str,
        end_date: str,
        business_unit_id: str | None = None,
        job_id: str | None = None,
        foreman_id: str | None = None,
    ) -> list[dict[str, Any]]:
        url = f"{self.heavyjob_base}/timeCardInfo"
        params: dict[str, Any] = {"startDate": start_date, "endDate": end_date}
        if business_unit_id:
            params["businessUnitId"] = business_unit_id
        if job_id:
            params["jobId"] = job_id
        if foreman_id:
            params["foremanId"] = foreman_id

        logger.info(
            "Fetching timecards: start=%s end=%s bu=%s job=%s foreman=%s",
            start_date, end_date, business_unit_id or "all", job_id or "all", foreman_id or "all"
        )

        all_results: list[dict[str, Any]] = []
        cursor: str | None = None
        page_count = 0
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", url, params=params)
            results, next_cursor = self._extract_results_and_cursor(data)
            page_count += 1
            logger.debug("Fetched page %d: %d timecards", page_count, len(results))
            all_results.extend(results)
            if not next_cursor:
                break
            cursor = next_cursor
        
        logger.info(
            "Fetched %d total timecards across %d pages for date range %s to %s",
            len(all_results), page_count, start_date, end_date
        )
        return all_results

    def fetch_timecard_detail(self, timecard_id: str) -> dict[str, Any]:
        url = f"{self.heavyjob_base}/timeCards/{timecard_id}"
        data = self._request("GET", url)
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected timecard detail response type.")
        return data

    def fetch_diaries(
        self,
        *,
        business_unit_id: str,
        job_ids: list[str] | None = None,
        foreman_ids: list[str] | None = None,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        url = f"{self.heavyjob_base}/diaries/search"
        payload: dict[str, Any] = {
            "businessUnitId": business_unit_id,
            "startDate": start_date,
            "endDate": end_date,
        }
        if job_ids:
            payload["jobIds"] = job_ids
        if foreman_ids:
            payload["foremanIds"] = foreman_ids

        all_results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            if cursor:
                payload["cursor"] = cursor
            data = self._request("POST", url, json=payload)
            results, next_cursor = self._extract_results_and_cursor(data)
            all_results.extend(results)
            if not next_cursor:
                break
            cursor = next_cursor
        return all_results

    def fetch_attachments_advanced(
        self,
        *,
        business_unit_id: str,
        job_ids: list[str] | None = None,
        foreman_ids: list[str] | None = None,
        start_date: str,
        end_date: str,
        file_type: str = "all",
    ) -> list[dict[str, Any]]:
        url = f"{self.heavyjob_base}/attachment/advancedRequest"
        payload: dict[str, Any] = {
            "businessUnitId": business_unit_id,
            "startDate": start_date,
            "endDate": end_date,
            "fileType": file_type,
        }
        if job_ids:
            payload["jobIds"] = job_ids
        if foreman_ids:
            payload["foremanIds"] = foreman_ids

        all_results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            if cursor:
                payload["cursor"] = cursor
            data = self._request("POST", url, json=payload)
            results, next_cursor = self._extract_results_and_cursor(data)
            all_results.extend(results)
            if not next_cursor:
                break
            cursor = next_cursor
        return all_results

    def fetch_job_subcontract_items(self, *, job_id: str, is_deleted: bool | None = None, is_discontinued: bool | None = None) -> list[dict[str, Any]]:
        url = f"{self.heavyjob_base}/jobs/{job_id}/costTypes/jobSubcontract"
        params: dict[str, Any] = {}
        if is_deleted is not None:
            params["isDeleted"] = bool(is_deleted)
        if is_discontinued is not None:
            params["isDiscontinued"] = bool(is_discontinued)
        data = self._request("GET", url, params=params)
        return data.get("results", []) if isinstance(data, dict) else data  # type: ignore[return-value]

    def fetch_subcontract_work_transactions_advanced(
        self,
        *,
        business_unit_id: str | None = None,
        job_ids: list[str] | None = None,
        foreman_ids: list[str] | None = None,
        start_date: str,
        end_date: str,
        cost_code_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        url = f"{self.heavyjob_base}/costTypes/subcontractWork/advancedRequest"
        payload: dict[str, Any] = {"startDate": start_date, "endDate": end_date}
        if business_unit_id:
            payload["businessUnitId"] = business_unit_id
        if job_ids:
            payload["jobIds"] = job_ids
        if foreman_ids:
            payload["foremanIds"] = foreman_ids
        if cost_code_ids:
            payload["costCodeIds"] = cost_code_ids

        all_results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            if cursor:
                payload["cursor"] = cursor
            data = self._request("POST", url, json=payload)
            results, next_cursor = self._extract_results_and_cursor(data)
            all_results.extend(results)
            if not next_cursor:
                break
            cursor = next_cursor
        return all_results

