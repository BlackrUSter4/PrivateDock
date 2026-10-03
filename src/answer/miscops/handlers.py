import asyncio
from typing import Optional

from src.connection.client import Client
from src.protobuf import protobuf
from src.db.store import get_default_store
from src.consts.disconnect_reasons import DR_CONNECTION_TO_SERVER_LOST
from src.logger.logger import log_event, LOG_LEVEL_DEBUG, LOG_LEVEL_ERROR, LOG_LEVEL_WARN
from src.db.async_lock import create_locked_task


def handle_cheater_mark(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        payload = protobuf.CS_10994()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 10995, e

    response = protobuf.SC_10995()
    response.result = payload.type
    create_locked_task(client.send_message(10995, response))
    return 0, 10995, None


def handle_give_item(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        payload = protobuf.CS_11202()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 11203, e

    log_event("Server", "AskItem",
              f"uid={client.commander.commander_id} asked for activity_id={payload.activity_id}",
              LOG_LEVEL_DEBUG)

    response = protobuf.SC_11203()
    response.result = 0
    drop = protobuf.DROPINFO()
    drop.type = 2
    drop.id = 20001
    drop.number = 99
    response.award_list.append(drop)
    response.number.append(99)
    create_locked_task(client.send_message(11203, response))
    return 0, 11203, None


def handle_give_resources(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        payload = protobuf.CS_11013()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 11014, e

    if payload.type not in (1, 2):
        response = protobuf.SC_11014()
        response.result = 1
        create_locked_task(client.send_message(11014, response))
        return 0, 11014, None

    field_id = 7 if payload.type == 1 else 5

    import time
    now_unix = int(time.time())
    try:
        from src.answer.playerops.helpers import load_naval_academy_runtime_snapshot
        load_naval_academy_runtime_snapshot(client.commander.commander_id, now_unix)
    except Exception:
        pass

    number = client.commander.get_resource_count(field_id)
    if number > 0:
        # Client harvestresourcecommand.lua: the collected amount is capped by
        # the player's remaining oil/gold bag capacity (getLevelMaxOil /
        # getLevelMaxGold); whatever does not fit stays in the well field.
        moved = number
        try:
            from src.answer.playerops.helpers import player_max_resource
            max_amount = player_max_resource(payload.type, client.commander.level,
                                             int(client.commander.commander_id))
            if max_amount is not None:
                current = client.commander.get_resource_count(payload.type)
                moved = min(number, max(0, max_amount - current))
        except Exception:
            pass
        if moved > 0:
            client.commander.add_resource(payload.type, moved)
            client.commander.set_resource(field_id, number - moved)
            try:
                from src.orm.naval_academy_runtime import load_naval_academy_runtime, save_naval_academy_runtime
                rt = load_naval_academy_runtime(client.commander.commander_id)
                if rt:
                    if payload.type == 1:
                        rt.gold_collect_timestamp = now_unix
                    elif payload.type == 2:
                        rt.oil_collect_timestamp = now_unix
                    save_naval_academy_runtime(rt)
            except Exception:
                pass

    response = protobuf.SC_11014()
    response.result = 0
    create_locked_task(client.send_message(11014, response))
    return 0, 11014, None


def handle_click_mingshi(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    """Akashi easter egg: tapping her in the Charge/Shop screen (first two taps
    per session -- the client stops at mingshiflag >= 2) grants +5 acc_pay_lv
    ("chargeExp" on the client). 30 taps total trigger the Akashi Commission
    chain via CS_11202 (activity 21), handled in activity_operation.py."""
    try:
        payload = protobuf.CS_11506()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 11507, e

    client.commander.acc_pay_lv += 5
    store = get_default_store()
    if store is not None:
        try:
            store.execute(
                "UPDATE commanders SET acc_pay_lv = $1 WHERE commander_id = $2",
                client.commander.acc_pay_lv, client.commander.commander_id,
            )
        except Exception as e:
            log_event("Server", "ClickMingshi",
                      f"acc_pay_lv save failed: {e}", LOG_LEVEL_ERROR)

    response = protobuf.SC_11507()
    response.result = 0
    create_locked_task(client.send_message(11507, response))
    return 0, 11507, None


def handle_owned_items(_buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    response = protobuf.SC_15001()
    items_map = getattr(client.commander, "commander_items_map", None) or {}
    for item_id, item in items_map.items():
        info = protobuf.ITEMINFO()
        info.id = item["item_id"] if isinstance(item, dict) else item.item_id
        info.count = item["count"] if isinstance(item, dict) else item.count
        response.item_list.append(info)
    # Limit items (SC_15001.limit_list) restore the per-month Specialized Core
    # tally the client shows on the UR-exchange / "Specialized Core" tab and uses
    # for overflow checks (BagProxy.GetLimitCntById(59010)). Without this the
    # cores awarded by weekly cumulative missions vanish from that view on relogin.
    from src.orm.limit_item import get_limit_item_count, SPECIALIZED_CORE_ITEM_ID
    limit_count = get_limit_item_count(client.commander.commander_id, SPECIALIZED_CORE_ITEM_ID)
    if limit_count > 0:
        info = protobuf.ITEMINFO()
        info.id = SPECIALIZED_CORE_ITEM_ID
        info.count = limit_count
        response.limit_list.append(info)
    misc_map = getattr(client.commander, "misc_items_map", None) or {}
    for item_id, item in misc_map.items():
        misc = protobuf.ITEMMISC()
        misc.id = item["item_id"] if isinstance(item, dict) else item.item_id
        misc.data = item["data"] if isinstance(item, dict) else item.data
        response.item_misc_list.append(misc)
    from src.connection.server import generate_packet_header
    data = response.SerializeToString()
    header = generate_packet_header(15001, data, client.packet_index)
    client.write_to_buffer(header + data)
    return 0, 15001, None


def build_player_buffs_message(commander_id: int) -> protobuf.SC_11015:
    """Build SC_11015 with all active commander buffs (used on login and pushed
    after food use so the client's buff list stays up to date)."""
    from datetime import datetime, timezone
    now_dt = datetime.now(timezone.utc)
    store = get_default_store()
    rows = store.fetch(
        "SELECT buff_id, expires_at FROM commander_buffs WHERE commander_id = $1 AND expires_at > $2",
        commander_id, now_dt,
    )
    response = protobuf.SC_11015()
    for row in rows:
        buff = protobuf.BENEFITBUFF()
        buff.id = row["buff_id"]
        buff.timestamp = int(row["expires_at"].timestamp()) if hasattr(row["expires_at"], "timestamp") else row["expires_at"]
        response.buff_list.append(buff)
    return response


def handle_player_buffs(_buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    response = build_player_buffs_message(client.commander.commander_id)
    log_event("Server", "SC_11015", f"Sending {len(response.buff_list)} buffs to the user", LOG_LEVEL_WARN)
    data = response.SerializeToString()
    from src.connection.server import generate_packet_header
    header = generate_packet_header(11015, data, client.packet_index)
    client.write_to_buffer(header + data)
    return 0, 11015, None


async def _send_sell_result(client: Client, response) -> None:
    await client.send_message(15009, response)
    try:
        from src.answer.player_resource_sync import send_player_resource_sync
        send_player_resource_sync(client)
    except Exception:
        pass


def handle_sell_item(_buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        payload = protobuf.CS_15008()
        payload.ParseFromString(_buffer)
        items = [(int(item.id), int(item.count)) for item in payload.item_list]
        if not items:
            raise ValueError("empty sell item list")

        from src.orm.item import sell_commander_items
        awards = sell_commander_items(client.commander.commander_id, items)
    except Exception as e:
        log_event("MiscOps", "SellItem",
                  f"Failed: commander={getattr(client.commander, 'commander_id', 0)}: {e} "
                  f"raw_buffer_hex={_buffer.hex()} raw_buffer_len={len(_buffer)}",
                  LOG_LEVEL_ERROR)
        response = protobuf.SC_15009(result=1)
        create_locked_task(_send_sell_result(client, response))
        return 0, 15009, None

    response = protobuf.SC_15009()
    response.result = 0
    create_locked_task(_send_sell_result(client, response))
    log_event("MiscOps", "SellItem",
              f"commander={client.commander.commander_id} items={items} awards={awards}",
              LOG_LEVEL_DEBUG)
    return 0, 15009, None


def handle_console_command(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        payload = protobuf.CS_11100()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 11101, e

    cmd = payload.cmd
    response = protobuf.SC_11101()
    response.result = 1

    if cmd == "into":
        response.result = 0
        response.msg = "CMD:into Result:ok"
    elif cmd == "world":
        if payload.arg1 == "reset":
            response.result = 0
            response.msg = "CMD:world Result:ok"
        else:
            response.msg = f"CMD:{cmd} Result:fail"
    elif cmd == "kick":
        response.result = 0
        response.msg = "CMD:kick Result:ok"
        create_locked_task(_kick_and_send(client, response))
        return 0, 11101, None
    else:
        response.msg = f"CMD:{cmd} Result:fail"

    create_locked_task(client.send_message(11101, response))
    return 0, 11101, None


async def _kick_and_send(client: Client, response):
    await client.send_message(11101, response)
    await client.disconnect(DR_CONNECTION_TO_SERVER_LOST)
