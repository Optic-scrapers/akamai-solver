import asyncio
import json
import os
import platform
import time
from dataclasses import asdict

import redis.asyncio as redis

from solver import solve
from utils import log, send_heartbeat

SOLVER_NAME = os.getenv("SOLVER_NAME", "akamai").strip().lower()
MAX_UPTIME_SECONDS = int(os.getenv("MAX_UPTIME_SECONDS", 3600))
REQUEST_CLAIM_IDLE_SECONDS = int(os.getenv("REQUEST_CLAIM_IDLE_SECONDS", 300))

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASS = os.getenv("REDIS_PASS")

PRIORITY_LEVELS = ["high", "normal", "low"]
REQUEST_STREAMS = [f"{SOLVER_NAME}:requests:{level}" for level in PRIORITY_LEVELS]
REQUEST_GROUP = f"{SOLVER_NAME}:solvers"
CONSUMER_NAME = f"{platform.node()}:{os.getpid()}"
REQUEST_CLAIM_IDLE_MS = max(REQUEST_CLAIM_IDLE_SECONDS, 1) * 1000
STREAM_TRIM_EVERY_ACKS = 100
REQUEST_ACK_COUNT = 0

DB = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    password=REDIS_PASS,
    socket_keepalive=True,
    socket_connect_timeout=5,
    socket_timeout=30,
    health_check_interval=30,
)


def encode_stream_fields(payload: dict) -> dict[str, str]:
    fields = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple)):
            fields[key] = json.dumps(value)
            continue
        fields[key] = str(value)
    return fields


def decode_stream_fields(fields: dict) -> dict:
    decoded = dict(fields)
    for key, value in list(decoded.items()):
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if not stripped or stripped[0] not in "[{":
            continue
        try:
            decoded[key] = json.loads(value)
        except json.JSONDecodeError:
            continue
    return decoded


async def ensure_request_groups() -> None:
    for stream in REQUEST_STREAMS:
        try:
            await DB.xgroup_create(stream, REQUEST_GROUP, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise


def pending_count(pending) -> int:
    if isinstance(pending, dict):
        return int(pending.get("pending") or pending.get("count") or 0)
    if isinstance(pending, (list, tuple)) and pending:
        return int(pending[0] or 0)
    return 0


def last_delivered_id(groups, group: str) -> str | None:
    for group_info in groups:
        name = group_info.get("name")
        if isinstance(name, bytes):
            name = name.decode()
        if name != group:
            continue
        last_id = group_info.get("last-delivered-id")
        if isinstance(last_id, bytes):
            last_id = last_id.decode()
        return last_id
    return None


async def trim_stream(stream: str) -> None:
    pending = await DB.xpending(stream, REQUEST_GROUP)
    if pending_count(pending):
        return
    last_id = last_delivered_id(await DB.xinfo_groups(stream), REQUEST_GROUP)
    if not last_id or last_id == "0-0":
        return
    await DB.xtrim(stream, minid=last_id, approximate=True)


async def trim_acknowledged_requests_if_due() -> None:
    global REQUEST_ACK_COUNT
    REQUEST_ACK_COUNT += 1
    if REQUEST_ACK_COUNT % STREAM_TRIM_EVERY_ACKS:
        return
    for stream in REQUEST_STREAMS:
        try:
            await trim_stream(stream)
        except Exception:
            log.exception("stream_trim_failed", stream=stream, group=REQUEST_GROUP)


async def publish_result(reply_to: str, payload: dict) -> None:
    await DB.xadd(reply_to, encode_stream_fields(payload))


async def publish_terminal_error(reply_to: str, request_id: str | None, message: str) -> None:
    payload = {
        "status": "error",
        "error": "solver_error",
        "message": message,
    }
    if request_id is not None:
        payload["request_id"] = request_id
    await publish_result(reply_to, payload)


async def process_request(stream: str, stream_id: str, fields: dict) -> bool:
    payload = decode_stream_fields(fields)
    log.debug("received", stream=stream, stream_id=stream_id, msg=payload)
    reply_to = payload.get("reply_to")
    request_id = payload.get("request_id")
    challenge_url = payload.get("challenge_url")
    proxy = payload.get("proxy")
    if not reply_to:
        log.warning("invalid_message", stream=stream, stream_id=stream_id, msg=payload)
        await DB.xack(stream, REQUEST_GROUP, stream_id)
        await trim_acknowledged_requests_if_due()
        return False
    if not challenge_url:
        await publish_terminal_error(reply_to, request_id, "missing challenge_url")
        await DB.xack(stream, REQUEST_GROUP, stream_id)
        await trim_acknowledged_requests_if_due()
        return False
    try:
        session = await asyncio.wait_for(
            solve(challenge_url, proxy, solver_name=SOLVER_NAME),
            timeout=90,
        )
        response = {"status": "ok", **asdict(session)}
        if request_id is not None:
            response["request_id"] = request_id
        await publish_result(reply_to, response)
        await DB.xack(stream, REQUEST_GROUP, stream_id)
        await trim_acknowledged_requests_if_due()
        await asyncio.to_thread(send_heartbeat, SOLVER_NAME)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await publish_terminal_error(reply_to, request_id, str(e))
        await DB.xack(stream, REQUEST_GROUP, stream_id)
        await trim_acknowledged_requests_if_due()
        log.exception(
            "request_failed",
            exception=str(e),
            reply_to=reply_to,
            request_id=request_id,
            solver=SOLVER_NAME,
            stream=stream,
            stream_id=stream_id,
        )
        return False


def extract_stream_entries(result) -> list[tuple[str, str, dict]]:
    entries = []
    if not result:
        return entries
    for stream, messages in result:
        for stream_id, fields in messages:
            entries.append((stream, stream_id, fields))
    return entries


def extract_claimed_entries(stream: str, result) -> list[tuple[str, str, dict]]:
    if not result or len(result) < 2:
        return []
    messages = result[1]
    if not messages:
        return []
    return [(stream, stream_id, fields) for stream_id, fields in messages]


async def claim_stale_request(stream: str) -> tuple[str, str, dict] | None:
    result = await DB.xautoclaim(
        stream,
        REQUEST_GROUP,
        CONSUMER_NAME,
        REQUEST_CLAIM_IDLE_MS,
        start_id="0-0",
        count=1,
    )
    entries = extract_claimed_entries(stream, result)
    return entries[0] if entries else None


async def read_new_request(stream: str) -> tuple[str, str, dict] | None:
    result = await DB.xreadgroup(
        REQUEST_GROUP,
        CONSUMER_NAME,
        {stream: ">"},
        count=1,
    )
    entries = extract_stream_entries(result)
    return entries[0] if entries else None


async def next_request() -> tuple[str, str, dict] | None:
    for stream in REQUEST_STREAMS:
        entry = await claim_stale_request(stream)
        if entry:
            return entry
    for stream in REQUEST_STREAMS:
        entry = await read_new_request(stream)
        if entry:
            return entry
    await asyncio.sleep(1)
    return None


async def run() -> None:
    await ensure_request_groups()
    started_at = time.monotonic()
    log.info(
        "started",
        streams=REQUEST_STREAMS,
        group=REQUEST_GROUP,
        consumer=CONSUMER_NAME,
        solver=SOLVER_NAME,
        max_uptime=MAX_UPTIME_SECONDS,
    )
    while time.monotonic() - started_at < MAX_UPTIME_SECONDS:
        try:
            entry = await next_request()
            if not entry:
                continue
            stream, stream_id, fields = entry
            await process_request(stream, stream_id, fields)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("request_loop_failed", exception=str(e), solver=SOLVER_NAME)
            await asyncio.sleep(1)
    log.info(
        "uptime_limit_reached",
        uptime=int(time.monotonic() - started_at),
        max_uptime=MAX_UPTIME_SECONDS,
        solver=SOLVER_NAME,
    )


if __name__ == "__main__":
    asyncio.run(run())

