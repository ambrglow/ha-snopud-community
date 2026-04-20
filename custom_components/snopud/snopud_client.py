"""Async HTTP client for the MySnoPUD customer portal.

Responsibilities:
  * Authenticate via the MySnoPUD login form (cookie-based session)
  * Discover available meters on the account
  * Fetch Green Button XML for a given meter and date range, using the same
    public export the portal's own "Download my usage" button uses.

This module has no Home Assistant dependencies so it can be unit-tested
standalone.

Endpoints used (all are the same endpoints the logged-in web UI calls):
  POST /Home/Login                        — form login (cookie session)
  GET  /Usage/InitializeDownloadSettings  — returns an AjaxResults JSON
                                             envelope containing the CSRF
                                             token and meter list
  POST /Usage/Download                    — returns Green Button XML directly
                                             as the response body
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final
from urllib.parse import urlencode

import aiohttp

from .const import (
    BASE_URL,
    DEFAULT_SELECTED_INTERVAL,
    DOWNLOAD_SETTINGS_URL,
    DOWNLOAD_URL,
    FORMAT_GREEN_BUTTON,
    LOGIN_URL,
    MAX_DOWNLOAD_WINDOW_DAYS,
    SERVICE_TYPE_ELECTRIC,
    USAGE_TYPE_KWH,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Matches the CSRF token inside the JSON-escaped HTML returned by
# /Usage/InitializeDownloadSettings. Attribute quotes are backslash-escaped
# because the HTML is embedded inside a JSON string value.
_CSRF_RE: Final = re.compile(
    r'name=\\?"__RequestVerificationToken\\?"[^>]*?value=\\?"([^"\\]+)\\?"'
)

# Matches each meter in the escaped HTML:
#   <input ... name="Meters[0].Value" ... value="9000001">
#   <label ... for="Meters_0__Selected">Meter #1000000001 (Electric) - ...</label>
_METER_INDEX_RE: Final = re.compile(
    r'name=\\?"Meters\[(?P<idx>\d+)\]\.Value\\?"[^>]*?value=\\?"(?P<internal>\d+)\\?"'
)
_METER_LABEL_RE: Final = re.compile(
    r'for=\\?"Meters_(?P<idx>\d+)__Selected\\?"[^>]*?>\s*Meter\s*#(?P<account>\d+)\s*\(([^)]+)\)(?:\s*-\s*([^<]+?))?\s*<'
)

# Honest User-Agent: identifies the integration, its version, and where to find
# it. Per MySnoPUD T&C §3(i) we must not disguise our origin.
_DEFAULT_USER_AGENT = (
    f"ha-snopud-community/{VERSION} "
    f"(+https://github.com/ambrglow/ha-snopud-community)"
)


class SnoPUDError(Exception):
    """Base exception for SnoPUD client errors."""


class SnoPUDAuthError(SnoPUDError):
    """Authentication failed (bad credentials, session expired, locked out)."""


class SnoPUDDownloadError(SnoPUDError):
    """A download request was rejected or returned an unexpected payload."""


@dataclass(frozen=True)
class MeterInfo:
    """Describes a meter available on the authenticated account."""

    account_number: str  # the meter number the user sees, e.g. "1000000001"
    internal_id: str  # the platform-internal ID used in form POSTs, e.g. "9000001"
    service_type: str  # "Electric", etc.
    rate_schedule: str | None  # "Residential Schedule 7", etc.


class SnoPUDClient:
    """Client for interacting with my.snopud.com.

    Usage:
        async with aiohttp.ClientSession() as http:
            client = SnoPUDClient(http, "user@example.com", "password")
            await client.async_login()
            meters = await client.async_get_meters()
            xml = await client.async_download_green_button(
                meter=meters[0],
                start=date(2026, 4, 1),
                end=date(2026, 4, 17),
            )
    """

    def __init__(
        self,
        http_session: aiohttp.ClientSession,
        email: str,
        password: str,
    ) -> None:
        self._http = http_session
        self._email = email
        self._password = password
        self._logged_in = False
        # Identify ourselves honestly in the User-Agent header so SnoPUD's
        # operators can see exactly which tool is making the request.
        self._default_headers = {"User-Agent": _DEFAULT_USER_AGENT}

    def _headers(self, **extra: str) -> dict[str, str]:
        """Merge default headers with per-request extras."""
        return {**self._default_headers, **extra}

    # ---------- auth ----------

    async def async_login(self) -> None:
        """Authenticate. Raises SnoPUDAuthError on failure."""
        # Warm the session first: GET / so the server can set any anti-forgery
        # cookies and the ASP.NET session-state cookie. A POST to /Home/Login
        # without these is silently rejected even with correct credentials —
        # the server returns a tiny 200 body rather than redirecting to
        # /Dashboard.
        try:
            async with self._http.get(
                f"{BASE_URL}/", headers=self._headers()
            ) as warmup:
                await warmup.read()
        except aiohttp.ClientError as err:
            raise SnoPUDError(f"network error during session warmup: {err}") from err

        form = {
            "RedirectUrl": "",
            "LoginErrorMessage": "",
            "LoginEmail": self._email,
            "LoginPassword": self._password,
            "ExternalLogin": "False",
            "TwoFactorRendered": "False",
            "SecretQuestionRendered": "False",
            "RememberMe": "false",
        }

        try:
            async with self._http.post(
                LOGIN_URL,
                data=form,
                allow_redirects=True,
                headers=self._headers(Referer=f"{BASE_URL}/"),
            ) as resp:
                final_url = str(resp.url)
                body = await resp.text()
                status = resp.status
        except aiohttp.ClientError as err:
            raise SnoPUDError(f"network error during login: {err}") from err

        _LOGGER.debug(
            "SnoPUD login POST completed: status=%s final_url=%s body_len=%d",
            status, final_url, len(body),
        )

        # Success detection. The customer-portal SPA returns one of three
        # shapes on success:
        #   1. Server-side 302 redirect to /Dashboard (final_url contains it)
        #   2. JSON AjaxResults envelope with a "Redirect" action pointing to
        #      a post-login URL like /Integration/LoginActions
        #   3. Full HTML page containing the authenticated /User/LogOut link
        # Failure is a JSON envelope with LoginErrorMessage in Data.
        import json as _json
        redirect_target = None
        if body.lstrip().startswith("{"):
            try:
                parsed = _json.loads(body)
                data = parsed.get("Data") or {}
                err_msg = data.get("LoginErrorMessage") if isinstance(data, dict) else None
                if err_msg:
                    raise SnoPUDAuthError(f"login rejected: {err_msg}")
                for action in parsed.get("AjaxResults", []) or []:
                    if isinstance(action, dict) and action.get("Action") == "Redirect":
                        redirect_target = action.get("Value")
                        break
            except ValueError:
                pass

        success = (
            "/Dashboard" in final_url
            or "/User/LogOut" in body
            or ("Dashboard" in body and len(body) > 2000)
            or (redirect_target and "/Home/Login" not in redirect_target)
        )
        if not success:
            raise SnoPUDAuthError(
                "login rejected — credentials invalid, account locked, or MFA required"
            )

        # Follow the post-login redirect — the target page performs post-login
        # bookkeeping and may set additional session cookies.
        if redirect_target:
            _LOGGER.debug("following post-login redirect to %s", redirect_target)
            try:
                async with self._http.get(
                    redirect_target, headers=self._headers()
                ) as post_login:
                    await post_login.read()
            except aiohttp.ClientError as err:
                _LOGGER.warning(
                    "post-login redirect fetch failed (continuing anyway): %s", err
                )

        self._logged_in = True
        _LOGGER.debug("SnoPUD login succeeded (landed at %s)", final_url)

    async def async_logout(self) -> None:
        """Best-effort logout. Never raises."""
        if not self._logged_in:
            return
        try:
            async with self._http.get(
                f"{BASE_URL}/User/LogOut", headers=self._headers()
            ) as _resp:
                pass
        except aiohttp.ClientError:
            pass
        self._logged_in = False

    # ---------- discovery ----------

    async def async_get_meters(self) -> list[MeterInfo]:
        """Return all meters visible to the authenticated account.

        Side effect: populates the CSRF token needed for the next download.
        """
        html_fragment, _token = await self._fetch_download_settings()
        return self._parse_meters(html_fragment)

    # ---------- downloads ----------

    async def async_download_green_button(
        self,
        meter: MeterInfo,
        start: date,
        end: date,
        all_meters: list[MeterInfo] | None = None,
        interval: str = DEFAULT_SELECTED_INTERVAL,
    ) -> bytes:
        """Download Green Button XML for *meter* across [start, end].

        Parameters
        ----------
        meter
            The meter to fetch. Must be one of the meters returned by
            async_get_meters().
        start, end
            Inclusive date range. The server treats dates in its local (PT)
            timezone. Range must be <= MAX_DOWNLOAD_WINDOW_DAYS.
        all_meters
            Optional — if the account has multiple meters, the form *must*
            include all Meters[n].Value entries (only the target's .Selected
            field is set to true). If omitted, we fetch the meter list first.
        interval
            MyMeterQ interval code (one of the INTERVAL_* constants). Default
            is hourly because that's what Home Assistant's statistics API
            stores.

        Returns
        -------
        bytes
            Raw XML body of the Green Button Atom feed.
        """
        if not self._logged_in:
            raise SnoPUDAuthError("client is not logged in")
        if end < start:
            raise ValueError("end date must be >= start date")
        if (end - start).days > MAX_DOWNLOAD_WINDOW_DAYS:
            raise ValueError(
                f"date range too large ({(end - start).days} days); "
                f"chunk into <= {MAX_DOWNLOAD_WINDOW_DAYS}-day windows"
            )

        html_fragment, token = await self._fetch_download_settings()
        meters = all_meters or self._parse_meters(html_fragment)
        if meter.internal_id not in {m.internal_id for m in meters}:
            raise SnoPUDError(
                f"meter {meter.account_number} not found on this account"
            )

        body = self._build_download_form(
            token=token,
            meters=meters,
            target_internal_id=meter.internal_id,
            start=start,
            end=end,
            interval=interval,
        )

        try:
            async with self._http.post(
                DOWNLOAD_URL,
                data=body,
                headers=self._headers(**{
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"{BASE_URL}/Dashboard",
                }),
            ) as resp:
                if resp.status != 200:
                    raise SnoPUDDownloadError(
                        f"download returned HTTP {resp.status}"
                    )
                raw = await resp.read()
        except aiohttp.ClientError as err:
            raise SnoPUDDownloadError(f"network error during download: {err}") from err

        # Server returns an HTML error page on validation failure rather than XML.
        head = raw[:80].lstrip()
        if not head.startswith(b"<?xml"):
            if b"<html" in head.lower() or b"<!doctype" in head.lower():
                raise SnoPUDDownloadError(
                    "server returned an error page instead of Green Button XML — "
                    "check that the meter is selectable and the date range is valid"
                )
            raise SnoPUDDownloadError(
                f"unexpected response shape (first bytes: {head[:40]!r})"
            )
        return raw

    # ---------- internals ----------

    async def _fetch_download_settings(self) -> tuple[str, str]:
        """Fetch /Usage/InitializeDownloadSettings, return (html_fragment, csrf_token)."""
        try:
            async with self._http.get(
                DOWNLOAD_SETTINGS_URL,
                headers=self._headers(**{"X-Requested-With": "XMLHttpRequest"}),
            ) as resp:
                if resp.status == 401 or resp.status == 403:
                    raise SnoPUDAuthError("session expired")
                if resp.status != 200:
                    raise SnoPUDError(
                        f"settings endpoint returned HTTP {resp.status}"
                    )
                body = await resp.text()
        except aiohttp.ClientError as err:
            raise SnoPUDError(f"network error fetching settings: {err}") from err

        match = _CSRF_RE.search(body)
        if not match:
            # If the session died silently, the response is typically a redirect
            # to the login page rendered inline.
            if "LoginEmail" in body or "LoginPassword" in body:
                self._logged_in = False
                raise SnoPUDAuthError("session expired (login form returned)")
            raise SnoPUDError("could not locate CSRF token in settings response")
        return body, match.group(1)

    @staticmethod
    def _parse_meters(html_fragment: str) -> list[MeterInfo]:
        """Extract MeterInfo records from the download settings HTML fragment."""
        # Pair each Meters[idx].Value (internal id) with the adjacent label text.
        internals: dict[str, str] = {
            m.group("idx"): m.group("internal")
            for m in _METER_INDEX_RE.finditer(html_fragment)
        }
        labels: dict[str, tuple[str, str, str | None]] = {}
        for m in _METER_LABEL_RE.finditer(html_fragment):
            idx = m.group("idx")
            account = m.group("account")
            service = m.group(3).strip() if m.group(3) else ""
            rate = m.group(4).strip() if m.group(4) else None
            labels[idx] = (account, service, rate)

        meters: list[MeterInfo] = []
        for idx, internal in internals.items():
            if idx not in labels:
                continue
            account, service, rate = labels[idx]
            meters.append(
                MeterInfo(
                    account_number=account,
                    internal_id=internal,
                    service_type=service,
                    rate_schedule=rate,
                )
            )
        return meters

    @staticmethod
    def _build_download_form(
        *,
        token: str,
        meters: list[MeterInfo],
        target_internal_id: str,
        start: date,
        end: date,
        interval: str = DEFAULT_SELECTED_INTERVAL,
    ) -> str:
        """Build the exact form body /Usage/Download requires.

        The MVC model binder rejects the request unless the ColumnOptions and
        RowOptions arrays are fully populated, even when SelectedFormat=1
        (Green Button) hides the column-selection UI. The values below mirror
        what the browser UI sends on a default-configured form.

        Meter selection semantics:
          * every meter on the account gets a Meters[n].Value entry
          * only the target meter gets Meters[n].Selected=true
          * unselected meters have NO .Selected key at all (mimics an
            unchecked box in the browser form)
        """
        pairs: list[tuple[str, str]] = [
            ("HasMultipleUsageTypes", "True"),
            ("FileFormat", "download-usage-xml"),
            ("SelectedFormat", FORMAT_GREEN_BUTTON),
            ("ThirdPartyPODID", ""),
            ("SelectedServiceType", SERVICE_TYPE_ELECTRIC),
        ]
        for i, m in enumerate(meters):
            pairs.append((f"Meters[{i}].Value", m.internal_id))
            if m.internal_id == target_internal_id:
                pairs.append((f"Meters[{i}].Selected", "true"))
        pairs.extend(
            [
                ("SelectedInterval", interval),
                ("SelectedUsageType", USAGE_TYPE_KWH),
                ("Start", start.isoformat()),
                ("End", end.isoformat()),
                # ColumnOptions: values+names for all 8 positions,
                # plus Checked=true for indices 6 (Consumption) and 7 (Dollar).
                ("ColumnOptions[0].Value", "ReadDate"),
                ("ColumnOptions[0].Name", "ReadDate"),
                ("ColumnOptions[1].Value", "AccountNumber"),
                ("ColumnOptions[1].Name", "AccountNumber"),
                ("ColumnOptions[2].Value", "Name"),
                ("ColumnOptions[2].Name", "Name"),
                ("ColumnOptions[3].Value", "Meter"),
                ("ColumnOptions[3].Name", "Meter"),
                ("ColumnOptions[4].Value", "Location"),
                ("ColumnOptions[4].Name", "Location"),
                ("ColumnOptions[5].Value", "Address"),
                ("ColumnOptions[5].Name", "Address"),
                ("ColumnOptions[6].Value", "Consumption"),
                ("ColumnOptions[6].Name", "Consumption"),
                ("ColumnOptions[6].Checked", "true"),
                ("ColumnOptions[7].Value", "Dollar"),
                ("ColumnOptions[7].Name", "Dollar"),
                ("ColumnOptions[7].Checked", "true"),
                # RowOptions (sort order) — 3 entries covering Date/kWh/$
                ("RowOptions[0].Value", "ReadDate"),
                ("RowOptions[0].Name", "Read Date"),
                ("RowOptions[0].Desc", "false"),
                ("RowOptions[1].Value", "Consumption"),
                ("RowOptions[1].Name", "kWh"),
                ("RowOptions[1].Desc", "false"),
                ("RowOptions[2].Value", "Dollar"),
                ("RowOptions[2].Name", "$"),
                ("RowOptions[2].Desc", "false"),
                ("__RequestVerificationToken", token),
                # Hidden default-false for unchecked ColumnOptions. ASP.NET's
                # checkbox binder expects both a "true" (if checked) and a
                # default "false" field; order matters — browser sends "false"
                # defaults after the primary values.
                ("ColumnOptions[0].Checked", "false"),
                ("ColumnOptions[1].Checked", "false"),
                ("ColumnOptions[2].Checked", "false"),
                ("ColumnOptions[3].Checked", "false"),
                ("ColumnOptions[4].Checked", "false"),
                ("ColumnOptions[5].Checked", "false"),
            ]
        )
        return urlencode(pairs)
