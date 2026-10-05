import datetime
import json
import os
import time
from typing import Optional

from src.connection.client import Client
from src.db.store import get_default_store
from src.region.region import local_now as _region_local_now
from src.logger.logger import log_event, LOG_LEVEL_ERROR, LOG_LEVEL_WARN
from src.orm.item import add_item as _add_item
from src.orm.resource import add_resource as _add_resource, consume_resource as _consume_resource
from src.orm.skin import give_skin as _give_skin
from src.orm.naval_academy_runtime import start_academy_upgrade
from src.protobuf import protobuf


def _resolve_config_path(filename: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(5):
        cand = os.path.join(cur, "configurations", filename)
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.join("configurations", filename)


def _load_offer_limits(filename: str) -> dict[int, dict]:
    path = _resolve_config_path(filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return {int(k): v for k, v in raw.items()}
    except Exception as exc:
        log_event("shopping", f"Failed to load {filename}: {exc}", level=LOG_LEVEL_WARN)
        return {}


GIFT_OFFER_LIMITS: dict[int, dict] = _load_offer_limits("gift_offer_limits.json")
GEM_SHOP_LIMITS: dict[int, dict] = _load_offer_limits("gem_shop_limits.json")

# Combined limits dict for limit lookups (gift packages + gem_shop items).
ALL_OFFER_LIMITS: dict[int, dict] = {**GIFT_OFFER_LIMITS, **GEM_SHOP_LIMITS}


def reload_offer_limits(gift_limits: dict, gem_limits: dict) -> None:
    """In-place refresh of the limit dicts. The ShopOffers importer regenerates
    the JSON files on every reseed — after this module has already been
    imported at server startup — so the live dicts must be updated too."""
    GIFT_OFFER_LIMITS.clear()
    GIFT_OFFER_LIMITS.update(gift_limits)
    GEM_SHOP_LIMITS.clear()
    GEM_SHOP_LIMITS.update(gem_limits)
    ALL_OFFER_LIMITS.clear()
    ALL_OFFER_LIMITS.update(gift_limits)
    ALL_OFFER_LIMITS.update(gem_limits)

# Monotonic suffix for unique lifetime claim keys (see _mark_offer_claimed).
_LIFETIME_COUNTER: list = [0]


def _period_key_weekly(dt: datetime.datetime) -> str:
    return dt.strftime("%G-W%V")


def _period_key_daily(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _period_key_monthly(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m")


# "time" cap offers are tracked with a fixed lifetime period key so the count
# never resets; group-capped offers use their group's daily/weekly/monthly key.
_LIFETIME_PERIOD = "life"


def _offer_period_key(offer_id: int, dt: datetime.datetime) -> str:
    limits = ALL_OFFER_LIMITS.get(offer_id)
    if limits and limits["group_type"]:
        if limits["group_type"] == 1:
            return _period_key_daily(dt)
        if limits["group_type"] == 2:
            return _period_key_weekly(dt)
        if limits["group_type"] == 3:
            return _period_key_monthly(dt)
    return _LIFETIME_PERIOD


# A claim row is stored per purchase. Group-capped offers (group_type 1/2/3)
# all share one period key per period (e.g. "2026-09" for monthly); repeated
# purchases within the same period append "#2", "#3", ... to the key, because
# the table's PRIMARY KEY (commander_id, offer_id, period) would otherwise
# collapse every repeat buy of an offer into one row and a group limit of 15
# (Cognitive Chips, offers 61014-61016) could never be reached. Reads therefore
# match `period = <key> OR period LIKE <key>#%`.
def _period_match_clause() -> str:
    return "(period = $3 OR period LIKE $4)"


async def _is_offer_claimed(commander_id: int, offer_id: int) -> bool:
    store = get_default_store()
    if store is None:
        return True
    try:
        period = _offer_period_key(offer_id, _region_local_now())
        row = await store.afetchrow(
            "SELECT 1 FROM commander_offer_claims "
            "WHERE commander_id = $1 AND offer_id = $2 AND " + _period_match_clause(),
            commander_id, offer_id, period, period + "#%",
        )
        return row is not None
    except Exception as e:
        log_event("OfferClaims", "IsClaimed", f"check failed: {e}", LOG_LEVEL_WARN)
        return False


async def _mark_offer_claimed(commander_id: int, offer_id: int):
    store = get_default_store()
    if store is None:
        return
    try:
        now = _region_local_now()
        limits = ALL_OFFER_LIMITS.get(offer_id)
        if limits and limits["group_type"]:
            base = _offer_period_key(offer_id, now)
            row = await store.afetchrow(
                "SELECT COUNT(*) AS c FROM commander_offer_claims "
                "WHERE commander_id = $1 AND offer_id = $2 AND " + _period_match_clause(),
                commander_id, offer_id, base, base + "#%",
            )
            n = row["c"] if row else 0
            period = base if n == 0 else "%s#%d" % (base, n + 1)
        else:
            # Lifetime-capped offers: each purchase keeps its own unique row so
            # offer_claim_total_count (a plain COUNT over all rows) adds up.
            # Microseconds + a process counter guarantee uniqueness even when
            # two purchases land in the same millisecond (the table's PK would
            # otherwise collapse them into one row).
            _LIFETIME_COUNTER[0] += 1
            period = "%s:%d:%d" % (_LIFETIME_PERIOD, int(now.timestamp() * 1000000), _LIFETIME_COUNTER[0])
        await store.aexecute(
            "INSERT INTO commander_offer_claims (commander_id, offer_id, period) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            commander_id, offer_id, period,
        )
    except Exception as e:
        log_event("OfferClaims", "MarkClaimed", f"insert failed: {e}", LOG_LEVEL_WARN)


async def offer_claim_count(commander_id: int, offer_id: int) -> int:
    store = get_default_store()
    if store is None:
        return 0
    try:
        period = _offer_period_key(offer_id, _region_local_now())
        row = await store.afetchrow(
            "SELECT COUNT(*) AS c FROM commander_offer_claims "
            "WHERE commander_id = $1 AND offer_id = $2 AND " + _period_match_clause(),
            commander_id, offer_id, period, period + "#%",
        )
        return row["c"] if row else 0
    except Exception as e:
        log_event("OfferClaims", "CountClaimed", f"count failed: {e}", LOG_LEVEL_WARN)
        return 0


async def offer_claim_total_count(commander_id: int, offer_id: int) -> int:
    store = get_default_store()
    if store is None:
        return 0
    try:
        row = await store.afetchrow(
            "SELECT COUNT(*) AS c FROM commander_offer_claims "
            "WHERE commander_id = $1 AND offer_id = $2",
            commander_id, offer_id,
        )
        return row["c"] if row else 0
    except Exception as e:
        log_event("OfferClaims", "TotalCount", f"count failed: {e}", LOG_LEVEL_WARN)
        return 0


def offer_ids() -> tuple:
    return tuple(ALL_OFFER_LIMITS.keys())


async def offer_group_claims(commander_id: int) -> list[tuple[int, int]]:
    agg: dict[int, int] = {}
    for offer_id, limits in ALL_OFFER_LIMITS.items():
        group = limits["group"]
        if group <= 0:
            continue
        count = await offer_claim_count(commander_id, offer_id)
        agg[group] = agg.get(group, 0) + count
    return list(agg.items())


async def _load_shop_offer(offer_id: int) -> Optional[dict]:
    store = get_default_store()
    if store is None:
        return None
    try:
        row = await store.afetchrow(
            "SELECT id, effects, effect_args, number, resource_number, resource_id, type, genre, discount "
            "FROM shop_offers WHERE id = $1",
            offer_id,
        )
    except Exception:
        return None
    if row is None:
        return None
    result = dict(row)
    raw_effects = result.get("effects")
    if raw_effects:
        if isinstance(raw_effects, str):
            try:
                parsed = json.loads(raw_effects)
            except (ValueError, TypeError):
                parsed = raw_effects
            result["effects"] = parsed
        else:
            result["effects"] = raw_effects
    else:
        result["effects"] = []
    return result


async def _send_shop_result(client, result: int, drop_list: Optional[list] = None):
    resp = protobuf.SC_16002(result=result)
    if drop_list:
        for d in drop_list:
            resp.drop_list.append(protobuf.DROPINFO(type=d["type"], id=d["id"], number=d["number"]))
    await client.send_message(16002, resp)


async def handle_shop_purchase(buffer: bytes, client: Client) -> tuple[int, int, Optional[Exception]]:
    bought_offer = protobuf.CS_16001.FromString(buffer)
    offer_id = bought_offer.id
    count = bought_offer.number or 1

    shop_offer = await _load_shop_offer(offer_id)
    if shop_offer is None:
        await _send_shop_result(client, 2)
        return 0, 16002, None

    cid = client.commander.commander_id
    limits = ALL_OFFER_LIMITS.get(offer_id)
    if limits:
        if limits["level"] > 0 and getattr(client.commander, "level", 0) < limits["level"]:
            await _send_shop_result(client, 1)
            return 0, 16002, None
        if limits["time"] > 0:
            total = await offer_claim_total_count(cid, offer_id)
            if total >= limits["time"]:
                await _send_shop_result(client, 1)
                return 0, 16002, None
        if limits["group_limit"] > 0:
            group = limits["group"]
            if group > 0:
                group_count = 0
                for oid, lim in ALL_OFFER_LIMITS.items():
                    if lim["group"] == group:
                        group_count += await offer_claim_count(cid, oid)
                if group_count >= limits["group_limit"]:
                    await _send_shop_result(client, 1)
                    return 0, 16002, None
            elif await _is_offer_claimed(cid, offer_id):
                await _send_shop_result(client, 1)
                return 0, 16002, None

    if shop_offer.get("genre") == "shopping_street":
        store = get_default_store()
        if store is not None:
            try:
                row = await store.afetchrow(
                    "SELECT buy_count FROM shopping_street_goods "
                    "WHERE commander_id = $1 AND goods_id = $2",
                    client.commander.commander_id, shop_offer["id"],
                )
                buy_count = row["buy_count"] if row else 0
            except Exception as e:
                buy_count = 0
                log_event("Answer", "shopping_command_answer", f"Exception: {e}", LOG_LEVEL_ERROR)
            if buy_count <= 0:
                await _send_shop_result(client, 1)
                return 0, 16002, None
    elif shop_offer.get("genre") == "arena_shop":
        store = get_default_store()
        if store is not None:
            try:
                row = await store.afetchrow(
                    "SELECT buy_count FROM arena_shop_goods "
                    "WHERE commander_id = $1 AND shop_id = $2",
                    client.commander.commander_id, shop_offer["id"],
                )
                buy_count = row["buy_count"] if row else 0
            except Exception:
                buy_count = 0
            if buy_count >= 1:
                await _send_shop_result(client, 1)
                return 0, 16002, None

    effects = shop_offer.get("effects", [])
    offer_type = shop_offer.get("type", 0)
    base_number = shop_offer.get("number", 0)
    total_number = base_number * count
    drop_list = []
    result = 0

    cid = client.commander.commander_id

    resource_id = shop_offer.get("resource_id", 0)
    resource_number = shop_offer.get("resource_number", 0)
    discount = shop_offer.get("discount", 0) or 0
    if discount > 0 and discount < 100 and resource_number > 0:
        resource_number = (resource_number * (100 - discount)) // 100
    total_cost = resource_number * count


    if offer_type == 1:
        # Yostar's own shop_template.json genuinely encodes the 4
        # "buy_oil" offers (ids 13-16, the "Spend N Gems to buy 500 Oil"
        # quick-refill tiers shown when a commission/build needs more oil
        # than the commander has) with number=-1 - a real placeholder in
        # the official data, not a seeding bug (confirmed: no other genre
        # in the whole template uses num=-1). The real game grants a
        # fixed 500 Oil per purchase regardless of which gem-cost tier;
        # the escalating cost (50/100/200/400 gems) across tiers is
        # selected by purchase count (limit_args), not a multiplier on
        # this placeholder. Multiplying it blindly (-1 * count) produced
        # a negative "amount"/drop number that crashed protobuf
        # serialization (`ValueError: Value out of range: -1` in
        # DROPINFO, an unsigned field) - which made the whole purchase
        # flow hang/loop for the player (SC_16002 never sent, so the
        # client never got a response and retried forever).
        for resource_id in effects:
            grant_number = total_number
            if shop_offer.get("genre") == "buy_oil" and base_number == -1:
                grant_number = 500 * count
            _add_resource(cid, resource_id, grant_number)
            drop_list.append({"type": offer_type, "id": resource_id, "number": grant_number})
    elif offer_type == 2:
        from src.orm.item import resolve_virtual_item_drops as _resolve_drops
        for pack_id in effects:
            _add_item(cid, pack_id, total_number)
            # Send the RESOLVED contents (e.g. 2 Promise Rings for a Promise
            # Crate), not the open_directly wrapper, so the client's live
            # inventory matches the grant without needing a re-login.
            for (dt, di, dc) in _resolve_drops(pack_id, total_number):
                drop_list.append({"type": dt, "id": di, "number": dc})
    elif offer_type == 4:
        ship_ids = effects if isinstance(effects, list) else [effects]
        if not ship_ids:
            result = 2
        else:
            new_ships = []
            add_ship_fn = getattr(client.commander, "add_ship", None)
            for ship_id in ship_ids:
                try:
                    ship_id = int(ship_id)
                except (ValueError, TypeError):
                    result = 2
                    break
                for _ in range(total_number):
                    try:
                        if add_ship_fn is not None:
                            s = add_ship_fn(ship_id)
                        else:
                            from src.orm.owned_ship import add_ship as _orm_add_ship
                            s = _orm_add_ship(cid, ship_id)
                        if s:
                            new_ships.append(s)
                        else:
                            result = 1
                            break
                    except Exception as e:
                        log_event("ShopPurchase", "AddShip", f"failed to add ship {ship_id}: {e}", LOG_LEVEL_ERROR)
                        result = 1
                        break
                if result != 0:
                    break
                drop_list.append({"type": offer_type, "id": ship_id, "number": total_number})

            if result == 0 and new_ships:
                try:
                    from src.answer.shipinfo.builder import build_ship_infos
                    push = protobuf.SC_12042()
                    for s_info in build_ship_infos(new_ships, cid):
                        push.ship_list.append(s_info)
                    await client.send_message(12042, push)
                except Exception as e:
                    log_event("ShopPurchase", "DockSync", f"failed to sync dock: {e}", LOG_LEVEL_WARN)
    elif offer_type == 6:
        for skin_id in effects:
            _give_skin(cid, skin_id)
            drop_list.append({"type": offer_type, "id": skin_id, "number": total_number})
    elif offer_type == 12:
        pass
    elif offer_type == 20:
        result = 3
    elif offer_type == 0:
        if isinstance(effects, str):
            effect_str: str = effects
            if effect_str == "dorm_food_max":
                from src.orm.commander_dorm_state import (
                    get_or_create_commander_dorm_state,
                    save_commander_dorm_state,
                )
                state = get_or_create_commander_dorm_state(cid)
                state.food_max_increase = (
                    getattr(state, 'food_max_increase', 0) or 0
                ) + base_number * count
                state.food_max_increase_count = (
                    getattr(state, 'food_max_increase_count', 0) or 0
                ) + count
                save_commander_dorm_state(state)
            elif effect_str in ("dorm_exp_pos", "dorm_fix_pos", "dorm_floor"):
                from src.orm.commander_dorm_state import (
                    get_or_create_commander_dorm_state,
                    save_commander_dorm_state,
                )
                state = get_or_create_commander_dorm_state(cid)
                if effect_str == "dorm_floor":
                    state.floor_num = (getattr(state, "floor_num", 0) or 0) + count
                else:
                    state.exp_pos = (getattr(state, "exp_pos", 0) or 0) + count
                save_commander_dorm_state(state)
            elif effect_str in ("equip_bag_size", "ship_bag_size", "commander_bag_size", "spweapon_bag_size"):
                # Bag / dock capacity expansions: add the purchased amount to the
                # commander's running total. SC_11003 (ship/equip/commander) and
                # SC_14001 (spweapon) read these columns to report the real max.
                store = get_default_store()
                if store is not None:
                    try:
                        setattr(
                            client.commander, effect_str,
                            (getattr(client.commander, effect_str, 0) or 0) + base_number * count,
                        )
                        await store.aexecute(
                            f'UPDATE commanders SET {effect_str} = COALESCE({effect_str}, 0) + $1 WHERE commander_id = $2',
                            base_number * count, cid,
                        )
                    except Exception as e:
                        log_event("ShopPurchase", "BagSize", f"failed to apply {effect_str}: {e}", LOG_LEVEL_ERROR)
                        result = 2
            elif effect_str == "skill_room_pos":
                # Tactical Class (skill class) slot expansion. In the EN client
                # ShopArgs.EffectSkillPos == "skill_room_pos" and the purchase
                # calls NavalAcademyProxy:inCreaseKillClassNum() (cap 4) locally
                # when it receives the shop result. So the server only needs to
                # persist the new count; do NOT also re-push SC_22001, or the
                # client would apply its local +1 on top of the pushed value
                # and unlock one slot too many.
                store = get_default_store()
                if store is not None:
                    try:
                        cur = getattr(client.commander, "tactical_class_slots", 0) or 0
                        new_slots = min(4, cur + 1)
                        setattr(client.commander, "tactical_class_slots", new_slots)
                        await store.aexecute(
                            "UPDATE commanders SET tactical_class_slots = $1 "
                            "WHERE commander_id = $2",
                            new_slots, cid,
                        )
                    except Exception as e:
                        log_event("ShopPurchase", "SkillRoomPos", f"failed: {e}", LOG_LEVEL_ERROR)
                        result = 2
                else:
                    result = 2
            elif effect_str in ("tradingport_level", "oilfield_level", "shop_street_level",
                                "class_room_level"):
                if not start_academy_upgrade(cid, effect_str):
                    result = 2
                elif effect_str == "class_room_level":
                    try:
                        from src.answer.task_handlers import schedule_emit
                        schedule_emit(client, 84, 0, 1)
                    except Exception:
                        pass
            elif effect_str == "shop_street_flash":
                from src.answer.shopstreet.helpers import (
                    refresh_goods_internal,
                    RefreshOptions,
                    ensure_state,
                )
                from src.answer.shopstreet.handlers import build_shopping_street_proto
                from src.orm.shopping_street import get_shopping_street_state
                state = get_shopping_street_state(cid) or {}
                refresh_goods_internal(
                    cid,
                    int(time.time()),
                    RefreshOptions(set_flash_count=state.get("flash_count", 0) + 1, buy_count=1),
                )
                new_state, new_goods = ensure_state(cid, int(time.time()))
                resp = protobuf.SC_22102()
                resp.street.CopyFrom(build_shopping_street_proto(new_state, new_goods))
                await client.send_message(22102, resp)
            else:
                result = 2
        else:
            result = 2
    else:
        result = 2

    if result == 0:
        store = get_default_store()
        if store and shop_offer.get("genre") == "shopping_street":
            try:
                await store.aexecute(
                    "UPDATE shopping_street_goods SET buy_count = buy_count - 1 "
                    "WHERE commander_id = $1 AND goods_id = $2 AND buy_count > 0",
                    cid, shop_offer["id"],
                )
            except Exception:
                pass
        elif store and shop_offer.get("genre") == "arena_shop":
            # One purchase per item per flash. Record the buy so the shop shows
            # the item as sold out until the next daily/forced refresh.
            try:
                await store.aexecute(
                    "INSERT INTO arena_shop_goods (commander_id, shop_id, buy_count) "
                    "VALUES ($1, $2, 1) "
                    "ON CONFLICT (commander_id, shop_id) "
                    "DO UPDATE SET buy_count = LEAST(arena_shop_goods.buy_count + 1, 1)",
                    cid, shop_offer["id"],
                )
            except Exception:
                pass

        if resource_id and total_cost > 0:
            _consume_resource(cid, resource_id, total_cost)

    if result == 0:
        await _mark_offer_claimed(cid, offer_id)
        # Server-authoritative task progress: a successful shop purchase
        # advances the Handbook buy tasks (sub_type 150 "Buy 1 item at the
        # supply shop", sub_type 155 for Merit shop).
        # The Core Shop (Mo.) buy goes through handle_month_shop_purchase,
        # which emits sub_type 152 separately.
        try:
            from src.answer.task_handlers import schedule_emit, schedule_possession_sync
            schedule_emit(client, 150, 0, 1)
            schedule_emit(client, 154, offer_id, 1)
            schedule_emit(client, 154, 0, 1)
            if shop_offer.get("genre") == "arena_shop":
                schedule_emit(client, 155, 0, 1)
            schedule_possession_sync(client)
        except Exception:
            pass

    await _send_shop_result(client, result, drop_list)
    return 0, 16002, None
