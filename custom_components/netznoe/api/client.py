"""Netz NO Smartmeter API Client."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib import parse

import requests

from . import constants as const
from .errors import (
    SmartmeterConnectionError,
    SmartmeterLoginError,
    SmartmeterQueryError,
)

logger = logging.getLogger(__name__)


@dataclass
class ConsumptionDay:
    """One day of interval readings.

    `metered` is the total consumption. For members of an energy community the
    API splits that total into `self_coverage`, the share covered by the
    community, and `grid_leftover`, the remainder drawn from the public grid.
    That split is published later than the consumption itself, so the two
    lists can be empty or hold None entries for intervals it is missing for.
    """

    times: list[str] = field(default_factory=list)
    metered: list[float | None] = field(default_factory=list)
    grid_leftover: list[float | None] = field(default_factory=list)
    self_coverage: list[float | None] = field(default_factory=list)

    def self_coverage_at(self, index: int) -> float | None:
        """Return the community share for an interval, if it is published."""
        if index < len(self.self_coverage):
            return self.self_coverage[index]
        return None

    def grid_leftover_at(self, index: int) -> float | None:
        """Return the grid share for an interval, if it is published."""
        if index < len(self.grid_leftover):
            return self.grid_leftover[index]
        return None


class Smartmeter:
    """Netz NO Smartmeter client."""

    def __init__(self, username: str, password: str):
        """Initialize the Smartmeter API client.

        Args:
            username: Username for Netz NO portal
            password: Password for Netz NO portal

        """
        self.username = username
        self.password = password
        self.session = requests.Session()
        self._is_authenticated = False

        # Account info from API
        self._account_id: str | None = None
        self._metering_point_id: str | None = None
        self._has_smart_meter: bool = False
        self._has_communicative: bool = False
        self._has_active: bool = False
        self._metering_points_cache: list[dict[str, Any]] = []

    def reset(self):
        """Reset session and authentication state."""
        self.session = requests.Session()
        self._is_authenticated = False
        self._account_id = None
        self._metering_point_id = None
        self._metering_points_cache = []

    def is_logged_in(self) -> bool:
        """Check if user is currently logged in."""
        return self._is_authenticated

    def _validate_session(self) -> bool:
        """Validate if current session is still valid."""
        if not self._is_authenticated:
            return False
        try:
            response = self.session.get(
                parse.urljoin(const.BASE_URL, const.ENDPOINT_USER_INFO)
            )
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def login(self) -> Smartmeter:
        """Authenticate with Netz NO API.

        Returns:
            Self for method chaining

        Raises:
            SmartmeterLoginError: If authentication fails
            SmartmeterConnectionError: If connection fails

        """
        if self._is_authenticated and self._validate_session():
            return self

        logger.debug("Attempting login to %s", const.AUTH_URL)
        try:
            response = self.session.post(
                const.AUTH_URL, json={"user": self.username, "pwd": self.password}
            )
        except requests.exceptions.RequestException as e:
            logger.error("Connection error during login: %s", e)
            raise SmartmeterConnectionError("Could not connect to Netz NO API") from e

        logger.debug("Login response status: %s", response.status_code)
        logger.debug("Login response headers: %s", dict(response.headers))
        try:
            logger.debug("Login response body: %s", response.text[:500])
        except Exception:
            pass

        if response.status_code != 200:
            raise SmartmeterLoginError(
                f"Login failed (HTTP {response.status_code}). Check username/password."
            )

        self._is_authenticated = True
        self._load_account_info()
        return self

    def _load_account_info(self):
        """Load account and metering point information after login."""
        # Get metering points (returns JSON array)
        response = self._call_api(const.ENDPOINT_METERING_POINTS)
        logger.debug("Metering points response: %s", response)

        if response and len(response) > 0:
            metering_point = response[0]
            self._metering_point_id = metering_point.get("meteringPointId")
            self._account_id = metering_point.get("accountId")
            self._has_smart_meter = metering_point.get("smartMeterType") is not None
            self._has_communicative = metering_point.get("communicative", False)
            self._has_active = not metering_point.get("locked", False)
            # Store full response for get_metering_points()
            self._metering_points_cache = response

    def _call_api(
        self,
        endpoint: str,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> Any:
        """Make API call to Netz NO endpoint.

        Args:
            endpoint: API endpoint (relative to BASE_URL)
            method: HTTP method
            query: Query parameters
            timeout: Request timeout

        Returns:
            JSON response from API

        Raises:
            SmartmeterConnectionError: If request fails
            SmartmeterQueryError: If response is invalid

        """
        if not self._is_authenticated:
            raise SmartmeterConnectionError("Not authenticated. Call login() first.")

        url = parse.urljoin(const.BASE_URL, endpoint)

        if query:
            url += ("?" if "?" not in endpoint else "&") + parse.urlencode(query)

        try:
            response = self.session.request(method, url, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if response.status_code == 401:
                self._is_authenticated = False
                raise SmartmeterLoginError("Session expired") from e
            raise SmartmeterQueryError(f"API error: {e}") from e
        except requests.exceptions.RequestException as e:
            raise SmartmeterConnectionError(f"Request failed: {e}") from e

    @property
    def account_id(self) -> str | None:
        """Return the account ID."""
        return self._account_id

    @property
    def metering_point_id(self) -> str | None:
        """Return the metering point ID."""
        return self._metering_point_id

    def get_metering_points(self) -> list[dict[str, Any]]:
        """Get all metering points for the account.

        Returns:
            List of metering point dictionaries

        """
        return self._metering_points_cache

    def get_account_info(self) -> dict[str, Any]:
        """Get account information.

        Returns:
            Dictionary with account info

        """
        return {
            "accountId": self._account_id,
            "meteringPointId": self._metering_point_id,
            "hasSmartMeter": self._has_smart_meter,
            "hasCommunicative": self._has_communicative,
            "hasActive": self._has_active,
        }

    def get_consumption_day(
        self, day: date, meter_id: str | None = None
    ) -> tuple[list[str], list[float]]:
        """Get daily consumption data.

        Args:
            day: Date to get consumption for
            meter_id: Metering point ID (uses default if None)

        Returns:
            Tuple of (peak_demand_times, metered_values)

        """
        consumption = self.get_consumption_day_series(day, meter_id)
        return (consumption.times, consumption.metered)

    def get_consumption_day_series(
        self, day: date, meter_id: str | None = None
    ) -> ConsumptionDay:
        """Get daily consumption data including the energy community split.

        Args:
            day: Date to get consumption for
            meter_id: Metering point ID (uses default if None)

        Returns:
            The day's interval readings

        """
        meter_id = meter_id or self._metering_point_id
        if not meter_id:
            raise SmartmeterQueryError("No metering point ID available")

        response = self._call_api(
            const.ENDPOINT_CONSUMPTION_DAY,
            query={"meterId": meter_id, "day": day.strftime(const.API_DATE_FORMAT)},
        )
        logger.debug("ConsumptionDay raw response: %s", response)

        # API may return multiple entries for energy community users.
        # We want the entry with ec_id == null (the base meter, not community allocations).
        if isinstance(response, list) and len(response) > 0:
            base_entries = [r for r in response if r.get("ec_id") is None]
            response = base_entries[0] if base_entries else response[0]
        elif isinstance(response, list):
            return ConsumptionDay()

        return ConsumptionDay(
            times=response.get("peakDemandTimes") or [],
            metered=response.get("meteredValues") or [],
            grid_leftover=response.get("gridUsageLeftoverValues") or [],
            self_coverage=response.get("selfCoverageValues") or [],
        )

    def get_consumption_month(
        self, year: int, month: int, meter_id: str | None = None
    ) -> tuple[list[str], list[float]]:
        """Get monthly consumption data.

        Args:
            year: Year
            month: Month (1-12)
            meter_id: Metering point ID (uses default if None)

        Returns:
            Tuple of (peak_demand_times, metered_values)

        """
        meter_id = meter_id or self._metering_point_id
        if not meter_id:
            raise SmartmeterQueryError("No metering point ID available")

        response = self._call_api(
            const.ENDPOINT_CONSUMPTION_MONTH,
            query={"meterId": meter_id, "year": year, "month": month},
        )

        # API may return multiple entries for energy community users.
        # We want the entry with ec_id == null (the base meter, not community allocations).
        if isinstance(response, list) and len(response) > 0:
            base_entries = [r for r in response if r.get("ec_id") is None]
            response = base_entries[0] if base_entries else response[0]
        elif isinstance(response, list):
            return ([], [])

        return (
            response.get("peakDemandTimes", []),
            response.get("meteredValues", []),
        )

    def get_consumption_year(
        self, year: int, meter_id: str | None = None
    ) -> tuple[list[str], list[float]]:
        """Get yearly consumption data.

        Args:
            year: Year
            meter_id: Metering point ID (uses default if None)

        Returns:
            Tuple of (peak_demand_times, values)

        """
        meter_id = meter_id or self._metering_point_id
        if not meter_id:
            raise SmartmeterQueryError("No metering point ID available")

        response = self._call_api(
            const.ENDPOINT_CONSUMPTION_YEAR,
            query={"meterId": meter_id, "year": year},
        )

        # API returns a list containing a dictionary - extract first element
        if isinstance(response, list) and len(response) > 0:
            response = response[0]
        elif isinstance(response, list):
            return ([], [])

        return (
            response.get("peakDemandTimes", []),
            response.get("values", []),
        )

    def get_historical_consumption(
        self,
        start_date: date,
        end_date: date | None = None,
        meter_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get historical consumption data day by day.

        This fetches daily consumption for each day in the range.

        Args:
            start_date: Start date
            end_date: End date (defaults to today)
            meter_id: Metering point ID

        Returns:
            List of daily consumption records

        """
        if end_date is None:
            end_date = date.today()

        meter_id = meter_id or self._metering_point_id
        results = []

        current = start_date
        while current <= end_date:
            try:
                times, values = self.get_consumption_day(current, meter_id)
                results.append(
                    {
                        "date": current.isoformat(),
                        "peakDemandTimes": times,
                        "meteredValues": values,
                    }
                )
            except SmartmeterQueryError as e:
                logger.warning(f"Could not fetch data for {current}: {e}")
            current = current + timedelta(days=1)

        return results
