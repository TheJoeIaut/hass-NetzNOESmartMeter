"""Component constants for Netz NO Smartmeter."""

DOMAIN = "netznoe"

CONF_METERING_POINTS = "metering_points"
CONF_ENERGY_COMMUNITY = "energy_community"

# Suffixes for the statistic ids and names of the energy community series.
STAT_SUFFIX_SELF_COVERAGE = "eigendeckung"
STAT_SUFFIX_GRID = "restnetzbezug"

# The API publishes a day's energy community split some time after the
# consumption itself, so recent days are imported again on every run to pick
# up values that were still missing the first time round.
ENERGY_COMMUNITY_RESYNC_DAYS = 3


def is_meter_active(metering_point_data: dict) -> bool:
    """Check if a specific metering point is an active smart meter."""
    has_smart = metering_point_data.get("smartMeterType") is not None
    is_active = not metering_point_data.get("locked", False)
    return has_smart and is_active


def has_energy_community(metering_point_data: dict) -> bool:
    """Check if a metering point belongs to an energy community."""
    return bool(metering_point_data.get("energyCommunities"))
