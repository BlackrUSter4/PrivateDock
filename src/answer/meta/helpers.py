import time
from typing import Optional

from src.db.store import get_default_store
from src.orm.config_entry import fetch_config_entries_data, fetch_config_entry_data
from src.protobuf import protobuf
from src.consts.drop_types import (
    DROP_TYPE_RESOURCE,
    DROP_TYPE_ITEM,
    DROP_TYPE_EQUIP,
    DROP_TYPE_SHIP,
    DROP_TYPE_FURNITURE,
    DROP_TYPE_SKIN,
    DROP_TYPE_VITEM,
    DROP_TYPE_LOVE_LETTER,
)

SHIP_META_REPAIR_CATEGORY = "ShareCfg/ship_meta_repair.json"
SHIP_META_SKILL_TASK_CATEGORY = "ShareCfg/ship_meta_skilltask.json"
SHIP_META_BREAKOUT_CATEGORY = "ShareCfg/ship_meta_breakout.json"
SHIP_STRENGTHEN_META_CATEGORY = "ShareCfg/ship_strengthen_meta.json"
SKILL_DATA_TEMPLATE_CATEGORY = "ShareCfg/skill_data_template.json"
ITEM_DATA_STATISTICS_CATEGORY = "sharecfgdata/item_data_statistics.json"
ITEM_DATA_STATISTICS_CATEGORY2 = "ShareCfg/item_data_statistics.json"

DEFAULT_META_TACTICS_SWITCH_COUNT = 3


get_config_entry = fetch_config_entry_data
list_config_entries = fetch_config_entries_data


def get_ship_meta_repair_config(repair_id: int) -> Optional[dict]:
    return get_config_entry(SHIP_META_REPAIR_CATEGORY, str(repair_id))


def get_ship_meta_breakout_config(template_id: int) -> Optional[dict]:
    return get_config_entry(SHIP_META_BREAKOUT_CATEGORY, str(template_id))


def get_ship_strengthen_meta_config(meta_id: int) -> Optional[dict]:
    return get_config_entry(SHIP_STRENGTHEN_META_CATEGORY, str(meta_id))


def get_ship_meta_skill_task_config(skill_id: int, level: int) -> Optional[dict]:
    entries = list_config_entries(SHIP_META_SKILL_TASK_CATEGORY)
    for entry in entries:
        if entry.get("skill_ID") == skill_id and entry.get("level") == level:
            return entry
    return None


def get_skill_data_template_config(skill_id: int) -> Optional[dict]:
    return get_config_entry(SKILL_DATA_TEMPLATE_CATEGORY, str(skill_id))


def get_ship_data_template_meta_config(template_id: int) -> Optional[dict]:
    cfg_key = "ShareCfg/ship_data_template.json"
    entry = get_config_entry(cfg_key, str(template_id))
    if entry is None:
        return None
    return {"id": entry.get("id", 0), "buff_list_display": entry.get("buff_list_display", [])}


def get_ship_data_template_config(template_id: int) -> Optional[dict]:
    return get_config_entry("ShareCfg/ship_data_template.json", str(template_id))


def get_item_data_statistics_config(item_id: int) -> Optional[dict]:
    entry = get_config_entry(ITEM_DATA_STATISTICS_CATEGORY, str(item_id))
    if entry is None:
        entry = get_config_entry(ITEM_DATA_STATISTICS_CATEGORY2, str(item_id))
    return entry


def get_meta_tactics_skill_slots_by_ship_template(ship_template_id: int):
    ship_cfg = get_ship_data_template_meta_config(ship_template_id)
    if ship_cfg is None:
        return []
    result = []
    buff_list = ship_cfg.get("buff_list_display", [])
    for buff_id in buff_list:
        task_cfg = get_ship_meta_skill_task_config(buff_id, 1)
        if task_cfg is None:
            continue
        result.append({"skill_id": buff_id, "pos": len(result) + 1})
    return result


def ensure_commander_meta_loaded(commander):
    if commander.owned_ships_map is not None and commander.items_map is not None:
        return
    commander.load()


def meta_skill_slots(ship):
    ship_template_id = ship.get("ship_id", 0) if isinstance(ship, dict) else getattr(ship, "ship_id", 0)
    slots = get_meta_tactics_skill_slots_by_ship_template(ship_template_id)
    index = {}
    for slot in slots:
        index[slot["skill_id"]] = slot["pos"]
    return slots, index, None


def meta_skill_state_by_skill(states):
    indexed = {}
    for state in states:
        sid = state["skill_id"] if isinstance(state, dict) else state.skill_id
        indexed[sid] = state
    return indexed


def get_commander_meta_tactics_state(commander_id: int, ship_id: int) -> Optional[dict]:
    store = get_default_store()
    row = store.fetchrow(
        """SELECT commander_id, ship_id, current_skill_id, daily_exp, double_exp, switch_cnt
           FROM commander_meta_tactics_states
           WHERE commander_id = $1 AND ship_id = $2""",
        commander_id, ship_id
    )
    if row is None:
        return None
    return {
        "commander_id": row[0],
        "ship_id": row[1],
        "current_skill_id": row[2],
        "daily_exp": row[3],
        "double_exp": row[4],
        "switch_cnt": row[5],
    }


def list_commander_meta_tactics_skill_states(commander_id: int, ship_id: int):
    store = get_default_store()
    rows = store.fetch(
        """SELECT commander_id, ship_id, skill_id, skill_pos, level, exp
           FROM commander_meta_tactics_skill_states
           WHERE commander_id = $1 AND ship_id = $2
           ORDER BY skill_pos ASC, skill_id ASC""",
        commander_id, ship_id
    )
    return [
        {"commander_id": r[0], "ship_id": r[1], "skill_id": r[2], "skill_pos": r[3], "level": r[4], "exp": r[5]}
        for r in rows
    ]


def list_commander_meta_tactics_task_progress(commander_id: int, ship_id: int):
    store = get_default_store()
    rows = store.fetch(
        """SELECT commander_id, ship_id, skill_id, task_id, finish_cnt
           FROM commander_meta_tactics_task_progress
           WHERE commander_id = $1 AND ship_id = $2""",
        commander_id, ship_id
    )
    result = [
        {"commander_id": r[0], "ship_id": r[1], "skill_id": r[2], "task_id": r[3], "finish_cnt": r[4]}
        for r in rows
    ]
    result.sort(key=lambda x: (x["skill_id"], x["task_id"]))
    return result


def get_or_create_commander_meta_tactics_state_tx( commander_id: int, ship_id: int) -> dict:
    """``conn`` is accepted for call-site compatibility and ignored: the
    meta tx helpers run through the dialect-neutral sync store (src.db.store)."""
    store = get_default_store()
    store.execute(
        """INSERT INTO commander_meta_tactics_states (commander_id, ship_id, current_skill_id, daily_exp, double_exp, switch_cnt)
           VALUES ($1, $2, 0, 0, 0, $3)
           ON CONFLICT (commander_id, ship_id) DO NOTHING""",
        commander_id, ship_id, DEFAULT_META_TACTICS_SWITCH_COUNT
    )
    row = store.fetchrow(
        """SELECT commander_id, ship_id, current_skill_id, daily_exp, double_exp, switch_cnt
           FROM commander_meta_tactics_states
           WHERE commander_id = $1 AND ship_id = $2""",
        commander_id, ship_id
    )
    if row is None:
        return None
    return {
        "commander_id": row[0],
        "ship_id": row[1],
        "current_skill_id": row[2],
        "daily_exp": row[3],
        "double_exp": row[4],
        "switch_cnt": row[5],
    }


def save_commander_meta_tactics_state_tx( state: dict):
    store = get_default_store()
    store.execute(
        """UPDATE commander_meta_tactics_states
           SET current_skill_id = $3, daily_exp = $4, double_exp = $5, switch_cnt = $6, updated_at = NOW()
           WHERE commander_id = $1 AND ship_id = $2""",
        state["commander_id"], state["ship_id"], state["current_skill_id"],
        state["daily_exp"], state["double_exp"], state["switch_cnt"]
    )


def get_or_create_commander_meta_tactics_skill_state_tx( commander_id: int, ship_id: int, skill_id: int, skill_pos: int) -> dict:
    store = get_default_store()
    store.execute(
        """INSERT INTO commander_meta_tactics_skill_states (commander_id, ship_id, skill_id, skill_pos, level, exp)
           VALUES ($1, $2, $3, $4, 0, 0)
           ON CONFLICT (commander_id, ship_id, skill_id) DO NOTHING""",
        commander_id, ship_id, skill_id, skill_pos
    )
    row = store.fetchrow(
        """SELECT commander_id, ship_id, skill_id, skill_pos, level, exp
           FROM commander_meta_tactics_skill_states
           WHERE commander_id = $1 AND ship_id = $2 AND skill_id = $3""",
        commander_id, ship_id, skill_id
    )
    if row is None:
        return None
    return {
        "commander_id": row[0],
        "ship_id": row[1],
        "skill_id": row[2],
        "skill_pos": row[3],
        "level": row[4],
        "exp": row[5],
    }


def save_commander_meta_tactics_skill_state_tx( state: dict):
    store = get_default_store()
    store.execute(
        """INSERT INTO commander_meta_tactics_skill_states (commander_id, ship_id, skill_id, skill_pos, level, exp, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, NOW())
           ON CONFLICT (commander_id, ship_id, skill_id)
           DO UPDATE SET skill_pos = EXCLUDED.skill_pos, level = EXCLUDED.level, exp = EXCLUDED.exp, updated_at = NOW()""",
        state["commander_id"], state["ship_id"], state["skill_id"],
        state["skill_pos"], state["level"], state["exp"]
    )


def ensure_meta_skill_states_tx( commander_id: int, ship_id: int, slots: list):
    result = []
    for slot in slots:
        state = get_or_create_commander_meta_tactics_skill_state_tx(
            commander_id, ship_id, slot["skill_id"], slot["pos"]
        )
        if state is not None:
            result.append(state)
    result.sort(key=lambda x: x["skill_pos"])
    return result


def get_meta_tactics_snapshot(commander_id: int, ship_id: int):
    state = get_commander_meta_tactics_state(commander_id, ship_id)
    if state is None:
        state = {"commander_id": commander_id, "ship_id": ship_id, "current_skill_id": 0, "daily_exp": 0, "double_exp": 0, "switch_cnt": DEFAULT_META_TACTICS_SWITCH_COUNT}
    skill_states = list_commander_meta_tactics_skill_states(commander_id, ship_id)
    tasks = list_commander_meta_tactics_task_progress(commander_id, ship_id)
    return state, skill_states, tasks


def build_meta_skill_exp_payload(states: list) -> list:
    result = []
    for s in states:
        level = s["level"] if isinstance(s, dict) else s.level
        if level == 0:
            continue
        skill_id = s["skill_id"] if isinstance(s, dict) else s.skill_id
        exp = s["exp"] if isinstance(s, dict) else s.exp
        entry = protobuf.SKILL_EXP()
        entry.skill_id = skill_id
        entry.exp = exp
        result.append(entry)
    return result


def list_owned_ship_meta_repair_ids(owner_id: int, ship_id: int):
    store = get_default_store()
    rows = store.fetch(
        """SELECT repair_id FROM owned_ship_meta_repairs
           WHERE owner_id = $1 AND ship_id = $2
           ORDER BY repair_id ASC""",
        owner_id, ship_id
    )
    return [r[0] for r in rows]


def add_owned_ship_meta_repair_tx( owner_id: int, ship_id: int, repair_id: int):
    get_default_store().execute(
        """INSERT INTO owned_ship_meta_repairs (owner_id, ship_id, repair_id)
           VALUES ($1, $2, $3)
           ON CONFLICT (owner_id, ship_id, repair_id) DO NOTHING""",
        owner_id, ship_id, repair_id
    )


def normalize_ship_exp_books(books: list) -> tuple:
    if not books:
        return None, False
    merged = {}
    for entry in books:
        if entry is None:
            return None, False
        item_id = entry.id if hasattr(entry, "id") else entry.get("id", 0)
        count = entry.num if hasattr(entry, "num") else entry.get("num", 0)
        if item_id == 0 or count == 0:
            return None, False
        merged[item_id] = merged.get(item_id, 0) + count
    return merged, True


def accumulate_drop(drops: dict, drop_type: int, drop_id: int, count: int):
    key = f"{drop_type}_{drop_id}"
    entry = drops.get(key)
    if entry is None:
        d = protobuf.DROPINFO()
        d.type = drop_type
        d.id = drop_id
        d.number = count
        drops[key] = d
    else:
        entry.number = entry.number + count


def drop_map_to_sorted_list(drops: dict) -> list:
    lst = list(drops.values())
    lst.sort(key=lambda x: (x.type, x.id))
    return lst


def apply_love_letter_drops_tx( client, drops: dict):
    for drop in drops.values():
        drop_type = drop.type
        drop_id = drop.id
        drop_count = drop.number
        commander = client.commander
        if drop_type == DROP_TYPE_RESOURCE:
            commander.add_resource(drop_id, drop_count)
        elif drop_type in (DROP_TYPE_ITEM, DROP_TYPE_LOVE_LETTER):
            commander.add_item(drop_id, drop_count)
        elif drop_type == DROP_TYPE_EQUIP:
            commander.add_owned_equipment(drop_id, drop_count)
        elif drop_type == DROP_TYPE_SHIP:
            for _ in range(drop_count):
                commander.add_ship(drop_id)
        elif drop_type == DROP_TYPE_FURNITURE:
            get_default_store().execute(
                """INSERT INTO commander_furnitures (commander_id, furniture_id, count, get_time)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (commander_id, furniture_id)
                   DO UPDATE SET count = commander_furnitures.count + EXCLUDED.count, get_time = EXCLUDED.get_time""",
                commander.commander_id, drop_id, drop_count, int(time.time())
            )
        elif drop_type == DROP_TYPE_SKIN:
            for _ in range(drop_count):
                commander.give_skin(drop_id)
        elif drop_type == DROP_TYPE_VITEM:
            pass
        else:
            pass
