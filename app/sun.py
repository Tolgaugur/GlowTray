"""Sunrise / sunset calculation (NOAA 'Almanac for Computers' method)."""
import math
from datetime import date, datetime, timedelta, timezone


def _calc(day, lat, lon, rising, zenith=90.833):
    n = day.timetuple().tm_yday
    lng_hour = lon / 15
    t = n + ((6 if rising else 18) - lng_hour) / 24
    m = 0.9856 * t - 3.289
    l = (m + 1.916 * math.sin(math.radians(m)) + 0.020 * math.sin(math.radians(2 * m)) + 282.634) % 360
    ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l)))) % 360
    ra = (ra + math.floor(l / 90) * 90 - math.floor(ra / 90) * 90) / 15
    sin_dec = 0.39782 * math.sin(math.radians(l))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = (math.cos(math.radians(zenith)) - sin_dec * math.sin(math.radians(lat))) / (
        cos_dec * math.cos(math.radians(lat)))
    if not -1 <= cos_h <= 1:
        return None
    h = (360 - math.degrees(math.acos(cos_h))) if rising else math.degrees(math.acos(cos_h))
    ut = (h / 15 + ra - 0.06571 * t - 6.622 - lng_hour) % 24
    base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    result = (base + timedelta(hours=ut)).astimezone().replace(tzinfo=None)
    # Fix up if the UTC conversion shifted the result to the previous/next day
    if result.date() != day:
        result = datetime.combine(day, result.time())
    return result


def sun_times(lat, lon, day=None):
    day = day or date.today()
    return _calc(day, lat, lon, True), _calc(day, lat, lon, False)


def is_dark(lat, lon, now=None):
    now = now or datetime.now()
    rise, set_ = sun_times(lat, lon, now.date())
    if rise is None or set_ is None:
        return False
    return now < rise or now >= set_
