"""
Spotfire Server REST API Client.

Handles authentication, library item lookup, Automation Services job
execution, and file download — all via HTTP.  No local Spotfire Analyst
installation required.

Reference (Spotfire Server REST API):
  https://docs.tibco.com/pub/spotfire_server/latest/doc/html/TIB_sfire_server_tsdk_help/
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import yaml

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  Data classes
# ═══════════════════════════════════════════════════════════════

@dataclass
class SpotfireConfig:
    """Configuration for the Spotfire Server REST API client."""
    server_url: str
    api_base: str = "/api/rest"
    auth_type: str = "basic"
    username: str = ""
    password: str = ""
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    timeout: int = 120

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SpotfireConfig":
        return cls(
            server_url=data.get("server_url", "").rstrip("/"),
            api_base=data.get("api_base", "/api/rest"),
            auth_type=data.get("auth_type", "basic"),
            username=data.get("username", ""),
            password=data.get("password", "")
            or os.environ.get("SPOTFIRE_PASSWORD", ""),
            oauth_token_url=data.get("oauth_token_url", ""),
            oauth_client_id=data.get("oauth_client_id", ""),
            oauth_client_secret=data.get("oauth_client_secret", "")
            or os.environ.get("SPOTFIRE_OAUTH_SECRET", ""),
            timeout=data.get("timeout", 120),
        )


@dataclass
class LibraryItem:
    """A Spotfire library item (folder, analysis, or file)."""
    item_id: str
    title: str
    path: str
    item_type: str  # "analysis" | "folder" | "informationLink" | ...
    parent_id: str = ""


@dataclass
class JobResult:
    """Result of an Automation Services job execution."""
    job_id: str
    status: str  # "SUCCESS" | "FAILED" | "RUNNING" | "UNKNOWN"
    message: str = ""
    output_files: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
#  SpotfireClient
# ═══════════════════════════════════════════════════════════════

class SpotfireClient:
    """
    REST API client for TIBCO Spotfire Server.

    Supports:
      - Basic auth (username/password)
      - OAuth 2.0 (client credentials grant)
      - Library item lookup by path
      - Automation Services job execution + polling
      - File download from server library
    """

    def __init__(self, config: SpotfireConfig):
        self.config = config
        self._token: str | None = None
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ──────────────────────────────────────────────
    #  URL helpers
    # ──────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return f"{self.config.server_url}{self.config.api_base}"

    # ──────────────────────────────────────────────
    #  Authentication
    # ──────────────────────────────────────────────

    def authenticate(self) -> str:
        """Authenticate to Spotfire Server and store the bearer token."""
        if self.config.auth_type == "oauth":
            return self._auth_oauth()
        return self._auth_basic()

    def _auth_basic(self) -> str:
        """Authenticate using basic credentials."""
        url = f"{self.base_url}/api/v1/authentication/login"
        body = {
            "username": self.config.username,
            "password": self.config.password,
        }
        logger.info("Authenticating to Spotfire as %s …", self.config.username)
        logger.info("POST %s", url)
        resp = self._session.post(url, json=body, timeout=self.config.timeout)

        # Log response details for debugging
        logger.info("Response status: %d", resp.status_code)
        logger.info("Response headers: %s", dict(resp.headers))
        logger.info("Response body (first 500 chars): %s", resp.text[:500])

        resp.raise_for_status()

        # Check content type before parsing JSON
        content_type = resp.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            raise RuntimeError(
                f"Expected JSON response but got Content-Type: {content_type}\n"
                f"Response body: {resp.text[:500]}"
            )

        data = resp.json()
        token = data.get("accessToken", "")
        if not token:
            raise RuntimeError(
                f"Spotfire login succeeded but no token returned.\n"
                f"Response: {resp.text[:500]}"
            )

        self._token = token
        self._session.headers["Authorization"] = f"Bearer {token}"
        logger.info("Spotfire authentication successful")
        return token

    def _auth_oauth(self) -> str:
        """Authenticate using OAuth 2.0 client credentials grant."""
        logger.info("Authenticating to Spotfire via OAuth …")
        resp = self._session.post(
            self.config.oauth_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.config.oauth_client_id,
                "client_secret": self.config.oauth_client_secret,
            },
            timeout=self.config.timeout,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token", "")
        if not token:
            raise RuntimeError("OAuth token endpoint returned no access_token")
        self._token = token
        self._session.headers["Authorization"] = f"Bearer {token}"
        logger.info("OAuth authentication successful")
        return token

    def logout(self) -> None:
        """Invalidate the current session."""
        if not self._token:
            return
        url = f"{self.base_url}/api/v1/authentication/logout"
        try:
            self._session.post(url, timeout=10)
        except requests.RequestException:
            pass
        finally:
            self._token = None
            self._session.headers.pop("Authorization", None)
            logger.info("Spotfire logout complete")

    def _ensure_token(self) -> None:
        if not self._token:
            self.authenticate()

    def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        """Wrapper that auto-reauths on 401."""
        self._ensure_token()
        resp = self._session.request(
            method, url, timeout=self.config.timeout, **kwargs
        )
        if resp.status_code == 401:
            logger.info("Token expired, re-authenticating …")
            self.authenticate()
            resp = self._session.request(
                method, url, timeout=self.config.timeout, **kwargs
            )
        resp.raise_for_status()
        return resp

    # ──────────────────────────────────────────────
    #  Library operations
    # ──────────────────────────────────────────────

    def find_library_item(self, path_or_id: str) -> LibraryItem:
        """
        Find a library item by its Spotfire library path (e.g.
        ``/Users/Cargo/Revenue_Report_TD``) or by its item ID.

        Returns a :class:`LibraryItem`.
        """
        # If it looks like a GUID, fetch directly
        if _looks_like_guid(path_or_id):
            return self._get_library_item_by_id(path_or_id)

        # Otherwise search by path
        url = f"{self.base_url}/api/v1/library/items"
        resp = self._request("GET", url, params={"path": path_or_id})
        data = resp.json()

        items = data.get("items", [])
        if not items:
            raise ValueError(f"Library item not found: {path_or_id}")
        if len(items) > 1:
            # Pick exact path match
            for it in items:
                if it.get("path", "").lower() == path_or_id.lower():
                    items = [it]
                    break
            if len(items) > 1:
                logger.warning(
                    "Multiple items match '%s', using first: %s",
                    path_or_id,
                    items[0].get("title"),
                )

        it = items[0]
        return LibraryItem(
            item_id=it.get("id", ""),
            title=it.get("title", ""),
            path=it.get("path", path_or_id),
            item_type=it.get("type", ""),
            parent_id=it.get("parentId", ""),
        )

    def _get_library_item_by_id(self, item_id: str) -> LibraryItem:
        url = f"{self.base_url}/api/v1/library/items/{item_id}"
        resp = self._request("GET", url)
        it = resp.json()
        return LibraryItem(
            item_id=it.get("id", item_id),
            title=it.get("title", ""),
            path=it.get("path", ""),
            item_type=it.get("type", ""),
            parent_id=it.get("parentId", ""),
        )

    def list_data_tables(self, analysis_item_id: str) -> list[dict[str, Any]]:
        """
        List all data tables in an analysis (DXP) by its library item ID.

        Returns a list of dicts with keys: ``id``, ``name``, ``type``.
        """
        url = (
            f"{self.base_url}/api/v1/library/items/{analysis_item_id}"
            f"/datatables"
        )
        resp = self._request("GET", url)
        data = resp.json()
        tables = []
        for t in data.get("dataTables", []):
            tables.append(
                {
                    "id": t.get("id", ""),
                    "name": t.get("name", ""),
                    "type": t.get("type", ""),
                }
            )
        return tables

    # ──────────────────────────────────────────────
    #  Automation Services job execution
    # ──────────────────────────────────────────────

    def execute_automation_job(
        self,
        job_xml: str,
        poll_interval: int = 10,
        job_timeout: int = 1800,
    ) -> JobResult:
        """
        Submit an Automation Services job (XML) to the server and poll
        until completion.

        ``job_xml`` is the full Automation Services job definition XML string.
        """
        url = f"{self.base_url}/api/v1/automation/jobs"

        logger.info("Submitting Automation Services job …")
        resp = self._request(
            "POST",
            url,
            data=job_xml,
            headers={"Content-Type": "application/xml"},
        )
        data = resp.json()
        job_id = data.get("jobId", "")
        if not job_id:
            raise RuntimeError(
                f"Automation job submission failed: {data}"
            )
        logger.info("Job submitted, job_id=%s", job_id)

        # Poll for completion
        result = self._poll_job(job_id, poll_interval, job_timeout)
        return result

    def _poll_job(
        self, job_id: str, poll_interval: int, job_timeout: int
    ) -> JobResult:
        """Poll job status until terminal state or timeout."""
        url = f"{self.base_url}/api/v1/automation/jobs/{job_id}"
        start = time.time()

        while True:
            resp = self._request("GET", url)
            data = resp.json()
            status = data.get("status", "UNKNOWN").upper()

            if status in ("SUCCESS", "FAILED", "CANCELED"):
                message = data.get("message", "")
                output_files = data.get("outputFiles", [])
                logger.info("Job %s finished: %s", job_id, status)
                return JobResult(
                    job_id=job_id,
                    status=status,
                    message=message,
                    output_files=output_files,
                )

            elapsed = time.time() - start
            if elapsed > job_timeout:
                logger.error("Job %s timed out after %ds", job_id, job_timeout)
                return JobResult(
                    job_id=job_id,
                    status="TIMEOUT",
                    message=f"Job timed out after {job_timeout}s",
                )

            logger.debug(
                "Job %s status=%s, waiting %ds …", job_id, status, poll_interval
            )
            time.sleep(poll_interval)

    # ──────────────────────────────────────────────
    #  File download
    # ──────────────────────────────────────────────

    def download_library_file(
        self, item_id: str, dest_path: str
    ) -> str:
        """
        Download a library file (e.g. exported CSV) by its item ID
        to ``dest_path``.  Returns the local file path.
        """
        url = (
            f"{self.base_url}/api/v1/library/items/{item_id}/content"
        )
        resp = self._request("GET", url)
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(resp.content)
        logger.info("Downloaded %s → %s", item_id, dest_path)
        return dest_path

    # ──────────────────────────────────────────────
    #  Context manager
    # ──────────────────────────────────────────────

    def __enter__(self) -> "SpotfireClient":
        self.authenticate()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.logout()


# ═══════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════

def _looks_like_guid(s: str) -> bool:
    """Heuristic: does *s* look like a Spotfire GUID/UUID?"""
    s = s.strip().strip("{}")
    parts = s.split("-")
    return len(parts) == 5 and all(len(p) in (8, 4, 4, 4, 12) for p in parts)


def load_config(config_path: str) -> dict[str, Any]:
    """Load a YAML config file and return it as a dict."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)