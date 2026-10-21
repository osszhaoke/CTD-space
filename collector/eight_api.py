"""Read-only adapter for lukas-clarke/eight_sleep, commit 1f5d84e.

OAuth constants and endpoint/query conventions come from its MIT-licensed
pyEight client. See LICENSE.eight_sleep. Never invoke its bed control methods.
"""

import asyncio
import math
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

CLIENT = "https://client-api.8slp.net/v1"
APP = "https://app-api.8slp.net"
AUTH = "https://auth-api.8slp.net/v1/tokens"
CLIENT_ID = "0894c7f33bb94800a03f1f4df13a4f38"
CLIENT_SECRET = "f0954a3ed5763ba3d06834c73731a32f15f168f47d4f164751275def86db0c76"
TIMEOUT = 30
INTERVAL = 10


class APIError(Exception):
    """Deliberately excludes response bodies, URLs, credentials and tokens."""

    def __init__(self, status=0, *, retry_after=INTERVAL, code=None):
        self.status = status
        self.retry_after = retry_after
        self.code = code
        label = f"HTTP {status}" if status else "网络超时或响应无效"
        super().__init__(f"{label} · {code}" if code else label)


def redact(value):
    """Keep measurement fields intact; authentication material is never archived."""
    if isinstance(value, dict):
        return {
            key: redact(item)
            for key, item in value.items()
            if not any(word in key.lower().replace("_", "").replace("-", "")
                       for word in ("password", "token", "secret", "authorization", "apikey"))
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def identifier(value):
    return quote(str(value), safe="")


def retry_delay(response):
    value = response.headers.get("retry-after", "")
    minimum = 300 if response.status_code == 429 else INTERVAL
    try:
        return max(minimum, float(value))
    except ValueError:
        try:
            return max(minimum, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return minimum


class EightAPI:
    def __init__(self, email, password, http):
        self.email, self.password, self.http = email, password, http
        self.token = None
        self.expires = 0
        self.auth_lock = asyncio.Lock()

    async def _request(self, method, url, **kwargs):
        try:
            async with asyncio.timeout(TIMEOUT):
                response = await self.http.request(method, url, timeout=TIMEOUT, **kwargs)
            if response.status_code >= 300:
                code = None
                try:
                    detail = response.json()
                    reported = detail.get("errorType") if isinstance(detail, dict) else None
                    if reported in {"PodOffline", "NoPairedPod", "NoPairedBase", "NoPairedSpeaker", "SubscriptionRequired"}:
                        code = reported
                except ValueError:
                    pass
                raise APIError(response.status_code, retry_after=retry_delay(response), code=code)
            return response.json()
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise APIError() from None

    async def authenticate(self):
        data = await self._request("POST", AUTH, json={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "grant_type": "password", "username": self.email, "password": self.password,
        })
        try:
            self.token = data["access_token"]
            self.expires = time.monotonic() + float(data["expires_in"]) - 120
        except (KeyError, TypeError, ValueError):
            raise APIError() from None

    async def get(self, url, params=None):
        # Only fixed upstream API hosts; redirects are disabled by the HTTP client.
        if not (url.startswith(CLIENT + "/") or url.startswith(APP + "/")):
            raise ValueError("Unsupported Eight Sleep endpoint")
        async with self.auth_lock:
            if not self.token or time.monotonic() >= self.expires:
                await self.authenticate()
        for attempt in range(2):
            token = self.token
            try:
                return redact(await self._request(
                    "GET", url, params=params,
                    headers={"Authorization": f"Bearer {token}"},
                ))
            except APIError as error:
                if error.status != 401 or attempt:
                    raise
                async with self.auth_lock:
                    if self.token == token:
                        await self.authenticate()

    async def trends(self, user_id, tz, start, end):
        return await self.get(f"{CLIENT}/users/{identifier(user_id)}/trends", {
            "tz": tz, "from": start.isoformat(), "to": end.isoformat(),
            "include-main": "false", "include-all-sessions": "true", "model-version": "v2",
        })


def user_ids(device):
    return {
        value for value in (
            device.get("leftUserId"), device.get("rightUserId"),
            *(device.get("awaySides") or {}).values(),
        ) if isinstance(value, str) and value
    }


async def concurrent_reads(*jobs):
    """Bounded reads finish or are cancelled before another poll can begin."""
    tasks = [asyncio.create_task(job) for job in jobs]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def snapshot(api):
    """Collect all endpoints with at most eight independent requests in flight."""
    data, errors = {}, {}
    limit = asyncio.Semaphore(8)

    async def read(key, url=None, params=None, fetch=None):
        try:
            async with limit:
                result = await (fetch() if fetch else api.get(url, params))
            data[key] = result
            return result
        except APIError as error:
            if error.status in (401, 429):
                raise
            errors[key] = str(error)
            return {}

    account = await api.get(f"{CLIENT}/users/me")
    data["account"] = account
    me = account.get("user") or {}
    main_id = me.get("userId")
    if not main_id:
        raise APIError()
    household = await read("household", f"{APP}/v1/household/users/{identifier(main_id)}/summary")
    devices = {d for d in (me.get("devices") or []) if isinstance(d, str)}
    users = {main_id}
    for home in household.get("households") or []:
        for user in home.get("users") or []:
            if isinstance(user, dict) and user.get("userId"):
                users.add(user["userId"])
        for group in home.get("sets") or []:
            for device in group.get("devices") or []:
                if device.get("deviceId"):
                    devices.add(device["deviceId"])

    async def read_device(device_id):
        result = await read(f"device/{device_id}", f"{CLIENT}/devices/{identifier(device_id)}")
        users.update(user_ids(result.get("result") or {}))

    await concurrent_reads(*(read_device(device_id) for device_id in sorted(devices)))
    profiles = {}

    async def read_profile(uid):
        profiles[uid] = me if uid == main_id else (
            await read(f"user/{uid}/profile", f"{CLIENT}/users/{identifier(uid)}")
        ).get("user") or {}

    await concurrent_reads(*(read_profile(uid) for uid in sorted(users)))
    jobs = [
        read(f"device/{device_id}/peripherals", f"{CLIENT}/devices/{identifier(device_id)}/peripherals")
        for device_id in sorted(devices)
    ]
    jobs.append(read("audio_categories", f"{APP}/v1/audio/categories"))
    today = datetime.now(timezone.utc).date()
    for uid in sorted(users):
        key, path = f"user/{uid}", f"users/{identifier(uid)}"
        jobs.append(read(key + "/current_device", f"{CLIENT}/{path}/current-device"))
        for name, suffix in (
            ("temperature", "temperature"), ("temperature_all", "temperature/all"),
            ("base", "base"), ("audio_player", "audio/player"),
            ("base_presets", "base/presets"), ("audio_tracks", "audio/tracks"),
            ("audio_favorites", "audio/tracks/favorites"),
        ):
            jobs.append(read(f"{key}/{name}", f"{APP}/v1/{path}/{suffix}"))
        jobs.append(read(key + "/alarms", f"{APP}/v2/{path}/alarms"))
        tz = (profiles[uid].get("currentDevice") or {}).get("timeZone") or "UTC"
        jobs.append(read(key + "/trends", fetch=lambda uid=uid, tz=tz: api.trends(
            uid, tz, today - timedelta(days=2), today + timedelta(days=2))))
    await concurrent_reads(*jobs)
    return {"responses": data, "errors": errors, "main_user_id": main_id}, profiles


def history_start(profile):
    # Backfill to account creation; this also covers long gaps without sleep.
    try:
        return date.fromisoformat(str(profile["createdAt"])[:10])
    except (KeyError, ValueError):
        return date(2014, 1, 1)



# Same nearest-entry conversion as upstream pyEight/util.py and constants.py.
# These are approximate temperatures derived from device sensor levels, not
# the historical tempBedC sleep-session measurements.
RAW_TO_CELSIUS = {
    -100: 13, -97: 14, -94: 15, -91: 16, -83: 17, -75: 18, -67: 19,
    -58: 20, -50: 21, -42: 22, -33: 23, -25: 24, -17: 25, -8: 26,
    0: 27, 6: 28, 11: 29, 17: 30, 22: 31, 28: 32, 33: 33, 39: 34,
    44: 35, 50: 36, 56: 37, 61: 38, 67: 39, 72: 40, 78: 41,
    83: 42, 89: 43, 100: 44,
}


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def bed_temperature(level):
    if not finite_number(level) or not -100 <= level <= 100:
        return None
    return RAW_TO_CELSIUS[min(RAW_TO_CELSIUS, key=lambda raw: abs(raw - level))]


def current_bed_measurements(responses, uid):
    current = responses.get(f"user/{uid}/current_device") or {}
    profile = (responses.get("account") or {}).get("user") or {}
    device_id = current.get("id") or (profile.get("currentDevice") or {}).get("id")
    devices = {
        key.removeprefix("device/"): value.get("result") or {}
        for key, value in responses.items()
        if key.startswith("device/") and key.count("/") == 1 and isinstance(value, dict)
    }
    # Do not replace the user's current bed with a pillow or another household bed.
    device = devices.get(device_id, {}) if device_id else next(iter(devices.values()), {}) if len(devices) == 1 else {}
    result = {"hub_online": device.get("online")}
    if device.get("online") is True and device.get("isTemperatureAvailable") is not False:
        for side in ("left", "right"):
            level = device.get(f"{side}HeatingLevel")
            value = bed_temperature(level)
            if value is not None:
                result[f"{side}BedTempC"] = value
                result[f"{side}BedLevel"] = level
        temperature = responses.get(f"user/{uid}/temperature") or {}
        value = bed_temperature(temperature.get("currentDeviceLevel"))
        if value is not None:
            result["currentBedTempC"] = value

    # Successful base/audio replies supply their current readings, including zero.
    if device.get("online") is not False:
        base = responses.get(f"user/{uid}/base") or {}
        for side in ("left", "right"):
            for part in ("torso", "leg"):
                value = ((base.get(side) or {}).get(part) or {}).get("currentAngle")
                if finite_number(value):
                    result[f"{side}{part.title()}Angle"] = value
        audio = responses.get(f"user/{uid}/audio_player") or {}
        volume = audio.get("volume")
        if finite_number(volume):
            result["audioVolume"] = volume
        if isinstance(audio.get("state"), str):
            result["audio_state"] = audio["state"]
    return result


def measurements(payload):
    """Small preview only. All users, samples, stages and nulls stay in responses."""
    uid = payload["main_user_id"]
    responses = payload["responses"]
    days = (responses.get(f"user/{uid}/trends") or {}).get("days") or []
    days = sorted(days, key=lambda day: day.get("day") or day.get("date") or "")
    latest = next((day for day in reversed(days) if day.get("sessions") or day.get("score") is not None), {})
    result = {}
    for key in ("score", "sleepScore", "sleepFitnessScore"):
        value = latest.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result["sleep_score"] = value
            break
    result["sleep_day"] = latest.get("day") or latest.get("date")
    sessions = latest.get("sessions") or [{}]
    timeseries = sessions[-1].get("timeseries") or {}
    for key in ("heartRate", "respiratoryRate", "hrv", "tempBedC", "tempRoomC"):
        points = timeseries.get(key) or []
        if points and isinstance(points[-1], list) and len(points[-1]) >= 2:
            value = points[-1][1]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = value
    quality = latest.get("sleepQualityScore")
    if isinstance(quality, dict):
        for key in ("hrv", "respiratoryRate"):
            part = quality.get(key)
            if key not in result and isinstance(part, dict):
                value = part.get("current")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    result[key] = value
    for key in ("sleepDuration", "presenceDuration", "lightDuration", "deepDuration", "remDuration", "tnt"):
        value = latest.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = value
    temperature = responses.get(f"user/{uid}/temperature") or {}
    for key in ("currentLevel", "currentDeviceLevel"):
        value = temperature.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = value
    result["bed_state"] = (temperature.get("currentState") or {}).get("type")
    result.update(current_bed_measurements(responses, uid))
    result["base_status"] = payload["errors"].get(f"user/{uid}/base", "已读取")
    result["audio_status"] = payload["errors"].get(f"user/{uid}/audio_player", "已读取")
    return result
