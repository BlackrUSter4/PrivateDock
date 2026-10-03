import time

from src.orm.medal_shop import (
    adecrement_medal_shop_good_count,
    aget_medal_shop_good_by_goods_id,
    aget_medal_shop_next_refresh,
)
from src.protobuf import protobuf
from src.answer.medal.helpers import (
    medalShopCurrencyItemID,
    medalShopPurchaseResultOK,
    medalShopPurchaseResultInvalid,
    medalShopPurchaseResultInsufficient,
    medalShopPurchaseResultStock,
    medalShopPurchaseResultStale,
    contains_uint32,
    load_honor_medal_goods_list_entry,
    load_config,
    refresh_if_needed,
    build_medal_shop_goods,
)
from src.consts.drop_types import DROP_TYPE_ITEM, DROP_TYPE_SHIP


async def GetMedalShop(buffer: bytes, client) -> tuple:
    request = protobuf.CS_16106()
    request.ParseFromString(buffer)
    goods_ids, purchase_limit = await load_config()
    next_refresh, goods = await refresh_if_needed(
        client.commander.commander_id, int(time.time()), goods_ids, purchase_limit
    )
    response = protobuf.SC_16107(
        result=0,
        item_flash_time=next_refresh,
        good_list=build_medal_shop_goods(goods),
    )
    return await client.send_message(16107, response)


async def MedalShopPurchase(buffer: bytes, client) -> tuple:
    request = protobuf.CS_16108()
    request.ParseFromString(buffer)
    response = protobuf.SC_16109(result=medalShopPurchaseResultInvalid)
    shop_id = request.shopid
    if shop_id == 0 or request.flash_time == 0:
        return await client.send_message(16109, response)
    selected = list(request.selected)
    if not selected:
        return await client.send_message(16109, response)
    entry, found = await load_honor_medal_goods_list_entry(shop_id)
    if not found:
        return await client.send_message(16109, response)
    if entry.num == 0 or not entry.goods:
        return await client.send_message(16109, response)
    if entry.goods_type == 1 and len(selected) != 1:
        return await client.send_message(16109, response)
    total_units = 0
    rewards = {}
    for pick in selected:
        pid = pick.id
        count = pick.count
        if pid == 0 or count == 0:
            return await client.send_message(16109, response)
        if not contains_uint32(entry.goods, pid):
            return await client.send_message(16109, response)
        total_units += count
        rewards[pid] = rewards.get(pid, 0) + count
    if total_units == 0:
        return await client.send_message(16109, response)
    total_cost = entry.price * total_units
    commander_id = client.commander.commander_id

    next_refresh_time = await aget_medal_shop_next_refresh(commander_id)
    if next_refresh_time is None:
        response.result = medalShopPurchaseResultInvalid
        return await client.send_message(16109, response)
    if next_refresh_time != request.flash_time:
        response.result = medalShopPurchaseResultStale
        return await client.send_message(16109, response)
    slot = await aget_medal_shop_good_by_goods_id(commander_id, shop_id)
    if slot is None:
        response.result = medalShopPurchaseResultInvalid
        return await client.send_message(16109, response)
    # `count` is the REMAINING stock (limit - bought). The client's
    # MedalGoods.CanPurchase is `count > 0`, so reject when there isn't
    # enough remaining to cover the requested units.
    if slot["count"] < total_units:
        response.result = medalShopPurchaseResultStock
        return await client.send_message(16109, response)
    if not client.commander.has_enough_item(medalShopCurrencyItemID, total_cost):
        response.result = medalShopPurchaseResultInsufficient
        return await client.send_message(16109, response)

    # `is_ship` on honormedal_goods_list entries is unreliable: the "SR <hull
    # class>" mystery-pick slots (goods_type 2, e.g. id 29/31/32/36/37/44/45)
    # carry real ship_data_template ids in `goods` but are imported with
    # is_ship=0. Trusting that flag routed the grant through add_item() with
    # a ship id as the item id -- no exception, medals/stock still consumed,
    # but the resulting "item" doesn't exist in the client's item tables, so
    # the client never shows a reward and the purchase looks like it silently
    # failed. Classify each picked id by whether it's actually a real ship
    # template instead, which is correct regardless of the entry's own flag.
    from src.orm.game_data import get_ship_template_config

    # Sequential store calls (no explicit transaction): the guarded stock
    # UPDATE runs first so a "0 rows" result cannot leave consumed medals.
    ok = await adecrement_medal_shop_good_count(commander_id, slot["index"], total_units)
    if not ok:
        response.result = medalShopPurchaseResultStock
        return await client.send_message(16109, response)
    client.commander.consume_item(medalShopCurrencyItemID, total_cost)
    drops = []
    for rid, units in rewards.items():
        reward_amount = entry.num * units
        is_real_ship = False
        try:
            is_real_ship = bool(get_ship_template_config(rid))
        except Exception:
            pass
        drop_type = DROP_TYPE_SHIP if is_real_ship else DROP_TYPE_ITEM
        if drop_type == DROP_TYPE_ITEM:
            client.commander.add_item(rid, reward_amount)
        else:
            for _ in range(reward_amount):
                client.commander.add_ship(rid)
        drops.append(protobuf.DROPINFO(type=drop_type, id=rid, number=reward_amount))
    response.drop_list.extend(drops)

    response.result = medalShopPurchaseResultOK
    # Server-authoritative task progress: a Medal Shop purchase advances
    # "Buy X item(s) at the supply shop"-style tasks. The Medal shop maps to
    # sub_type 162 ("check/earn medals") territory, but per client data the
    # relevant event is a shop buy; emit sub_type 150 (supply shop buy).
    try:
        from src.answer.task_handlers import schedule_emit, schedule_possession_sync
        schedule_emit(client, 150, 0, 1)
        schedule_emit(client, 154, 0, 1)
        schedule_possession_sync(client)
    except Exception:
        pass
    return await client.send_message(16109, response)
