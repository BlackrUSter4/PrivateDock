import asyncio
import uuid
from typing import Optional

from src.connection.client import Client
from src.logger.logger import log_event, LOG_LEVEL_WARN
from src.protobuf import protobuf
from src.db.async_lock import create_locked_task

from .helpers import ChargeSuccessEvent, apply_charge_success_event


_MOCK_PAY_PREFIX = "mock"


def _parse_drop_rows(raw) -> list[tuple[int, int, int]]:
    """Normalize pay_data_display drop fields into (type, id, count) rows."""
    if not isinstance(raw, list):
        return []
    if len(raw) >= 3 and all(isinstance(value, (int, float)) for value in raw[:3]):
        return [(int(raw[0]), int(raw[1]), int(raw[2]))]

    rows = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        if not all(isinstance(value, (int, float)) for value in entry[:3]):
            continue
        rows.append((int(entry[0]), int(entry[1]), int(entry[2])))
    return rows


def _has_auto_open_container(rows: list[tuple[int, int, int]]) -> bool:
    from src.orm.item import _load_virtual_item_config

    for drop_type, drop_id, _count in rows:
        if drop_type != 2:
            continue
        config = _load_virtual_item_config(drop_id)
        if config is not None and int(config.get("open_directly") or 0) == 1:
            return True
    return False


def _build_charge_event(item: dict, pay_id: str, first_purchase: bool) -> ChargeSuccessEvent:
    shop_id = int(item.get("id") or 0)
    gem = int(item.get("gem") or 0)
    gem_free = int(item.get("extra_gem") or 0)
    drops: dict[tuple[int, int], int] = {}
    resource_rewards: dict[tuple[int, int], int] = {}

    drop_rows = _parse_drop_rows(item.get("drop_item"))
    # Lucky bags are open_directly containers: add_item resolves their full
    # contents. Do not also apply display/extra_service_item or they double.
    keys = ("drop_item",) if _has_auto_open_container(drop_rows) else (
        "drop_item", "extra_service_item", "display",
    )

    # display and extra_service_item overlap heavily. Fold gem resources into
    # SC_11503's gem fields and take the max count for every other reward.
    for key in keys:
        for drop_type, drop_id, count in _parse_drop_rows(item.get(key)):
            if drop_type == 1 and drop_id == 4:
                reward_key = (drop_type, drop_id)
                resource_rewards[reward_key] = max(resource_rewards.get(reward_key, 0), count)
            elif drop_type == 1 and drop_id == 14:
                reward_key = (drop_type, drop_id)
                resource_rewards[reward_key] = max(resource_rewards.get(reward_key, 0), count)
            else:
                drop_key = (drop_type, drop_id)
                drops[drop_key] = max(drops.get(drop_key, 0), count)

    gem += resource_rewards.get((1, 4), 0)
    gem_free += resource_rewards.get((1, 14), 0)

    if first_purchase and int(item.get("first_pay_double") or 0) != 0:
        gem *= 2

    return ChargeSuccessEvent(
        shop_id=shop_id,
        pay_id=pay_id,
        gem=gem,
        gem_free=gem_free,
        drops=[(drop_type, drop_id, count) for (drop_type, drop_id), count in drops.items()],
    )


def _load_charge_item(shop_id: int) -> Optional[dict]:
    from src.orm.config_entry import fetch_config_entry_data
    item = fetch_config_entry_data("ShareCfg/pay_data_display.json", shop_id)
    return item if isinstance(item, dict) else None


def _new_mock_pay_id(shop_id: int) -> str:
    return f"{_MOCK_PAY_PREFIX}:{shop_id}:{uuid.uuid4().hex}"


def _shop_id_from_pay_id(pay_id: str) -> int:
    parts = (pay_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] != _MOCK_PAY_PREFIX:
        return 0
    try:
        return int(parts[1])
    except (TypeError, ValueError):
        return 0


async def _send_start_result(client: Client, result: int, pay_id: str = "") -> None:
    response = protobuf.SC_11502(result=result, pay_id=pay_id, url="", order_sign="")
    await client.send_message(11502, response)


async def _do_mock_charge(client: Client, shop_id: int) -> None:
    item = _load_charge_item(shop_id)
    if item is None or client.commander is None:
        await _send_start_result(client, 1)
        return

    commander_id = client.commander.commander_id
    pay_id = _new_mock_pay_id(shop_id)
    try:
        from src.answer.shopping_command_answer import offer_claim_total_count
        first_purchase = await offer_claim_total_count(commander_id, shop_id) == 0
    except Exception:
        first_purchase = False

    event = _build_charge_event(item, pay_id, first_purchase)
    error, applied = await apply_charge_success_event(commander_id, client, event)
    if error is not None or not applied:
        reason = error or "duplicate pay_id"
        log_event("Charge", "MockStart", f"shop={shop_id} pay_id={pay_id} failed: {reason}", LOG_LEVEL_WARN)
        await _send_start_result(client, 1)
        return

    from src.answer.miscops.handlers import handle_owned_items
    from src.answer.player_resource_sync import send_player_resource_sync
    send_player_resource_sync(client)
    if event.drops:
        handle_owned_items(b"", client)

    # result=1 is the only charge error code the client handles silently.
    # SC_11503 above already updated PlayerProxy/ShopsProxy and granted the
    # pack, so this only completes the queued CS_11501 without starting SdkPay.
    await _send_start_result(client, 1, pay_id)


async def _do_mock_confirm(client: Client, pay_id: str) -> None:
    shop_id = _shop_id_from_pay_id(pay_id)
    item = _load_charge_item(shop_id)
    if item is None or client.commander is None:
        response = protobuf.SC_11505(result=1)
        await client.send_message(11505, response)
        return

    commander_id = client.commander.commander_id
    try:
        from src.answer.shopping_command_answer import offer_claim_total_count
        first_purchase = await offer_claim_total_count(commander_id, shop_id) == 0
    except Exception:
        first_purchase = False

    event = _build_charge_event(item, pay_id, first_purchase)
    error, applied = await apply_charge_success_event(commander_id, client, event)
    response = protobuf.SC_11505(
        result=0 if error is None and applied else 1,
        shop_id=shop_id,
        gem=event.gem,
        gem_free=event.gem_free,
    )
    await client.send_message(11505, response)


def handle_charge_start(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        payload = protobuf.CS_11501.FromString(buffer)
    except Exception:
        create_locked_task(_send_start_result(client, 1))
        return 0, 11502, None

    shop_id = int(payload.shop_id or 0)
    if shop_id <= 0:
        create_locked_task(_send_start_result(client, 1))
        return 0, 11502, None

    create_locked_task(_do_mock_charge(client, shop_id))
    return 0, 11502, None


def handle_charge_confirm(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        payload = protobuf.CS_11504.FromString(buffer)
    except Exception:
        create_locked_task(client.send_message(11505, protobuf.SC_11505(result=1)))
        return 0, 11505, None

    create_locked_task(_do_mock_confirm(client, payload.pay_id))
    return 0, 11505, None


def handle_charge_failure(_buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    response = protobuf.SC_11511()
    response.result = 0
    create_locked_task(client.send_message(11511, response))
    return 0, 11511, None


def handle_refund_charge_start(_buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    response = protobuf.SC_11514()
    response.result = 5002
    response.pay_id = ""
    response.url = ""
    response.order_sign = ""
    create_locked_task(client.send_message(11514, response))
    return 0, 11514, None


def handle_get_charge_list(_buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    create_locked_task(_do_get_charge_list(client))
    return 0, 16105, None


async def _load_charge_items():
    from src.orm.config_entry import afetch_config_entries_data
    rows = await afetch_config_entries_data("ShareCfg/pay_data_display.json")
    return [r for r in rows if isinstance(r, dict)]


async def _do_get_charge_list(client: Client):
    from src.answer.shopping_command_answer import offer_ids, offer_claim_total_count, offer_group_claims
    response = protobuf.SC_16105()
    if client.commander is not None:
        cid = client.commander.commander_id
        for offer_id in offer_ids():
            count = await offer_claim_total_count(cid, offer_id)
            si = protobuf.SHOPINFO(shop_id=offer_id, pay_count=count)
            response.normal_list.append(si)
        for group_id, group_count in await offer_group_claims(cid):
            si = protobuf.SHOPINFO(shop_id=group_id, pay_count=group_count)
            response.normal_group_list.append(si)
        # Real-money / gem charge packs come from pay_data_display and populate
        # the client's getChargedList() (TYPE_CHARGE goods) + first-pay list.
        # Akashi Recommends and the charge shop render these banners from it.
        for item in await _load_charge_items():
            oid = item.get("id")
            if not oid:
                continue
            oid = int(oid)
            count = await offer_claim_total_count(cid, oid)
            response.pay_list.append(protobuf.SHOPINFO(shop_id=oid, pay_count=count))
            if item.get("first_pay_double") == 1:
                # first_pay_list is repeated uint32 (shop ids only), unlike the
                # other three SC_16105 fields which carry SHOPINFO messages.
                response.first_pay_list.append(oid)
    await client.send_message(16105, response)


def handle_get_refund_info(_buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    response = protobuf.SC_11024()
    response.result = 0
    del response.shop_info[:]
    create_locked_task(client.send_message(11024, response))
    return 0, 11024, None
