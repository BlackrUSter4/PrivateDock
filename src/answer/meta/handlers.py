import asyncio
import json
from typing import Optional

from src.connection.client import Client
from src.protobuf import protobuf
from src.db.store import get_default_store
from src.answer.lesson_resource_packet_helpers import parse_usage_arg_exp_value
from .helpers import (
    ensure_commander_meta_loaded,
    meta_skill_slots,
    get_meta_tactics_snapshot,
    build_meta_skill_exp_payload,
    get_or_create_commander_meta_tactics_state_tx,
    get_or_create_commander_meta_tactics_skill_state_tx,
    save_commander_meta_tactics_skill_state_tx,
    save_commander_meta_tactics_state_tx,
    get_ship_meta_breakout_config,
    get_ship_strengthen_meta_config,
    get_ship_meta_repair_config,
    get_ship_data_template_config,
    get_ship_meta_skill_task_config,
    get_skill_data_template_config,
    get_item_data_statistics_config,
    list_owned_ship_meta_repair_ids,
    add_owned_ship_meta_repair_tx,
    normalize_ship_exp_books,
)
from src.db.async_lock import create_locked_task

META_PT_CLAIM_RESULT_SUCCESS = 0
META_PT_CLAIM_RESULT_INVALID_GROUP = 1
META_PT_CLAIM_RESULT_INVALID_TIER = 2
META_PT_CLAIM_RESULT_INSUFFICIENT = 3
META_PT_CLAIM_RESULT_CLAIMED = 4

META_PT_CONFIG_CATEGORIES = [
    "ShareCfg/ship_strengthen_meta.json",
    "sharecfgdata/ship_strengthen_meta.json",
]


def _build_shipinfo_from_ship(ship, flag: int) -> protobuf.SHIPINFO:
    # Thin wrapper kept for the two legacy call sites (63306/70006): the
    # SHIPINFO building itself lives in src/answer/shipinfo/builder.py.
    # `flag` re-attaches the meta phantom id; `shadow` was never applied by
    # the previous minimal builder either.
    from src.answer.shipinfo.builder import build_ship_info
    return build_ship_info(ship, char_random_flags=[flag] if flag else [])


def _load_meta_pt_config(group_id: int) -> Optional[dict]:
    from .helpers import get_config_entry
    key = str(group_id)
    for category in META_PT_CONFIG_CATEGORIES:
        entry = get_config_entry(category, key)
        if entry is not None:
            return entry
    return None


def _meta_pt_tier_index(targets: list, target_pt: int) -> int:
    for i, value in enumerate(targets):
        if value == target_pt:
            return i
    return -1


def _build_meta_pt_tier_drops(award_display: list, tier_index: int):
    if tier_index < 0 or tier_index >= len(award_display):
        return None, False
    tier = award_display[tier_index]
    if len(tier) < 3:
        return None, False
    from src.consts.drop_types import DROP_TYPE_ITEM
    drop_type = tier[0]
    if drop_type == 2:
        drop_type = DROP_TYPE_ITEM
    from .helpers import accumulate_drop
    drops = {}
    accumulate_drop(drops, drop_type, tier[1], tier[2])
    return drops, True


def _meta_pt_contains(values: list, value: int) -> bool:
    return value in values


def _get_or_create_commander_meta_pt_progress_tx( commander_id: int, group_id: int) -> dict:
    """``conn`` is accepted for call-site compatibility and ignored: runs
    through the dialect-neutral sync store (src.db.store)."""
    store = get_default_store()
    store.execute(
        """INSERT INTO commander_meta_pt_progress (commander_id, group_id, pt, fetch_list, created_at, updated_at)
           VALUES ($1, $2, 0, '[]'::jsonb, NOW(), NOW())
           ON CONFLICT (commander_id, group_id) DO NOTHING""",
        commander_id, group_id
    )
    row = store.fetchrow(
        """SELECT commander_id, group_id, pt, fetch_list
           FROM commander_meta_pt_progress
           WHERE commander_id = $1 AND group_id = $2""",
        commander_id, group_id
    )
    if row is None:
        return {"commander_id": commander_id, "group_id": group_id, "pt": 0, "fetch_list": []}
    fetch_list = json.loads(row[3]) if isinstance(row[3], str) else (json.loads(row[3].decode()) if isinstance(row[3], bytes) else row[3] or [])
    return {"commander_id": row[0], "group_id": row[1], "pt": row[2], "fetch_list": fetch_list}


def _save_commander_meta_pt_progress_tx( state: dict):
    get_default_store().execute(
        """INSERT INTO commander_meta_pt_progress (commander_id, group_id, pt, fetch_list, created_at, updated_at)
           VALUES ($1, $2, $3, $4::jsonb, NOW(), NOW())
           ON CONFLICT (commander_id, group_id)
           DO UPDATE SET pt = EXCLUDED.pt, fetch_list = EXCLUDED.fetch_list, updated_at = NOW()""",
        state["commander_id"], state["group_id"], state["pt"], json.dumps(state["fetch_list"])
    )


def handle_get_meta_progress(_buffer: bytes, client: Client) -> tuple:
    response = protobuf.SC_63315()
    response.type = 1
    from src.connection.server import generate_packet_header
    data = response.SerializeToString()
    header = generate_packet_header(63315, data, client.packet_index)
    client.write_to_buffer(header + data)
    return 0, 63315, None


def handle_get_meta_ships_points_response(_buffer: bytes, client: Client) -> tuple:
    response = protobuf.SC_34002()
    commander_id = client.commander.commander_id
    store = get_default_store()
    rows = store.fetch(
        """SELECT group_id, pt, fetch_list
           FROM commander_meta_pt_progress
           WHERE commander_id = $1
           ORDER BY group_id ASC""",
        commander_id
    )
    for r in rows:
        info = protobuf.META_SHIP_INFO()
        info.group_id = r[0]
        info.pt = r[1]
        raw = r[2]
        if raw is not None:
            if isinstance(raw, str):
                info.fetch_list.extend(json.loads(raw))
            elif isinstance(raw, (bytes, bytearray)):
                info.fetch_list.extend(json.loads(raw.decode()))
            else:
                if raw:
                    info.fetch_list.extend(list(raw))
        else:
            pass
        response.meta_ship_list.append(info)
    from src.connection.server import generate_packet_header
    data = response.SerializeToString()
    header = generate_packet_header(34002, data, client.packet_index)
    client.write_to_buffer(header + data)
    return 0, 34002, None


def handle_claim_meta_pt_award(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_34003()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 34004, e

    response = protobuf.SC_34004()
    response.result = META_PT_CLAIM_RESULT_INVALID_GROUP

    config = _load_meta_pt_config(payload.group_id)
    if config is None or config.get("type") != 1:
        create_locked_task(client.send_message(34004, response))
        return 0, 34004, None

    target_pt = payload.target_pt
    tier_index = _meta_pt_tier_index(config.get("target", []), target_pt)
    if tier_index < 0:
        response.result = META_PT_CLAIM_RESULT_INVALID_TIER
        create_locked_task(client.send_message(34004, response))
        return 0, 34004, None

    drops, ok = _build_meta_pt_tier_drops(config.get("award_display", []), tier_index)
    if not ok:
        response.result = META_PT_CLAIM_RESULT_INVALID_TIER
        create_locked_task(client.send_message(34004, response))
        return 0, 34004, None

    commander_id = client.commander.commander_id
    store = get_default_store()

    async def _claim():
        progress = await store.afetchrow(
            """SELECT commander_id, group_id, pt, fetch_list
               FROM commander_meta_pt_progress
               WHERE commander_id = $1 AND group_id = $2
               FOR UPDATE""",
            commander_id, payload.group_id
        )
        if progress is None:
            await store.aexecute(
                """INSERT INTO commander_meta_pt_progress (commander_id, group_id, pt, fetch_list, created_at, updated_at)
                   VALUES ($1, $2, 0, '[]'::jsonb, NOW(), NOW())""",
                commander_id, payload.group_id
            )
            progress_pt = 0
            fetch_list = []
        else:
            progress_pt = progress["pt"]
            raw = progress["fetch_list"]
            if isinstance(raw, str):
                fetch_list = json.loads(raw)
            elif isinstance(raw, bytes):
                fetch_list = json.loads(raw.decode())
            else:
                fetch_list = list(raw) if raw else []

        curr_pt = progress_pt
        if curr_pt < target_pt:
            curr_pt = target_pt

        if curr_pt < target_pt:
            response.result = META_PT_CLAIM_RESULT_INSUFFICIENT
            await client.send_message(34004, response)
            return

        if _meta_pt_contains(fetch_list, target_pt):
            response.result = META_PT_CLAIM_RESULT_CLAIMED
            await client.send_message(34004, response)
            return

        from .helpers import apply_love_letter_drops_tx, drop_map_to_sorted_list

        apply_love_letter_drops_tx(client, drops)

        fetch_list.append(target_pt)
        fetch_list.sort()

        await store.aexecute(
            """INSERT INTO commander_meta_pt_progress (commander_id, group_id, pt, fetch_list, created_at, updated_at)
               VALUES ($1, $2, $3, $4::jsonb, NOW(), NOW())
               ON CONFLICT (commander_id, group_id)
               DO UPDATE SET pt = EXCLUDED.pt, fetch_list = EXCLUDED.fetch_list, updated_at = NOW()""",
            commander_id, payload.group_id, curr_pt, json.dumps(fetch_list)
        )

        response.result = META_PT_CLAIM_RESULT_SUCCESS
        response.drop_list.extend(drop_map_to_sorted_list(drops))
        await client.send_message(34004, response)

    create_locked_task(_claim())
    return 0, 34004, None


def handle_meta_quick_tactics_use_books(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_63319()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 63320, e

    response = protobuf.SC_63320()
    response.ret = 1

    ensure_commander_meta_loaded(client.commander)
    ship = client.commander.owned_ships_map.get(payload.ship_id)
    if ship is None or payload.skill_id == 0:
        create_locked_task(client.send_message(63320, response))
        return 0, 63320, None

    slots, skill_pos, err = meta_skill_slots(ship)
    if err is not None or len(skill_pos) == 0:
        create_locked_task(client.send_message(63320, response))
        return 0, 63320, None

    pos = skill_pos.get(payload.skill_id)
    if pos is None:
        create_locked_task(client.send_message(63320, response))
        return 0, 63320, None

    book_counts, ok = normalize_ship_exp_books(payload.books)
    if not ok:
        create_locked_task(client.send_message(63320, response))
        return 0, 63320, None

    total_exp = 0
    for item_id, count in book_counts.items():
        cfg = get_item_data_statistics_config(item_id)
        if cfg is None or cfg.get("type") != 25:
            create_locked_task(client.send_message(63320, response))
            return 0, 63320, None
        if not client.commander.has_enough_item(item_id, count):
            create_locked_task(client.send_message(63320, response))
            return 0, 63320, None
        exp_per_book = parse_usage_arg_exp_value(cfg.get("usage_arg"))
        total_exp += exp_per_book * count

    commander_id = client.commander.commander_id

    async def _use_books():
        store = get_default_store()
        skill_state = await store.afetchrow(
            """SELECT commander_id, ship_id, skill_id, skill_pos, level, exp
               FROM commander_meta_tactics_skill_states
               WHERE commander_id = $1 AND ship_id = $2 AND skill_id = $3
               FOR UPDATE""",
            commander_id, payload.ship_id, payload.skill_id
        )
        if skill_state is None:
            await store.aexecute(
                """INSERT INTO commander_meta_tactics_skill_states (commander_id, ship_id, skill_id, skill_pos, level, exp)
                   VALUES ($1, $2, $3, $4, 0, 0)""",
                commander_id, payload.ship_id, payload.skill_id, pos
            )
            level = 0
            exp = 0
        else:
            level = skill_state["level"]
            exp = skill_state["exp"]

        if level == 0:
            await client.send_message(63320, response)
            return

        skill_cfg = get_skill_data_template_config(payload.skill_id)
        if skill_cfg is None or skill_cfg.get("max_level", 0) == 0 or level >= skill_cfg["max_level"]:
            await client.send_message(63320, response)
            return

        new_level = level
        new_exp = exp
        remaining = total_exp
        while remaining > 0 and new_level < skill_cfg["max_level"]:
            lvl_cfg = get_ship_meta_skill_task_config(payload.skill_id, new_level)
            if lvl_cfg is None or lvl_cfg.get("need_exp", 0) == 0:
                break
            need = lvl_cfg["need_exp"]
            if new_exp + remaining < need:
                new_exp += remaining
                remaining = 0
                break
            remaining -= (need - new_exp)
            new_level += 1
            new_exp = 0

        if new_level >= skill_cfg["max_level"]:
            new_level = skill_cfg["max_level"]
            new_exp = 0

        for item_id, count in book_counts.items():
            client.commander.consume_item_tx(item_id, count
            )

        from .helpers import save_commander_meta_tactics_skill_state_tx
        save_commander_meta_tactics_skill_state_tx(  {
            "commander_id": commander_id,
            "ship_id": payload.ship_id,
            "skill_id": payload.skill_id,
            "skill_pos": pos,
            "level": new_level,
            "exp": new_exp,
        })

        response.ret = 0
        response.level = new_level
        response.exp = new_exp
        await client.send_message(63320, response)

    create_locked_task(_use_books())
    return 0, 63320, None


def handle_meta_character_repair(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_63301()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 63302, e

    response = protobuf.SC_63302()
    response.result = 1

    ensure_commander_meta_loaded(client.commander)
    ship_id = payload.ship_id
    repair_id = payload.repair_id
    if ship_id == 0 or repair_id == 0:
        create_locked_task(client.send_message(63302, response))
        return 0, 63302, None

    ship = client.commander.owned_ships_map.get(ship_id)
    if ship is None:
        create_locked_task(client.send_message(63302, response))
        return 0, 63302, None

    meta_id = ship["ship_id"] // 10 if isinstance(ship, dict) else ship.ship_id // 10
    meta_cfg = get_ship_strengthen_meta_config(meta_id)
    if meta_cfg is None:
        create_locked_task(client.send_message(63302, response))
        return 0, 63302, None

    allowed = {
        1: meta_cfg.get("repair_cannon", []),
        2: meta_cfg.get("repair_torpedo", []),
        3: meta_cfg.get("repair_air", []),
        4: meta_cfg.get("repair_reload", []),
    }

    s_id = ship["id"] if isinstance(ship, dict) else ship.id
    repairs = list_owned_ship_meta_repair_ids(client.commander.commander_id, s_id)
    consumed = set(repairs)

    valid_step = False
    for chain in allowed.values():
        if not chain:
            continue
        next_id = 0
        for rid in chain:
            if rid not in consumed:
                next_id = rid
                break
        if next_id == repair_id:
            valid_step = True
            break

    if not valid_step:
        create_locked_task(client.send_message(63302, response))
        return 0, 63302, None

    repair_cfg = get_ship_meta_repair_config(repair_id)
    if repair_cfg is None or repair_cfg.get("item_id", 0) == 0 or repair_cfg.get("item_num", 0) == 0:
        create_locked_task(client.send_message(63302, response))
        return 0, 63302, None

    if not client.commander.has_enough_item(repair_cfg["item_id"], repair_cfg["item_num"]):
        create_locked_task(client.send_message(63302, response))
        return 0, 63302, None

    async def _repair():
        store = get_default_store()
        client.commander.consume_item_tx(repair_cfg["item_id"], repair_cfg["item_num"])
        add_owned_ship_meta_repair_tx(client.commander.commander_id, s_id, repair_id)
        response.result = 0
        await client.send_message(63302, response)

    create_locked_task(_repair())
    return 0, 63302, None


def handle_meta_char_active_energy(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_63303()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 63304, e

    response = protobuf.SC_63304()
    response.result = 1

    ensure_commander_meta_loaded(client.commander)
    ship_id = payload.ship_id
    ship = client.commander.owned_ships_map.get(ship_id)
    if ship is None:
        create_locked_task(client.send_message(63304, response))
        return 0, 63304, None

    s_ship_id = ship["ship_id"] if isinstance(ship, dict) else ship.ship_id
    breakout_cfg = get_ship_meta_breakout_config(s_ship_id)
    if breakout_cfg is None or breakout_cfg.get("breakout_id", 0) == 0:
        create_locked_task(client.send_message(63304, response))
        return 0, 63304, None

    s_level = ship["level"] if isinstance(ship, dict) else ship.level
    if s_level < breakout_cfg.get("level", 0):
        create_locked_task(client.send_message(63304, response))
        return 0, 63304, None

    meta_cfg = get_ship_strengthen_meta_config(s_ship_id // 10)
    if meta_cfg is None:
        create_locked_task(client.send_message(63304, response))
        return 0, 63304, None

    s_id = ship["id"] if isinstance(ship, dict) else ship.id
    if breakout_cfg.get("repair", 0) > 0 and meta_cfg.get("repair_total_exp", 0) > 0:
        repair_ids = list_owned_ship_meta_repair_ids(client.commander.commander_id, s_id)
        total_repair_exp = 0
        for rid in repair_ids:
            rcfg = get_ship_meta_repair_config(rid)
            if rcfg is not None:
                total_repair_exp += rcfg.get("repair_exp", 0)
        repair_percent = total_repair_exp * 100 // meta_cfg["repair_total_exp"]
        if repair_percent < breakout_cfg["repair"]:
            create_locked_task(client.send_message(63304, response))
            return 0, 63304, None

    if breakout_cfg.get("gold", 0) > 0 and not client.commander.has_enough_gold(breakout_cfg["gold"]):
        create_locked_task(client.send_message(63304, response))
        return 0, 63304, None

    item1 = breakout_cfg.get("item1", 0)
    item1_num = breakout_cfg.get("item1_num", 0)
    item2 = breakout_cfg.get("item2", 0)
    item2_num = breakout_cfg.get("item2_num", 0)

    if item1 != 0 and item1_num > 0 and not client.commander.has_enough_item(item1, item1_num):
        create_locked_task(client.send_message(63304, response))
        return 0, 63304, None

    if item2 != 0 and item2_num > 0 and not client.commander.has_enough_item(item2, item2_num):
        create_locked_task(client.send_message(63304, response))
        return 0, 63304, None

    next_template = get_ship_data_template_config(breakout_cfg["breakout_id"])
    if next_template is None:
        create_locked_task(client.send_message(63304, response))
        return 0, 63304, None

    async def _active_energy():
        store = get_default_store()
        if breakout_cfg.get("gold", 0) > 0:
            client.commander.consume_resource_tx(1, breakout_cfg["gold"])
        if item1 != 0 and item1_num > 0:
            client.commander.consume_item_tx(item1, item1_num)
        if item2 != 0 and item2_num > 0:
            client.commander.consume_item_tx(item2, item2_num)
        await store.aexecute(
            """UPDATE owned_ships
               SET ship_id = $3, max_level = $4
               WHERE owner_id = $1 AND id = $2""",
            client.commander.commander_id, s_id,
            breakout_cfg["breakout_id"], next_template.get("max_level", 0)
        )
        if isinstance(ship, dict):
            ship["ship_id"] = breakout_cfg["breakout_id"]
            ship["max_level"] = next_template.get("max_level", 0)
        else:
            ship.ship_id = breakout_cfg["breakout_id"]
            ship.max_level = next_template.get("max_level", 0)
        response.result = 0
        await client.send_message(63304, response)

    create_locked_task(_active_energy())
    return 0, 63304, None


def handle_meta_character_unlock_ship(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_63305()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 63306, e

    response = protobuf.SC_63306()
    response.result = 1

    ensure_commander_meta_loaded(client.commander)
    meta_cfg = get_ship_strengthen_meta_config(payload.meta_id)
    if meta_cfg is None or meta_cfg.get("type") != 1 or meta_cfg.get("ship_id", 0) == 0:
        create_locked_task(client.send_message(63306, response))
        return 0, 63306, None

    target_ship_id = meta_cfg["ship_id"]
    ship_template = get_ship_data_template_config(target_ship_id)
    if ship_template is None:
        create_locked_task(client.send_message(63306, response))
        return 0, 63306, None

    commander_id = client.commander.commander_id

    async def _unlock():
        store = get_default_store()
        await store.aexecute(
            "SELECT commander_id FROM commanders WHERE commander_id = $1 FOR UPDATE",
            commander_id
        )
        row = await store.afetchrow(
            """SELECT id FROM owned_ships
               WHERE owner_id = $1 AND ship_id = $2 AND deleted_at IS NULL
               ORDER BY id ASC LIMIT 1""",
            commander_id, target_ship_id
        )
        owned_ship_id = 0
        if row is not None:
            owned_ship_id = row[0]
        else:
            new_ship = client.commander.add_ship_tx(target_ship_id)
            if new_ship is not None:
                owned_ship_id = new_ship.get("id", 0) if isinstance(new_ship, dict) else new_ship.id

        if owned_ship_id == 0:
            await client.send_message(63306, response)
            return

        ship = client.commander.owned_ships_map.get(owned_ship_id)
        if ship is None:
            client.commander.load()
            ship = client.commander.owned_ships_map.get(owned_ship_id)
        if ship is None:
            await client.send_message(63306, response)
            return

        response.result = 0
        response.ship.CopyFrom(_build_shipinfo_from_ship(ship, 0))
        await client.send_message(63306, response)

    create_locked_task(_unlock())
    return 0, 63306, None


def handle_meta_character_tactics_info_request(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_63317()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 63318, e

    ensure_commander_meta_loaded(client.commander)
    response = protobuf.SC_63318()

    for requested_ship_id in payload.ship_id_list:
        ship = client.commander.owned_ships_map.get(requested_ship_id)
        if ship is None:
            continue
        s_id = ship["id"] if isinstance(ship, dict) else ship.id
        slots, _skill_pos, err = meta_skill_slots(ship)
        if err is not None or len(slots) == 0:
            continue
        state, skill_states, _ = get_meta_tactics_snapshot(client.commander.commander_id, s_id)
        if state.get("switch_cnt", 0) == 0:
            state["switch_cnt"] = 3
        info = protobuf.META_SKILL_SIMPLE_INFO()
        info.ship_id = s_id
        info.exp = state.get("daily_exp", 0)
        info.skill_id = state.get("current_skill_id", 0)
        info.skill_exp.extend(build_meta_skill_exp_payload(skill_states))
        response.info_list.append(info)

    create_locked_task(client.send_message(63318, response))
    return 0, 63318, None


def handle_meta_character_tactics_request(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_63313()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 63314, e

    response = protobuf.SC_63314()
    response.ship_id = payload.ship_id
    response.double_exp = 0
    response.exp = 0
    response.skill_id = 0
    response.switch_cnt = 0

    ensure_commander_meta_loaded(client.commander)
    ship = client.commander.owned_ships_map.get(payload.ship_id)
    if ship is None:
        create_locked_task(client.send_message(63314, response))
        return 0, 63314, None

    slots, _skill_pos, err = meta_skill_slots(ship)
    if err is not None or len(slots) == 0:
        create_locked_task(client.send_message(63314, response))
        return 0, 63314, None

    s_id = ship["id"] if isinstance(ship, dict) else ship.id
    state, skill_states, tasks = get_meta_tactics_snapshot(client.commander.commander_id, s_id)
    response.double_exp = state.get("double_exp", 0)
    response.exp = state.get("daily_exp", 0)
    response.skill_id = state.get("current_skill_id", 0)
    response.switch_cnt = state.get("switch_cnt", 0)

    response.skill_exp.extend(build_meta_skill_exp_payload(skill_states))

    for task in tasks:
        t = protobuf.FINISH_TASK()
        t.skill_id = task["skill_id"]
        t.task_id = task["task_id"]
        t.finish_cnt = task["finish_cnt"]
        response.tasks.append(t)

    create_locked_task(client.send_message(63314, response))
    return 0, 63314, None


def handle_meta_character_tactics_switch(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_63307()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 63308, e

    response = protobuf.SC_63308()
    response.result = 1
    response.switch_cnt = 0

    ensure_commander_meta_loaded(client.commander)
    ship = client.commander.owned_ships_map.get(payload.ship_id)
    if ship is None:
        create_locked_task(client.send_message(63308, response))
        return 0, 63308, None

    slots, skill_pos, err = meta_skill_slots(ship)
    if err is not None or len(slots) == 0:
        create_locked_task(client.send_message(63308, response))
        return 0, 63308, None

    target_pos = skill_pos.get(payload.skill_id)
    if target_pos is None:
        create_locked_task(client.send_message(63308, response))
        return 0, 63308, None

    commander_id = client.commander.commander_id
    s_id = ship["id"] if isinstance(ship, dict) else ship.id

    async def _switch():
        store = get_default_store()
        state = get_or_create_commander_meta_tactics_state_tx(commander_id, s_id)
        response.switch_cnt = state["switch_cnt"]

        skill_state = get_or_create_commander_meta_tactics_skill_state_tx(commander_id, s_id, payload.skill_id, target_pos)
        if skill_state["level"] == 0:
            await client.send_message(63308, response)
            return

        if state["current_skill_id"] != payload.skill_id:
            if state["switch_cnt"] == 0:
                await client.send_message(63308, response)
                return
            state["current_skill_id"] = payload.skill_id
            state["switch_cnt"] -= 1
            save_commander_meta_tactics_state_tx(state)

        response.result = 0
        response.switch_cnt = state["switch_cnt"]
        await client.send_message(63308, response)

    create_locked_task(_switch())
    return 0, 63308, None


def handle_meta_character_tactics_level_up(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_63309()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 63310, e

    response = protobuf.SC_63310()
    response.result = 1
    response.switch_cnt = 0

    ensure_commander_meta_loaded(client.commander)
    ship = client.commander.owned_ships_map.get(payload.ship_id)
    if ship is None:
        create_locked_task(client.send_message(63310, response))
        return 0, 63310, None

    slots, skill_pos, err = meta_skill_slots(ship)
    if err is not None or len(skill_pos) == 0:
        create_locked_task(client.send_message(63310, response))
        return 0, 63310, None

    pos = skill_pos.get(payload.skill_id)
    if pos is None:
        create_locked_task(client.send_message(63310, response))
        return 0, 63310, None

    commander_id = client.commander.commander_id
    s_id = ship["id"] if isinstance(ship, dict) else ship.id

    async def _level_up():
        store = get_default_store()
        state = get_or_create_commander_meta_tactics_state_tx(commander_id, s_id)
        response.switch_cnt = state["switch_cnt"]

        skill_state = get_or_create_commander_meta_tactics_skill_state_tx(commander_id, s_id, payload.skill_id, pos)
        if skill_state["level"] == 0:
            await client.send_message(63310, response)
            return

        skill_cfg = get_skill_data_template_config(skill_state["skill_id"])
        if skill_cfg is None or skill_state["level"] >= skill_cfg.get("max_level", 0):
            await client.send_message(63310, response)
            return

        level_cfg = get_ship_meta_skill_task_config(skill_state["skill_id"], skill_state["level"])
        if level_cfg is None or skill_state["exp"] < level_cfg.get("need_exp", 0):
            await client.send_message(63310, response)
            return

        skill_state["level"] += 1
        skill_state["exp"] = 0
        save_commander_meta_tactics_skill_state_tx(skill_state)
        response.result = 0
        response.switch_cnt = state["switch_cnt"]
        await client.send_message(63310, response)

    create_locked_task(_level_up())
    return 0, 63310, None


def handle_meta_character_tactics_unlock(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_63311()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 63312, e

    response = protobuf.SC_63312()
    response.result = 1

    ensure_commander_meta_loaded(client.commander)
    ship = client.commander.owned_ships_map.get(payload.ship_id)
    if ship is None:
        create_locked_task(client.send_message(63312, response))
        return 0, 63312, None

    slots, skill_pos, err = meta_skill_slots(ship)
    if err is not None or len(skill_pos) == 0:
        create_locked_task(client.send_message(63312, response))
        return 0, 63312, None

    pos = skill_pos.get(payload.skill_id)
    if pos is None:
        create_locked_task(client.send_message(63312, response))
        return 0, 63312, None

    skill_cfg = get_ship_meta_skill_task_config(payload.skill_id, 1)
    if skill_cfg is None:
        create_locked_task(client.send_message(63312, response))
        return 0, 63312, None

    required_item = 0
    required_count = 0
    for unlock in skill_cfg.get("skill_unlock", []):
        if len(unlock) < 3:
            continue
        if unlock[0] == payload.index:
            required_item = unlock[1]
            required_count = unlock[2]
            break

    if required_item == 0 or required_count == 0 or not client.commander.has_enough_item(required_item, required_count):
        create_locked_task(client.send_message(63312, response))
        return 0, 63312, None

    commander_id = client.commander.commander_id
    s_id = ship["id"] if isinstance(ship, dict) else ship.id

    async def _unlock_skill():
        store = get_default_store()
        state = get_or_create_commander_meta_tactics_state_tx(commander_id, s_id)
        skill_state = get_or_create_commander_meta_tactics_skill_state_tx(commander_id, s_id, payload.skill_id, pos)
        if skill_state["level"] > 0:
            await client.send_message(63312, response)
            return

        client.commander.consume_item_tx(required_item, required_count)
        skill_state["level"] = 1
        skill_state["exp"] = 0
        save_commander_meta_tactics_skill_state_tx(skill_state)
        if state["current_skill_id"] == 0:
            state["current_skill_id"] = payload.skill_id
            save_commander_meta_tactics_state_tx(state)

        response.result = 0
        await client.send_message(63312, response)

    create_locked_task(_unlock_skill())
    return 0, 63312, None


def handle_meta_character_repair_legacy(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_70001()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 70002, e

    response = protobuf.SC_70002()
    response.result = 1

    ensure_commander_meta_loaded(client.commander)
    ship_id = payload.id
    repair_ids = list(payload.attr_list) if payload.attr_list else []

    if ship_id == 0 or len(repair_ids) == 0:
        create_locked_task(client.send_message(70002, response))
        return 0, 70002, None

    ship = client.commander.owned_ships_map.get(ship_id)
    if ship is None:
        create_locked_task(client.send_message(70002, response))
        return 0, 70002, None

    s_ship_id = ship["ship_id"] if isinstance(ship, dict) else ship.ship_id
    meta_id = s_ship_id // 10
    meta_cfg = get_ship_strengthen_meta_config(meta_id)
    if meta_cfg is None:
        create_locked_task(client.send_message(70002, response))
        return 0, 70002, None

    allowed = {
        1: meta_cfg.get("repair_cannon", []),
        2: meta_cfg.get("repair_torpedo", []),
        3: meta_cfg.get("repair_air", []),
        4: meta_cfg.get("repair_reload", []),
    }

    s_id = ship["id"] if isinstance(ship, dict) else ship.id
    repairs = list_owned_ship_meta_repair_ids(client.commander.commander_id, s_id)
    consumed = set(repairs)

    required_items = {}
    for rid in repair_ids:
        if rid == 0:
            create_locked_task(client.send_message(70002, response))
            return 0, 70002, None

        valid_step = False
        for chain in allowed.values():
            if not chain:
                continue
            next_id = 0
            for cid in chain:
                if cid not in consumed:
                    next_id = cid
                    break
            if next_id == rid:
                valid_step = True
                break

        if not valid_step:
            create_locked_task(client.send_message(70002, response))
            return 0, 70002, None

        repair_cfg = get_ship_meta_repair_config(rid)
        if repair_cfg is None or repair_cfg.get("item_id", 0) == 0 or repair_cfg.get("item_num", 0) == 0:
            create_locked_task(client.send_message(70002, response))
            return 0, 70002, None

        item_id = repair_cfg["item_id"]
        required_items[item_id] = required_items.get(item_id, 0) + repair_cfg["item_num"]
        consumed.add(rid)

    for item_id, item_num in required_items.items():
        if not client.commander.has_enough_item(item_id, item_num):
            create_locked_task(client.send_message(70002, response))
            return 0, 70002, None

    async def _repair_legacy():
        store = get_default_store()
        for item_id, item_num in required_items.items():
            client.commander.consume_item_tx(item_id, item_num)
        for rid in repair_ids:
            add_owned_ship_meta_repair_tx(client.commander.commander_id, s_id, rid)
        response.result = 0
        await client.send_message(70002, response)

    create_locked_task(_repair_legacy())
    return 0, 70002, None


def handle_meta_char_active_energy_legacy(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_70003()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 70004, e

    response = protobuf.SC_70004()
    response.result = 1

    ensure_commander_meta_loaded(client.commander)
    ship_id = payload.id
    ship = client.commander.owned_ships_map.get(ship_id)
    if ship is None:
        create_locked_task(client.send_message(70004, response))
        return 0, 70004, None

    s_ship_id = ship["ship_id"] if isinstance(ship, dict) else ship.ship_id
    breakout_cfg = get_ship_meta_breakout_config(s_ship_id)
    if breakout_cfg is None or breakout_cfg.get("breakout_id", 0) == 0:
        create_locked_task(client.send_message(70004, response))
        return 0, 70004, None

    s_level = ship["level"] if isinstance(ship, dict) else ship.level
    if s_level < breakout_cfg.get("level", 0):
        create_locked_task(client.send_message(70004, response))
        return 0, 70004, None

    meta_cfg = get_ship_strengthen_meta_config(s_ship_id // 10)
    if meta_cfg is None:
        create_locked_task(client.send_message(70004, response))
        return 0, 70004, None

    s_id = ship["id"] if isinstance(ship, dict) else ship.id
    if breakout_cfg.get("repair", 0) > 0 and meta_cfg.get("repair_total_exp", 0) > 0:
        repair_ids = list_owned_ship_meta_repair_ids(client.commander.commander_id, s_id)
        total_repair_exp = 0
        for rid in repair_ids:
            rcfg = get_ship_meta_repair_config(rid)
            if rcfg is not None:
                total_repair_exp += rcfg.get("repair_exp", 0)
        repair_percent = total_repair_exp * 100 // meta_cfg["repair_total_exp"]
        if repair_percent < breakout_cfg["repair"]:
            create_locked_task(client.send_message(70004, response))
            return 0, 70004, None

    if breakout_cfg.get("gold", 0) > 0 and not client.commander.has_enough_gold(breakout_cfg["gold"]):
        create_locked_task(client.send_message(70004, response))
        return 0, 70004, None

    item1 = breakout_cfg.get("item1", 0)
    item1_num = breakout_cfg.get("item1_num", 0)
    item2 = breakout_cfg.get("item2", 0)
    item2_num = breakout_cfg.get("item2_num", 0)

    if item1 != 0 and item1_num > 0 and not client.commander.has_enough_item(item1, item1_num):
        create_locked_task(client.send_message(70004, response))
        return 0, 70004, None

    if item2 != 0 and item2_num > 0 and not client.commander.has_enough_item(item2, item2_num):
        create_locked_task(client.send_message(70004, response))
        return 0, 70004, None

    next_template = get_ship_data_template_config(breakout_cfg["breakout_id"])
    if next_template is None:
        create_locked_task(client.send_message(70004, response))
        return 0, 70004, None

    async def _active_energy_legacy():
        store = get_default_store()
        if breakout_cfg.get("gold", 0) > 0:
            client.commander.consume_resource_tx(1, breakout_cfg["gold"])
        if item1 != 0 and item1_num > 0:
            client.commander.consume_item_tx(item1, item1_num)
        if item2 != 0 and item2_num > 0:
            client.commander.consume_item_tx(item2, item2_num)
        await store.aexecute(
            """UPDATE owned_ships
               SET ship_id = $3, max_level = $4
               WHERE owner_id = $1 AND id = $2""",
            client.commander.commander_id, s_id,
            breakout_cfg["breakout_id"], next_template.get("max_level", 0)
        )
        if isinstance(ship, dict):
            ship["ship_id"] = breakout_cfg["breakout_id"]
            ship["max_level"] = next_template.get("max_level", 0)
        else:
            ship.ship_id = breakout_cfg["breakout_id"]
            ship.max_level = next_template.get("max_level", 0)
        response.result = 0
        await client.send_message(70004, response)

    create_locked_task(_active_energy_legacy())
    return 0, 70004, None


def handle_meta_character_unlock_ship_legacy(buffer: bytes, client: Client) -> tuple:
    try:
        payload = protobuf.CS_70005()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 70006, e

    response = protobuf.SC_70006()
    response.result = 1

    ensure_commander_meta_loaded(client.commander)
    meta_cfg = get_ship_strengthen_meta_config(payload.id)
    if meta_cfg is None or meta_cfg.get("type") != 1 or meta_cfg.get("ship_id", 0) == 0:
        create_locked_task(client.send_message(70006, response))
        return 0, 70006, None

    target_ship_id = meta_cfg["ship_id"]
    ship_template = get_ship_data_template_config(target_ship_id)
    if ship_template is None:
        create_locked_task(client.send_message(70006, response))
        return 0, 70006, None

    commander_id = client.commander.commander_id

    async def _unlock_legacy():
        store = get_default_store()
        await store.aexecute(
            "SELECT commander_id FROM commanders WHERE commander_id = $1 FOR UPDATE",
            commander_id
        )
        row = await store.afetchrow(
            """SELECT id FROM owned_ships
               WHERE owner_id = $1 AND ship_id = $2 AND deleted_at IS NULL
               ORDER BY id ASC LIMIT 1""",
            commander_id, target_ship_id
        )
        owned_ship_id = 0
        if row is not None:
            owned_ship_id = row[0]
        else:
            new_ship = client.commander.add_ship_tx(target_ship_id)
            if new_ship is not None:
                owned_ship_id = new_ship.get("id", 0) if isinstance(new_ship, dict) else new_ship.id

        if owned_ship_id == 0:
            await client.send_message(70006, response)
            return

        ship = client.commander.owned_ships_map.get(owned_ship_id)
        if ship is None:
            client.commander.load()
            ship = client.commander.owned_ships_map.get(owned_ship_id)
        if ship is None:
            await client.send_message(70006, response)
            return

        response.result = 0
        response.ship.CopyFrom(_build_shipinfo_from_ship(ship, 0))
        await client.send_message(70006, response)

    create_locked_task(_unlock_legacy())
    return 0, 70006, None

