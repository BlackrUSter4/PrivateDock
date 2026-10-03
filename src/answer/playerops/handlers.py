import asyncio
import time
from typing import Optional

from src.connection.client import Client
from src.connection.server import generate_packet_header
from src.protobuf import protobuf
from src.logger.logger import log_event, LOG_LEVEL_ERROR, LOG_LEVEL_INFO

from .helpers import (
    apply_commander_morale_recovery,
    apply_naval_academy_login_catchup,
    load_naval_academy_runtime_snapshot,
    list_config_entries,
    list_commander_skill_classes,
    get_commander_daily_quick_finish_used,
    get_commander_skill_learn_time_allowance,
    commander_has_attire,
    ATTIRE_TYPE_ICON_FRAME,
    ATTIRE_TYPE_CHAT_FRAME,
    ATTIRE_TYPE_COMBAT_UI,
)
from src.db.async_lock import create_locked_task


def handle_send_heartbeat(_buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    response = protobuf.SC_10101()
    response.state = 0
    create_locked_task(client.send_message(10101, response))
    return 0, 10101, None


def handle_resources_info(_buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    now_unix = int(time.time())
    response = protobuf.SC_22001()
    response.oil_well_level = 1
    response.oil_well_lv_up_time = 0
    response.gold_well_level = 1
    response.gold_well_lv_up_time = 0
    response.class_lv = 1
    response.class_lv_up_time = 0
    response.skill_class_num = 0
    response.daily_finish_buff_cnt = 0

    cls = protobuf.NAVALACADEMY_CLASS()
    cls.proficiency = 0
    getattr(response, 'class').CopyFrom(cls)

    try:
        commander = client.commander
        runtime = load_naval_academy_runtime_snapshot(commander.commander_id, now_unix)
        response.oil_well_level = runtime.get("oil_well_level", 1)
        response.oil_well_lv_up_time = runtime.get("oil_upgrade_complete_time", 0)
        response.gold_well_level = runtime.get("gold_well_level", 1)
        response.gold_well_lv_up_time = runtime.get("gold_upgrade_complete_time", 0)

        response.class_lv = runtime.get("class_room_level", 1)
        response.class_lv_up_time = 0

        if hasattr(commander, "get_resource_count"):
            cls.proficiency = int(commander.get_resource_count(10) or 0)
            getattr(response, 'class').CopyFrom(cls)

        # The number of Tactical Class (skill class) slots the player currently
        # has. It starts at 2 and is expanded via the shop (cap 4). This is a
        # per-commander value persisted on the commanders row, NOT the count of
        # naval academy rooms (which would be 6 and is wrong here).
        slots = getattr(commander, "tactical_class_slots", 0) or 0
        response.skill_class_num = max(2, min(4, slots)) if slots else 2

        shopping_entries = list_config_entries("ShareCfg/navalacademy_shoppingstreet_template.json")
        if shopping_entries:
            from src.orm.config_entry import entry_data
            template = entry_data(shopping_entries[0])
            if isinstance(template, dict):
                response.daily_finish_buff_cnt = template.get("special_goods_num", 0)

        classes = list_commander_skill_classes(commander.commander_id)
        if classes:
            for c in classes:
                sc = protobuf.SKILL_CLASS()
                sc.room_id = c["room_id"]
                sc.ship_id = c["ship_id"]
                sc.start_time = c["start_time"]
                sc.finish_time = c["finish_time"]
                sc.skill_pos = c["skill_pos"]
                sc.exp = c["exp"]
                response.skill_class_list.append(sc)

        used = get_commander_daily_quick_finish_used(commander.commander_id, now_unix)
        allowance = get_commander_skill_learn_time_allowance(commander.commander_id, now_unix)
        if allowance > 0:
            if used >= allowance:
                response.daily_finish_buff_cnt = 0
            else:
                response.daily_finish_buff_cnt = allowance - used
    except Exception as e:
        return 0, 22001, e

    data = response.SerializeToString()
    header = generate_packet_header(22001, data, client.packet_index)
    client.write_to_buffer(header + data)

    # Push the updated oilField/goldField (pending well production, resource
    # ids 5/7) so the client shows the "ready to collect" badge on the wells.
    try:
        from src.answer.player_resource_sync import send_player_resource_sync
        send_player_resource_sync(client)
    except Exception:
        pass

    return 0, 22001, None


def handle_player_exist(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        payload = protobuf.CS_10026()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 10027, e

    response = protobuf.SC_10027()

    err = client.get_commander(payload.account_id)
    if err is not None:
        response.user_id = 0
        response.level = 0
    else:
        commander = client.commander
        response.user_id = commander.commander_id
        response.level = commander.level

    create_locked_task(client.send_message(10027, response))
    return 0, 10027, None


def handle_last_online_info(_buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    response = protobuf.SC_11752()
    response.active = 0
    response.return_lv = 0
    response.return_time = 0
    response.ship_number = 0
    response.last_offline_time = 0
    response.pt = 0
    response.pt_stage = 0
    response.sign_cnt = 0
    response.sign_last_time = 0
    data = response.SerializeToString()
    header = generate_packet_header(11752, data, client.packet_index)
    client.write_to_buffer(header + data)
    return 0, 11752, None


def handle_last_login(_buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    now_unix = int(time.time())

    if client.commander is None:
        log_event("Server", "SC_11000", f"commander not yet loaded for {client.ip}", LOG_LEVEL_INFO)
        response = protobuf.SC_11000(timestamp=now_unix, monday_0oclock_timestamp=1606114800)
        data = response.SerializeToString()
        header = generate_packet_header(11000, data, client.packet_index)
        client.write_to_buffer(header + data)
        return 0, 11000, None

    try:
        apply_commander_morale_recovery(client.commander.commander_id, now_unix)
    except Exception as e:
        return 0, 11000, e

    client.previous_login_at = client.commander.last_login

    try:
        previous_ts = 0
        if client.previous_login_at is not None:
            if hasattr(client.previous_login_at, 'timestamp'):
                previous_ts = int(client.previous_login_at.timestamp())
            elif isinstance(client.previous_login_at, (int, float)):
                previous_ts = int(client.previous_login_at)
        apply_naval_academy_login_catchup(client.commander.commander_id, previous_ts, now_unix)
    except Exception as e:
        return 0, 11000, e

    import datetime
    from src.db.store import get_default_store
    s = get_default_store()
    try:
        login_dt = datetime.datetime.fromtimestamp(now_unix, tz=datetime.timezone.utc)
        s.execute("UPDATE commanders SET last_login = $1 WHERE commander_id = $2",
                  login_dt, client.commander.commander_id)
    except Exception as e:
        return 0, 11000, e

    log_event("Server", "SC_11000", f"Updated last login of uid={client.commander.commander_id}", LOG_LEVEL_INFO)

    response = protobuf.SC_11000(timestamp=now_unix, monday_0oclock_timestamp=1606114800)
    data = response.SerializeToString()
    header = generate_packet_header(11000, data, client.packet_index)
    client.write_to_buffer(header + data)

    if not client._login_backup_scheduled:
        try:
            from src.db.backup import schedule_database_backup
            schedule_database_backup(f"login commander={client.commander.commander_id}")
            client._login_backup_scheduled = True
        except Exception as e:
            log_event("DB", "Backup", f"failed to schedule login backup: {e}", LOG_LEVEL_ERROR)
    return 0, 11000, None


def handle_change_ship_lock_state(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        data = protobuf.CS_12022()
        data.ParseFromString(buffer)
    except Exception as e:
        return 0, 12023, e

    response = protobuf.SC_12023()
    response.result = 1

    commander = client.commander
    ship_list = []
    ship_ids = list(data.ship_id_list)
    for ship_id in ship_ids:
        ship = commander.owned_ships_map.get(ship_id)
        if ship is None:
            create_locked_task(client.send_message(12023, response))
            return 0, 12023, None
        ship_list.append(ship)

    new_state = bool(data.is_locked)
    for ship in ship_list:
        ship["is_locked"] = new_state
        from src.db.session import get_sync_session
        from sqlalchemy import text
        with get_sync_session() as session:
            session.execute(
                text("UPDATE owned_ships SET is_locked = :locked WHERE id = :sid"),
                {"locked": new_state, "sid": ship["id"]},
            )
            session.commit()

    response.result = 0
    create_locked_task(client.send_message(12023, response))
    return 0, 12023, None


def handle_change_selected_skin(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        data = protobuf.CS_12202()
        data.ParseFromString(buffer)
    except Exception as e:
        return 0, 12203, e

    response = protobuf.SC_12203()
    response.result = 0

    commander = client.commander
    ship = commander.owned_ships_map.get(data.ship_id)
    if ship is None:
        response.result = 1
        create_locked_task(client.send_message(12203, response))
        return 0, 12203, None

    if data.skin_id != 0:
        if data.skin_id not in commander.owned_skins_map:
            response.result = 2
            create_locked_task(client.send_message(12203, response))
            return 0, 12203, None

    from src.db.session import get_sync_session
    from sqlalchemy import text
    ship["skin_id"] = data.skin_id
    try:
        with get_sync_session() as session:
            session.execute(
                text("UPDATE owned_ships SET skin_id = :skin WHERE id = :sid"),
                {"skin": data.skin_id, "sid": ship["id"]},
            )
            session.commit()
    except Exception:
        response.result = 3

    create_locked_task(client.send_message(12203, response))
    return 0, 12203, None


def handle_change_manifesto(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        payload = protobuf.CS_11009()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 11010, e

    response = protobuf.SC_11010()
    response.result = 0

    client.commander.manifesto = payload.adv
    try:
        client.commander.commit()
    except Exception:
        response.result = 1

    create_locked_task(client.send_message(11010, response))
    return 0, 11010, None


def handle_attire_apply(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    try:
        payload = protobuf.CS_11005()
        payload.ParseFromString(buffer)
    except Exception as e:
        return 0, 11006, e

    response = protobuf.SC_11006()
    response.result = 0
    attire_type = payload.type
    attire_id = payload.id

    if attire_type not in (ATTIRE_TYPE_ICON_FRAME, ATTIRE_TYPE_CHAT_FRAME, ATTIRE_TYPE_COMBAT_UI):
        response.result = 1
        create_locked_task(client.send_message(11006, response))
        return 0, 11006, None

    if attire_id != 0:
        now_unix = int(time.time())
        # every commander owns the default attires (granted at creation);
        # everything else requires a commander_attires row
        owned = commander_has_attire(client.commander.commander_id, attire_type, attire_id, now_unix)
        if not owned:
            response.result = 2
            create_locked_task(client.send_message(11006, response))
            return 0, 11006, None

    if attire_type == ATTIRE_TYPE_ICON_FRAME:
        client.commander.selected_icon_frame_id = attire_id
    elif attire_type == ATTIRE_TYPE_CHAT_FRAME:
        client.commander.selected_chat_frame_id = attire_id
    elif attire_type == ATTIRE_TYPE_COMBAT_UI:
        client.commander.selected_battle_ui_id = attire_id

    try:
        from src.orm.commander_attire import update_commander_attire_style_sync
        update_commander_attire_style_sync(client.commander.commander_id, attire_type, attire_id)
    except Exception as e:
        from src.logger.logger import LOG_LEVEL_WARN
        log_event("Attire", "ApplyError", f"failed to update attire style: {e}", LOG_LEVEL_WARN)
        response.result = 1

    create_locked_task(client.send_message(11006, response))
    return 0, 11006, None
