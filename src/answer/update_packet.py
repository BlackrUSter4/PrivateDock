import asyncio
import json
from typing import Optional

from src.connection.client import Client
from src.protobuf import protobuf
from src.db.async_lock import create_locked_task

PLATFORM_MAP = {"0": "Android", "1": "iOS"}
UPDATE_VERSION_MARKERS = ("count-2", "dTag-1")
_versions: list[str] = []


def _update_versions(hashes_fn) -> list[str]:
    global _versions
    if len(_versions) != 0:
        return _versions
    hashes = hashes_fn()
    _versions = [h["hash"] for h in hashes]
    # The stock client's updater expects both protocol markers after the
    # resource hashes. Omitting count-2 leaves it on "Checking for updates"
    # until its network timeout.
    _versions.extend(UPDATE_VERSION_MARKERS)
    return _versions


def handle_update_check(
    buffer: bytes, client: Client,
) -> tuple[int, int, Optional[Exception]]:
    return _build_update_check_response(buffer, client, _get_game_hashes_with_update)


def handle_gateway_update_check(
    buffer: bytes, client: Client,
) -> tuple[int, int, Optional[Exception]]:
    return _build_update_check_response(buffer, client, _get_game_hashes)


def _get_game_hashes():
    from src.misc import get_game_hashes
    return get_game_hashes()


def _get_game_hashes_with_update():
    from src.misc import get_game_hashes_with_update
    return get_game_hashes_with_update()


def _build_update_check_response(
    buffer: bytes, client: Client, hashes_fn,
) -> tuple[int, int, Optional[Exception]]:
    import time
    packet_id = 10801
    try:
        payload = protobuf.CS_10800()
        payload.ParseFromString(buffer)
    except Exception:
        payload_json = json.loads(buffer.decode("utf-8", errors="replace"))
        platform = payload_json.get("platform", "")
    else:
        platform = payload.platform if payload.HasField("platform") else ""

    _update_versions(hashes_fn)

    from src.region.region import current as get_region
    from src.config.regions import (
        MONDAY_0CLOCK_TIMESTAMPS,
        GAME_PLATFORM_URL,
    )

    region = get_region()
    resolved_platform = PLATFORM_MAP.get(platform, "Unknown")

    url_dict = GAME_PLATFORM_URL.get(region, {})
    url = url_dict.get(platform)
    if url is None:
        return 0, packet_id, ValueError(f"unknown platform '{resolved_platform}' (id='{platform}')")

    sockname = client.writer.get_extra_info("sockname")
    local_ip = sockname[0] if sockname else "127.0.0.1"

    from src.config.config import current as get_config
    cfg = get_config()
    proxy_ip = local_ip
    proxy_port = 20000
    if hasattr(cfg, "servers") and cfg.servers:
        proxy_ip = cfg.servers[0].ip
        proxy_port = cfg.servers[0].port

    response = protobuf.SC_10801(
        gateway_ip=local_ip,
        gateway_port=client.writer.get_extra_info("sockname")[1] if sockname else 80,
        url=url,
        version=_versions,
        proxy_ip=proxy_ip,
        proxy_port=proxy_port,
        is_ts=0,
        timestamp=int(time.time()),
        monday_0oclock_timestamp=MONDAY_0CLOCK_TIMESTAMPS.get(region, 0),
    )
    create_locked_task(client.send_message(packet_id, response))
    return 0, packet_id, None
