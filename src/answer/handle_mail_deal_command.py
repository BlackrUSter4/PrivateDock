from typing import Optional

from src.connection.client import Client
from src.orm.mail import (
    adelete_mails,
    afetch_mails_with_attachments,
    aupdate_mail_field,
)
from src.protobuf import protobuf

MAIL_MATCH_TYPE_IDS = 1
MAIL_MATCH_TYPE_RESOURCES = 2
MAIL_MATCH_TYPE_ITEMS = 3

MAIL_CMD_READ = 1
MAIL_CMD_IMPORTANT = 2
MAIL_CMD_UNIMPORTANT = 3
MAIL_CMD_DELETE = 4
MAIL_CMD_ATTACHMENT = 5
MAIL_CMD_OVERFLOW = 6
MAIL_CMD_MOVE = 7



def _select_mail_targets(mails: list, match_list) -> list:
    if not match_list:
        return [m for m in mails if not m.get("is_archived", False)]

    id_set = set()
    resource_set = set()
    item_set = set()

    for expr in match_list:
        expr_type = expr.type
        for aid in (expr.arg_list or []):
            if aid == 0:
                continue
            if expr_type == MAIL_MATCH_TYPE_IDS:
                id_set.add(aid)
            elif expr_type == MAIL_MATCH_TYPE_RESOURCES:
                resource_set.add(aid)
            elif expr_type == MAIL_MATCH_TYPE_ITEMS:
                item_set.add(aid)

    def matches_by_drop(mail):
        if not resource_set and not item_set:
            return False
        for att in mail.get("attachments", []) or []:
            att_type = att.get("type", 0)
            att_item_id = att.get("item_id", 0)
            if att_type == 1 and att_item_id in resource_set:
                return True
            if att_type == 2 and att_item_id in item_set:
                return True
        return False

    seen = set()
    result = []
    for mail in mails:
        if mail.get("is_archived", False):
            continue
        mail_id = mail.get("id", 0)
        if mail_id in seen:
            continue
        matched_by_id = mail_id in id_set
        matched_by_drop = not matched_by_id and matches_by_drop(mail)
        if not matched_by_id and not matched_by_drop:
            continue
        seen.add(mail_id)
        result.append(mail)
    return result


def _merge_drop_infos(drops: list) -> list:
    if not drops:
        return []
    merged = {}
    order = []
    for drop in drops:
        key = (drop.get("type", 0), drop.get("id", 0))
        if key in merged:
            merged[key]["number"] = merged[key].get("number", 0) + drop.get("number", 0)
        else:
            merged[key] = {
                "type": drop.get("type", 0),
                "id": drop.get("id", 0),
                "number": drop.get("number", 0),
            }
            order.append(key)
    return [merged[k] for k in order]


def _mail_attachment_to_drop_info(att: dict) -> dict:
    return {
        "type": att.get("type", 0),
        "id": att.get("item_id", 0),
        "number": att.get("quantity", 0),
    }


async def _collect_attachment_drops(client: Client, mails: list, apply: bool) -> tuple[list, list, list]:
    commander_id = client.commander.commander_id
    mail_ids = []
    drops = []
    new_ships = []
    for mail in mails:
        attachments = mail.get("attachments", []) or []
        if not attachments:
            continue
        if mail.get("attachments_collected", False):
            continue
        if apply:
            mail["read"] = True
            mail["attachments_collected"] = True
            try:
                for att in attachments:
                    att_type = att.get("type", 0)
                    att_item_id = att.get("item_id", 0)
                    att_qty = att.get("quantity", 0)
                    if att_type == 1:
                        from src.orm.resource import add_resource
                        add_resource(commander_id, att_item_id, att_qty)
                    elif att_type == 2:
                        from src.orm.item import add_item
                        add_item(commander_id, att_item_id, att_qty)
                    elif att_type == 4:
                        for _ in range(max(1, int(att_qty))):
                            new_ships.append(client.commander.add_ship(att_item_id))
                    elif att_type in (14, 15, 31):
                        # attire drops: persist ownership (SC_11003 lists); the
                        # DROPINFO in the claim result unlocks it client-side.
                        from src.orm.commander_attire import grant_commander_attire_drop_sync
                        grant_commander_attire_drop_sync(commander_id, att_type, att_item_id, att_qty)
                await aupdate_mail_field(commander_id, mail["id"], "attachments_collected", True)
            except Exception:
                pass

            for att in attachments:
                drops.append(_mail_attachment_to_drop_info(att))
        else:
            for att in attachments:
                drops.append(_mail_attachment_to_drop_info(att))
        mail_ids.append(mail.get("id", 0))
    return mail_ids, _merge_drop_infos(drops), new_ships


async def _push_new_ships(client: Client, new_ships: list) -> None:
    """Push the incremental dock packet so mail-granted ships appear live."""
    if not new_ships:
        return
    from src.answer.shipinfo.builder import build_ship_infos

    response = protobuf.SC_12042()
    for ship in build_ship_infos(new_ships, client.commander.commander_id):
        response.ship_list.append(ship)
    await client.send_message(12042, response)


async def _update_mail_field(commander_id: int, mail_id: int, field: str, value):
    await aupdate_mail_field(commander_id, mail_id, field, value)


async def _send_mail_deal_result(client, result: int, mail_id_list: list, drop_list: list, unread_number: int):
    resp = protobuf.SC_30007(result=result, unread_number=unread_number)
    if mail_id_list:
        resp.mail_id_list.extend(mail_id_list)
    for d in drop_list:
        resp.drop_list.append(protobuf.DROPINFO(type=d["type"], id=d["id"], number=d["number"]))
    await client.send_message(30007, resp)


async def handle_mail_deal_command(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    payload = protobuf.CS_30006()
    payload.ParseFromString(buffer)

    cmd = payload.cmd
    match_list = payload.match_list

    try:
        mails = await afetch_mails_with_attachments(client.commander.commander_id)
    except Exception as e:
        await _send_mail_deal_result(client, 0, [], [], 0)
        return 0, 30007, e

    target_mails = _select_mail_targets(mails, match_list)
    if not target_mails:
        unread_count = sum(1 for m in mails if not m.get("read", False))
        await _send_mail_deal_result(client, 0, [], [], unread_count)
        return 0, 30007, None

    commander_id = client.commander.commander_id
    mail_ids = []
    drops = []
    new_ships = []
    dirty = True

    if cmd == MAIL_CMD_READ:
        for mail in target_mails:
            if not mail.get("read", False):
                await _update_mail_field(commander_id, mail["id"], "read", True)
                mail["read"] = True
            mail_ids.append(mail["id"])

    elif cmd == MAIL_CMD_IMPORTANT:
        for mail in target_mails:
            if not mail.get("is_important", False):
                await _update_mail_field(commander_id, mail["id"], "is_important", True)
                mail["is_important"] = True
            mail_ids.append(mail["id"])

    elif cmd == MAIL_CMD_UNIMPORTANT:
        for mail in target_mails:
            if mail.get("is_important", False):
                await _update_mail_field(commander_id, mail["id"], "is_important", False)
                mail["is_important"] = False
            mail_ids.append(mail["id"])

    elif cmd == MAIL_CMD_DELETE:
        for mail in target_mails:
            if mail.get("read", False) and (mail.get("attachments_collected", False) or not mail.get("attachments", [])):
                mail_ids.append(mail["id"])
        if mail_ids:
            await adelete_mails(commander_id, mail_ids)
            mails[:] = [m for m in mails if m["id"] not in set(mail_ids)]

    elif cmd == MAIL_CMD_ATTACHMENT:
        mail_ids, drops, new_ships = await _collect_attachment_drops(client, target_mails, True)
        if mail_ids:
            try:
                from src.answer.task_handlers import schedule_emit, schedule_possession_sync
                schedule_emit(client, 136, 0, len(mail_ids))
                schedule_possession_sync(client)
            except Exception:
                pass

    elif cmd == MAIL_CMD_OVERFLOW:
        mail_ids, drops, _ = await _collect_attachment_drops(client, target_mails, False)

    elif cmd == MAIL_CMD_MOVE:
        for mail in target_mails:
            if not mail.get("is_archived", False):
                await _update_mail_field(commander_id, mail["id"], "is_archived", True)
                mail["is_archived"] = True
            mail_ids.append(mail["id"])

    else:
        dirty = False

    unread_count = sum(1 for m in mails if not m.get("read", False))

    if not dirty:
        mail_ids = [m["id"] for m in mails if not m.get("is_archived", False)]

    await _send_mail_deal_result(client, 0, mail_ids, drops, unread_count)
    if new_ships:
        try:
            await _push_new_ships(client, new_ships)
        except Exception:
            pass
    return 0, 30007, None

