from __future__ import annotations
import json
import random
import re
from typing import Any, Optional

from sqlalchemy import BigInteger, String, select, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.session import Base, get_session, get_sync_session

from src.consts.drop_types import (
    DROP_TYPE_EQUIP,
    DROP_TYPE_EQUIPMENT_SKIN,
    DROP_TYPE_ITEM,
    DROP_TYPE_RESOURCE,
    DROP_TYPE_SPWEAPON,
    DROP_TYPE_VITEM,
)

# Specialized Core: real item id + virtual drop proxy id (item_virtual_data_statistics
# 59011, virtual_type 20, link_id 59010). Drops grant the proxy; we store the real item.
SPECIALIZED_CORE_ITEM_ID = 59010
SPECIALIZED_CORE_VITEM_ID = 59011

# Virtual items that are actually proxies for real resources. In the official
# game, commission / drop rewards for "Coins", "Oil" and "Gems" credit the
# resource pool directly and never land in the item bag. Map them to their
# resource ids so every drop path (commissions, battles, activities, shops,
# item-use) grants the real resource and the reward popup shows Gold/Oil/Gems
# instead of a bag item. 59004/59005 are free/paid Gems but share one Diamond
# pool (resource 4); the free/paid split is client-side accounting only.
RESOURCE_VIRTUAL_ITEMS = {
    59001: 1,   # "Coins"  -> Gold (resource 1)
    59002: 2,   # "Oil"    -> Oil  (resource 2)
    59003: 3,   # "Merit"  -> Exploit / Merit (resource 3)
    59004: 4,   # "Gems" (free)  -> Gems (resource 4)
    59005: 4,   # "Gems" (paid)  -> Gems (resource 4)
    59006: 6,   # "Decor Tokens" -> Dorm Money (resource 6)
    59008: 8,   # "Guild Coin"   -> Guild Coin (resource 8)
    59015: 11,  # "Game Coin"    -> Game Coin (resource 11)
    59016: 12,  # "Game Ticket"  -> Game Ticket (resource 12)
    59017: 15,  # "Sound Story Card" -> Sound Story Card (resource 15)
}

# Virtual items whose display_icon lists their FULL fixed contents (the item
# description says "Open to receive the following items: ..." / "Contains ...")
# instead of a random pool to pick one from. They must resolve by granting
# EVERY display_icon entry scaled by count, never a random pick:
#   * Cognitive Datapacks 51001-51010 -- chapter 14+ "Get 3 stars" mission
#     rewards grant N Cognitive Arrays AND M Cognitive Chips, both;
#   * 58992/58998 "Black Friday Coupon & Cubes" -- coupon + N Wisdom Cubes.
# tests/test_mystery_pools.py asserts this set equals every multi-entry
# display_icon virtual whose text carries no randomness wording, so a newly
# imported bundle-type family fails the suite instead of silently becoming a
# random pool.
_FIXED_BUNDLE_VIRTUAL_IDS = frozenset(range(51001, 51011)) | {58992, 58998}


# ── Async ORM query functions (for api/handlers) ──


async def list_all_items() -> list[dict[str, Any]]:
    async with get_session() as session:
        result = await session.execute(text("SELECT id, name FROM items ORDER BY id"))
        return [dict(r) for r in result.mappings().all()]


async def list_commander_items(commander_id: int) -> list[dict[str, Any]]:
    async with get_session() as session:
        result = await session.execute(
            text("SELECT item_id, count FROM commander_items WHERE commander_id = :cid"),
            {"cid": commander_id},
        )
        return [dict(r) for r in result.mappings().all()]


def list_commander_items_sync(commander_id: int) -> list[dict[str, Any]]:
    with get_sync_session() as session:
        result = session.execute(
            text("SELECT item_id, count FROM commander_items WHERE commander_id = :cid"),
            {"cid": commander_id},
        )
        return [dict(r) for r in result.mappings().all()]


async def get_commander_item(commander_id: int, item_id: int) -> Optional[dict[str, Any]]:
    async with get_session() as session:
        result = await session.execute(
            text("SELECT count FROM commander_items WHERE commander_id = :cid AND item_id = :iid"),
            {"cid": commander_id, "iid": item_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def get_item_name(item_id: int) -> Optional[str]:
    async with get_session() as session:
        result = await session.execute(
            text("SELECT name FROM items WHERE id = :iid"),
            {"iid": item_id},
        )
        row = result.first()
        return row[0] if row else None


async def upsert_commander_item(commander_id: int, item_id: int, count: int) -> None:
    async with get_session() as session:
        await session.execute(
            text("INSERT INTO commander_items (commander_id, item_id, count) "
                 "VALUES (:cid, :iid, :cnt) "
                 "ON CONFLICT (commander_id, item_id) DO UPDATE SET count = :cnt"),
            {"cid": commander_id, "iid": item_id, "cnt": count},
        )
        await session.commit()


async def delete_commander_item(commander_id: int, item_id: int) -> None:
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM commander_items WHERE commander_id = :cid AND item_id = :iid"),
            {"cid": commander_id, "iid": item_id},
        )
        await session.commit()


class CommanderItem(Base):
    __tablename__ = 'commander_items'
    __table_args__ = {}
    commander_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    count: Mapped[int] = mapped_column(BigInteger, default=0)

def has_enough_item(commander_id: int, type_id: int, count: int) -> bool:
    from sqlalchemy import func
    with get_sync_session() as session:
        result = session.execute(
            select(func.coalesce(func.sum(CommanderItem.count), 0)).where(
                CommanderItem.commander_id == commander_id,
                CommanderItem.item_id == type_id,
            )
        )
        return (result.scalar() or 0) >= count

def add_item(commander_id: int, type_id: int, count: int):
    # "Coins" (59001) / "Oil" (59002) are real Gold/Oil resources in disguise;
    # never store them in the item bag. (resolve_virtual_item_drops already maps
    # them for the resolve-based drop paths; this guards direct add_item calls
    # such as shops and level awards.)
    if type_id in RESOURCE_VIRTUAL_ITEMS:
        from src.orm.resource import add_resource
        add_resource(commander_id, RESOURCE_VIRTUAL_ITEMS[type_id], count)
        return
    # Items flagged open_directly=1 (e.g. supply packs) are meant to auto-open
    # the moment they are received, never sitting in the inventory. Resolve them
    # into their contents (reusing the use_item preparation logic) instead of
    # storing the wrapper.
    cfg = _load_virtual_item_config(type_id)
    # Specialized Core is granted as the virtual proxy 59011 (virtual_type 20,
    # link_id 59010), but the client tracks and consumes it as the real item
    # 59010 (bag, Fragment/Prototype shop, UR-exchange limit list). Grant the
    # linked real item so the cores survive relogin.
    if cfg is not None and cfg.get("virtual_type") == 20 and cfg.get("link_id"):
        type_id = int(cfg["link_id"])
        cfg = _load_virtual_item_config(type_id)
    # Handover Permit (e.g. item 68700, virtual_type 34):
    # Managed as a chapter auto ticket with weekly expiration (next Monday 00:00:00 local time).
    if cfg is not None and cfg.get("virtual_type") == 34:
        from datetime import timedelta
        from src.region.region import local_now
        from src.orm.chapter_auto import add_chapter_auto_tickets, FOREVER_TIME
        now = local_now()
        drop_arg = cfg.get("drop_arg")
        expire_time = FOREVER_TIME
        if isinstance(drop_arg, list) and len(drop_arg) >= 2 and drop_arg[0] == "week":
            offset_weeks = int(drop_arg[1]) if isinstance(drop_arg[1], (int, float)) else 0
            days_ahead = 7 - now.weekday()
            next_monday = (now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
            target_time = next_monday + timedelta(weeks=offset_weeks)
            expire_time = int(target_time.timestamp())
        elif isinstance(drop_arg, list) and len(drop_arg) >= 2 and isinstance(drop_arg[0], list):
            try:
                y, m, d = drop_arg[0][:3]
                h, mi, s = drop_arg[1][:3] if len(drop_arg[1]) >= 3 else (23, 59, 59)
                target_dt = now.replace(year=y, month=m, day=d, hour=h, minute=mi, second=s, microsecond=0)
                expire_time = int(target_dt.timestamp())
            except Exception:
                expire_time = FOREVER_TIME
        add_chapter_auto_tickets(commander_id, 1, count, expire_time)
        return
    if cfg is not None and cfg.get("open_directly", 0) == 1:
        _auto_open_open_directly(commander_id, type_id, count)
        return
    # Mystery virtual items (Mystery Tech/Gear Parts, config types 98/99) resolve
    # into their random contents the moment they are received. They have no manual
    # "Use" button on the client, so storing them raw leaves an unusable wrapper in
    # the bag (e.g. T4 Mystery Gear Part id=54018 -> concrete T4 gear part). This
    # matches the original behaviour where the contents are granted on pickup.
    if cfg is not None and cfg.get("type") in (98, 99):
        has_pool = bool(
            _parse_icon_list(cfg.get("display_icon"))
            or _derived_mystery_pool(type_id, cfg)
            or _mystery_pool_override(type_id)
        )
        if has_pool:
            for (t, i, c) in resolve_virtual_item_drops(type_id, count):
                _apply_resolved_entry(commander_id, t, i, c)
            return
    _store_commander_item(commander_id, type_id, count)
    # Feed the monthly "limit" tally for Specialized Cores so SC_15001.limit_list
    # (UR-exchange progress / overflow checks) survives relogin.
    if type_id == SPECIALIZED_CORE_ITEM_ID:
        from src.orm.limit_item import add_limit_item
        add_limit_item(commander_id, type_id, count)


def _apply_resolved_entry(commander_id: int, drop_type: int, drop_id: int, drop_count: int):
    """Grant one already-resolved (concrete) drop entry."""
    if drop_type == 1:
        from src.orm.resource import add_resource
        add_resource(commander_id, drop_id, drop_count)
    elif drop_type == 4:
        from src.orm.owned_ship import add_ship
        for _ in range(max(1, drop_count)):
            add_ship(commander_id, drop_id)
    elif drop_type == 7:
        from src.orm.skin import give_skin
        give_skin(commander_id, drop_id)
    elif drop_type == DROP_TYPE_EQUIP:
        from src.orm.owned_equipment import _sync_add_owned_equipment
        _sync_add_owned_equipment(commander_id, drop_id, drop_count)
    elif drop_type == 8:
        add_item(commander_id, drop_id, drop_count)
    elif drop_type == DROP_TYPE_EQUIPMENT_SKIN:
        # persist ownership; SC_14101 lists owned skins with real counts and
        # the client refuses to apply a skin with count == 0.
        from src.orm.equipment_skin import grant_equip_skin_sync
        grant_equip_skin_sync(commander_id, drop_id, max(drop_count, 1))
    elif drop_type == DROP_TYPE_SPWEAPON:
        # Augment units have no storage in this emulator; consumed (the
        # client renders the drop popup from its own config).
        pass
    else:
        _store_commander_item(commander_id, drop_id, drop_count)


def _store_commander_item(commander_id: int, type_id: int, count: int):
    # Fill the catalog row from the virtual config when available so the item
    # is searchable by name in the DB (empty-name stubs are useless); the
    # importer backfills everything else on reseed.
    name, rarity, itype = "", 0, 0
    cfg = _load_virtual_item_config(type_id)
    if cfg:
        name = str(cfg.get("name") or "")
        try:
            rarity = int(cfg.get("rarity") or 0)
        except (TypeError, ValueError):
            rarity = 0
        try:
            itype = int(cfg.get("type") or 0)
        except (TypeError, ValueError):
            itype = 0
    with get_sync_session() as session:
        session.execute(
            text("INSERT INTO items (id, name, rarity, shop_id, type, virtual_type) "
                 "VALUES (:id, :name, :rarity, 0, :type, 0) ON CONFLICT (id) DO NOTHING"),
            {"id": type_id, "name": name, "rarity": rarity, "type": itype},
        )
        result = session.execute(
            select(CommanderItem).where(
                CommanderItem.commander_id == commander_id,
                CommanderItem.item_id == type_id,
            )
        )
        obj = result.scalar_one_or_none()
        if obj is None:
            obj = CommanderItem(commander_id=commander_id, item_id=type_id, count=count)
            session.add(obj)
        else:
            obj.count += count
        session.commit()
        # Keep the in-memory commander_items_map in sync so handlers that read it
        # (disassembly, etc.) see the grant without a relogin.
        try:
            from src.orm.active_commander import _bump_count_map
            _bump_count_map(commander_id, "commander_items_map", type_id, count, "item_id")
        except Exception:
            pass


class _AutoOpenCommander:
    """Minimal commander stand-in for auto-opening open_directly items."""

    def __init__(self, commander_id: int):
        self.commander_id = commander_id

    def add_ship(self, ship_template_id):
        from src.orm.owned_ship import add_ship
        add_ship(self.commander_id, ship_template_id)

    def give_skin(self, skin_id):
        from src.orm.skin import give_skin
        give_skin(self.commander_id, skin_id)

    def give_skin_with_expiry(self, skin_id, _expiry):
        from src.orm.skin import give_skin
        give_skin(self.commander_id, skin_id)


class _AutoOpenClient:
    def __init__(self, commander_id: int):
        self.commander = _AutoOpenCommander(commander_id)

    def send_message(self, *args, **kwargs):
        return None


def _auto_open_open_directly(commander_id: int, type_id: int, count: int, _depth: int = 0):
    if _depth > 5:
        _store_commander_item(commander_id, type_id, count)
        return
    cfg = _load_virtual_item_config(type_id)
    if cfg is None or cfg.get("open_directly", 0) != 1:
        _store_commander_item(commander_id, type_id, count)
        return
    from src.answer.item_usage import _display_icon_is_random
    usage_str = str(cfg.get("usage") or "")
    is_drop_template = usage_str in ("usgae_drop_template", "usage_drop_template")
    if not is_drop_template and _display_icon_is_random(cfg) and usage_str:
        # Random-pool open_directly boxes (Gear Skin Boxes etc.): the client is
        # told it received the ITEM (resolve_virtual_item_drops passes random
        # open_directly boxes through unchanged), so it adds the box to the bag
        # and opens it via CS_15002. Auto-opening here would silently consume
        # the box while the reply still shows the wrapper — the client then
        # holds a phantom box that fails with "Invalid Input" on use. Store the
        # box raw; the random pick happens at open time (item_usage.py).
        _store_commander_item(commander_id, type_id, count)
        return
    # Reuse the exact same preparation logic as a manual CS_15002 use, so the
    # granted contents (gold from usage_arg, random display_icon entry, etc.)
    # match opening the pack by hand.
    from src.answer.item_usage import _prepare_item_usage
    client = _AutoOpenClient(commander_id)
    plan = _prepare_item_usage(client, cfg, [], count)
    if plan is None or plan.get("result", 1) != 0 or not plan.get("_apply"):
        _store_commander_item(commander_id, type_id, count)
        return
    plan["_apply"]()

def consume_item(commander_id: int, type_id: int, count: int):
    with get_sync_session() as session:
        result = session.execute(
            select(CommanderItem).where(
                CommanderItem.commander_id == commander_id,
                CommanderItem.item_id == type_id,
            )
        )
        obj = result.scalar_one_or_none()
        if obj is not None:
            obj.count -= count
            if obj.count <= 0:
                session.delete(obj)
            session.commit()
            try:
                from src.orm.active_commander import _bump_count_map
                _bump_count_map(commander_id, "commander_items_map", type_id, -count, "item_id")
            except Exception:
                pass


def get_item_sell_price(item_id: int) -> tuple[int, int]:
    """Return the client-visible (resource_id, unit_price) for an item.

    Item configs use ``price = [resource_id, amount]``. An empty/absent price
    means the item can be discarded but grants nothing, matching BagProxy.
    """
    cfg = _load_virtual_item_config(item_id)
    raw = cfg.get("price") if cfg else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else None
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = None
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return 0, 0
    try:
        resource_id = int(raw[0] or 0)
        amount = int(raw[1] or 0)
    except (TypeError, ValueError):
        return 0, 0
    return resource_id, max(0, amount)


def sell_commander_items(commander_id: int, items: list[tuple[int, int]]) -> dict[int, int]:
    """Atomically sell bag items and return ``{resource_id: amount}`` awards."""
    from src.orm.resource import OwnedResource, dealias_resource

    normalized: dict[int, int] = {}
    for item_id, count in items:
        item_id = int(item_id)
        count = int(count)
        if item_id <= 0 or count <= 0:
            raise ValueError("invalid sell item entry")
        normalized[item_id] = normalized.get(item_id, 0) + count

    prices = {item_id: get_item_sell_price(item_id) for item_id in normalized}
    awards: dict[int, int] = {}
    with get_sync_session() as session:
        rows = session.execute(
            select(CommanderItem).where(
                CommanderItem.commander_id == commander_id,
                CommanderItem.item_id.in_(normalized),
            )
        ).scalars().all()
        owned = {int(row.item_id): row for row in rows}
        for item_id, count in normalized.items():
            if item_id not in owned or int(owned[item_id].count or 0) < count:
                raise ValueError(f"not enough item {item_id}")

        for item_id, count in normalized.items():
            obj = owned[item_id]
            obj.count -= count
            if obj.count <= 0:
                session.delete(obj)

            resource_id, unit_price = prices[item_id]
            if resource_id <= 0 or unit_price <= 0:
                continue
            resource_id = dealias_resource(resource_id)
            rewards = unit_price * count
            awards[resource_id] = awards.get(resource_id, 0) + rewards

        for resource_id, amount in awards.items():
            resource = session.execute(
                select(OwnedResource).where(
                    OwnedResource.commander_id == commander_id,
                    OwnedResource.resource_id == resource_id,
                )
            ).scalar_one_or_none()
            if resource is None:
                session.add(OwnedResource(
                    commander_id=commander_id,
                    resource_id=resource_id,
                    amount=amount,
                ))
            else:
                resource.amount += amount

        session.commit()

    try:
        from src.orm.active_commander import _bump_amount_map, _bump_count_map
        for item_id, count in normalized.items():
            _bump_count_map(commander_id, "commander_items_map", item_id, -count, "item_id")
        for resource_id, amount in awards.items():
            _bump_amount_map(commander_id, "owned_resources_map", resource_id, amount, "resource_id")
    except Exception:
        pass
    return awards


def _load_virtual_item_config(item_id: int) -> Optional[dict]:
    from src.orm.config_entry import get_config_entry_sync
    for category in ("sharecfgdata/item_data_statistics.json",
                     "sharecfgdata/item_virtual_data_statistics.json"):
        row = get_config_entry_sync(category, str(item_id))
        if row is not None:
            data = row.data
            if not isinstance(data, dict):
                try:
                    data = json.loads(data)
                except (ValueError, TypeError):
                    data = None
            if isinstance(data, dict):
                return data
    return None


def _parse_icon_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return []
    return []


def _mystery_pool_override(item_id: int) -> list:
    """Manual override pools from the OPTIONAL configurations/mystery_box_pools.json.

    The file is deleted by default: every known family box is data-derived
    (see `_derived_mystery_pool`) and boxes with an official display_icon
    resolve through that. Recreate the file with
    ``{"<item_id>": [[type, item_id, count], ...]}`` only to pin a pool for a
    box the derivation cannot handle (e.g. a future event box with a curated
    server-side list). Overrides are consulted AFTER the derivation, so they
    only ever apply to boxes with no derived pool.
    """
    import os

    global _MYSTERY_POOL_OVERRIDES
    if _MYSTERY_POOL_OVERRIDES is None:
        # configurations/ is at the project root
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        data = {}
        cand = os.path.join(base, "configurations", "mystery_box_pools.json")
        if os.path.exists(cand):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # strip comment keys (non-numeric)
                data = {k: v for k, v in (loaded or {}).items() if str(k).isdigit()}
            except Exception as e:
                # A broken override file must NOT silently poison the cache
                # (see AGENTS.md coding gotchas) — log it loudly.
                from src.logger.logger import log_event, LOG_LEVEL_ERROR
                log_event("Items", "MysteryPool",
                          f"failed to load {cand}: {e}", LOG_LEVEL_ERROR)
        _MYSTERY_POOL_OVERRIDES = data
    # Try int key first (modern config format), then str key (legacy)
    return _MYSTERY_POOL_OVERRIDES.get(item_id) or _MYSTERY_POOL_OVERRIDES.get(str(item_id)) or []


_MYSTERY_POOL_OVERRIDES = None

# ── Data-derived pools for OPAQUE mystery boxes ─────────────────────────────
# Two series of mystery virtual items exist in the client data:
#   * type 99 (54001-54003, 54011-54018, 54021-54025, 54041-54045): the EN
#     data SHIPS their official display_icon pools — these resolve through
#     the plain display_icon path and never need derivation. Their display
#     texts declare CUMULATIVE tier ranges ("random T1-T2 skill book",
#     "T1~T2 Tech Pack", "T2-T3 gear part").
#   * type 98 (54004-54007, 54031-54035/54039, 54049-54051, 52004): OPAQUE —
#     display_icon {} in every region; originally resolves them internally
#     at grant time. Their displays declare SAME-TIER drops
#     ("random T2 skill book" / "random T3 Tech Pack").
# The derivation below reproduces the official pools from the item configs,
# reading the tier RANGE from the box's own display text when present
# ("T1-T2" / "T1~T2", all regions ship it) so both series are handled.
# Verified against the original data:
#   * skill books: CN display_icon pools are SAME-TIER-ONLY for the type-98
#     series (54005 -> ONLY T2 books) and the type-99 series is cumulative
#     (54002 -> T1+T2) — the derivation reproduces both exactly.
#   * tech packs: non-opaque sibling 69959 "Mystery T5 Tech Pack" display_icon
#     = [30015,30025,30035,30045]; type-99 54022 = T1+T2 packs (8 entries).
#   * gear parts: 54011 official display_icon = [17001,17011,17021,17031,17041]
#     (5 part categories: General/Main Gun/Torpedo/Anti-Air Gun/Aircraft).
#   * retrofit blueprints: per-hull type-99 boxes (54041 -> [18001,18002]);
#     type-98 boxes span all 4 hull types (DD/CL/BB/CV — T4 blueprints do
#     NOT exist; a previous hand-written pool referenced ghost items
#     18004/18014/18024 and skipped the Carrier line entirely).
#   * Random Gear Design T{n}: client data self-contradicts (EN/JP NAME says
#     T2, CN/TW name says "Elite design", JP/KR display says "SR or lower");
#     we follow the item name the EN player sees (display carries no tier).
#     Pin a manual override in configurations/mystery_box_pools.json if a
#     real capture ever contradicts this.
# "Mystery Ship" boxes: the per-stage event boxes (2008xx, type 99) carry
# curated official display_icon pools of ships (drop type 4) and resolve
# through those. The generic opaque ones (56000/56500-56502, type 98) are
# derived from their display text's rarity ceiling ("SR or lower" / "Elite or
# lower" / "R or lower" / "Common") over ship_data_statistics — a faithful
# reading of the client text (the original pool is not shipped in any region).
_MEMBER_PATTERNS = {
    # family -> (member item type, member-name regex builder(tier, hull))
    "skill_book":  (10, lambda t, _hull: re.compile(rf"^T{t} .* Skill Book$")),
    "tech_pack":   (5,  lambda t, _hull: re.compile(rf"^T{t} .* Tech Pack$")),
    "gear_part":   (4,  lambda t, _hull: re.compile(rf"^T{t} .* Part$")),
    "retrofit":    (7,  lambda t, hull: (
        re.compile(rf"^T{t} {hull} Retrofit Blueprint$") if hull else
        re.compile(rf"^T{t} .* Retrofit Blueprint$"))),
    "gear_design": (9,  lambda t, _hull: re.compile(rf"\bT{t}\b")),
}


def _mystery_family(name: str):
    """Match a mystery box name -> (family, name_tier, hull) or None."""
    m = re.match(r"^T(\d+) Mystery Skill Book$", name)
    if m:
        return ("skill_book", int(m.group(1)), None)
    m = re.match(r"^Mystery T(\d+) Tech Pack$", name)
    if m:
        return ("tech_pack", int(m.group(1)), None)
    m = re.match(r"^T(\d+) Mystery Gear Part$", name)
    if m:
        return ("gear_part", int(m.group(1)), None)
    m = re.match(r"^T(\d+) Mystery Retrofit Blueprint$", name)
    if m:
        return ("retrofit", int(m.group(1)), None)
    m = re.match(r"^Mystery ([A-Za-z]+) Retrofit Blueprint$", name)
    if m:
        return ("retrofit", None, m.group(1))
    m = re.match(r"^Mystery Ship$", name)
    if m:
        return ("mystery_ship", None, None)
    m = re.match(r"^Random Gear Design T(\d+)$", name)
    if m:
        return ("gear_design", int(m.group(1)), None)
    return None


def _display_tier_range(cfg: dict):
    """Tier list declared by the box's own display text, or None.

    Handles the cumulative series: 'random T1-T2 skill book' (EN books),
    'T1~T2 Tech Pack' (EN tech packs use a tilde), 'T2-T3 gear part'.
    """
    disp = str(cfg.get("display") or "")
    m = re.search(r"\bT(\d+)\s*[-~]\s*T(\d+)\b", disp)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 1 <= lo <= hi:
            return list(range(lo, hi + 1))
    return None


_SHIP_CONFIG_INDEX: Optional[list] = None


def _ship_config_index() -> list:
    """[(ship_id, rarity)] for every ship in ship_data_statistics, built once."""
    global _SHIP_CONFIG_INDEX
    if _SHIP_CONFIG_INDEX is None:
        from src.logger.logger import log_event, LOG_LEVEL_ERROR
        from src.orm.config_entry import list_config_entries_sync
        ships = []
        try:
            entries = list_config_entries_sync(
                "sharecfgdata/ship_data_statistics.json")
        except Exception as e:
            log_event("Items", "MysteryPool",
                      f"failed to list ship configs: {e}", LOG_LEVEL_ERROR)
            entries = []
        for entry in entries:
            data = getattr(entry, "data", None)
            if not isinstance(data, dict):
                try:
                    data = json.loads(data)
                except (ValueError, TypeError):
                    continue
            if not isinstance(data, dict):
                continue
            try:
                ships.append((int(data.get("id") or getattr(entry, "key", 0)),
                              int(data.get("rarity") or 0)))
            except (TypeError, ValueError):
                continue
        _SHIP_CONFIG_INDEX = ships
    return _SHIP_CONFIG_INDEX


def _mystery_ship_rarity_ceiling(cfg: dict) -> Optional[int]:
    """Rarity ceiling declared by a 'Mystery Ship' box's display text.

    EN displays: 'SR or lower' -> 5, 'Elite or lower' -> 4, 'R or lower' -> 3,
    'Common' -> 2 (ship_data_statistics rarity scale: 2=Common, 3=Rare,
    4=Elite, 5=Super Rare, 6=Ultra Rare; rarity 18 entries are special and
    always excluded).
    """
    disp = str(cfg.get("display") or "")
    if "SR or lower" in disp:
        return 5
    if "Elite or lower" in disp:
        return 4
    if "R or lower" in disp:
        return 3
    if "Common" in disp:
        return 2
    return None

_ITEM_CONFIG_INDEX: Optional[dict] = None


def _item_config_index() -> dict:
    """All real+virtual item configs (str id -> config dict), built once."""
    global _ITEM_CONFIG_INDEX
    if _ITEM_CONFIG_INDEX is None:
        from src.logger.logger import log_event, LOG_LEVEL_ERROR
        from src.orm.config_entry import list_config_entries_sync
        index: dict = {}
        for category in ("sharecfgdata/item_data_statistics.json",
                         "sharecfgdata/item_virtual_data_statistics.json"):
            try:
                entries = list_config_entries_sync(category)
            except Exception as e:
                log_event("Items", "MysteryPool",
                          f"failed to list config entries for {category}: {e}",
                          LOG_LEVEL_ERROR)
                continue
            for entry in entries:
                data = getattr(entry, "data", None)
                if not isinstance(data, dict):
                    try:
                        data = json.loads(data)
                    except (ValueError, TypeError):
                        continue
                if isinstance(data, dict):
                    index[str(data.get("id") or getattr(entry, "key", ""))] = data
        _ITEM_CONFIG_INDEX = index
    return _ITEM_CONFIG_INDEX


def _derived_mystery_pool(item_id: int, cfg: dict) -> list:
    """Derive the drop pool for an opaque mystery box from item configs.

    Matches the box's config name against the known families, takes the tier
    (or cumulative tier range) from the box's own display text — falling back
    to the tier embedded in its name — and collects the matching same-family
    real items of every tier in range. Returns [[2, item_id, 1], ...] (sorted
    by item id) or [] when the box matches no family.
    """
    name = str(cfg.get("name") or "")
    fam = _mystery_family(name)
    if fam is None:
        return []
    family, name_tier, hull = fam
    if family == "mystery_ship":
        # display declares a rarity ceiling ("Chance to receive R or lower
        # ships") -> pool = every standard ship at or below it. Ships drop as
        # drop type 4 (granted via add_ship in _apply_resolved_entry). The
        # official per-stage "Mystery Ship" boxes (2008xx) carry curated
        # display_icon pools and resolve through those instead.
        ceiling = _mystery_ship_rarity_ceiling(cfg)
        if ceiling is None:
            return []
        # id >= 900000 entries are shadow/variant copies (and the "Hero"
        # placeholder) — no official curated ship pool ever grants them.
        pool = [[4, sid, 1] for sid, rarity in _ship_config_index()
                if 2 <= rarity <= ceiling and sid < 900000]
        pool.sort(key=lambda e: e[1])
        return pool
    tiers = _display_tier_range(cfg)
    if tiers is None:
        if name_tier is None:
            return []  # per-hull box without a tier range: cannot determine
        tiers = [name_tier]
    member_type, member_re_fn = _MEMBER_PATTERNS[family]
    pool = []
    for tier in tiers:
        member_re = member_re_fn(tier, hull)
        for iid, data in _item_config_index().items():
            if str(data.get("id") or iid) == str(item_id):
                continue
            try:
                if int(data.get("type") or 0) != member_type:
                    continue
                member_id = int(iid)
            except (TypeError, ValueError):
                continue
            if member_re.search(str(data.get("name") or "")):
                pool.append([2, member_id, 1])
    # dedupe + deterministic order
    seen = set()
    unique = []
    for entry in sorted(pool, key=lambda e: e[1]):
        if entry[1] not in seen:
            seen.add(entry[1])
            unique.append(entry)
    return unique


def resolve_virtual_item_drops(item_id: int, count: int, orig_type: int = DROP_TYPE_ITEM, _depth: int = 0) -> list:
    """Resolve a virtual "mystery box" item into its random contents.

    Virtual items whose config has a non-empty ``display_icon`` pool are
    expanded into one randomly-chosen entry per ``count``. Nested virtual
    items are resolved recursively (depth-capped). Non-virtual items and
    virtual items without a pool fall back to configurations/
    mystery_box_pools.json and are returned unchanged if no override exists.
    Real usable/selectable items (config has a usage) always pass through
    unchanged -- their display_icon only previews options and the player opens
    them from the depot via the item-use flow.
    """
    if _depth > 5:
        return [(orig_type, item_id, count)]
    # "Coins" / "Oil" are real-resource proxies, never bag items (see
    # RESOURCE_VIRTUAL_ITEMS). Resolve them straight to the resource drop so
    # every caller applies them to the resource pool.
    if item_id in RESOURCE_VIRTUAL_ITEMS:
        return [(DROP_TYPE_RESOURCE, RESOURCE_VIRTUAL_ITEMS[item_id], count)]
    cfg = _load_virtual_item_config(item_id)
    if not cfg:
        return [(orig_type, item_id, count)]
    usage = cfg.get("usage", "")
    if usage in ("usgae_drop_template", "usage_drop_template"):
        # Template packs carry their own gold/oil (usage_arg) + one random
        # item from display_icon. Resolve through the same logic the live
        # grant uses so drops/popups match the actual contents.
        from src.answer.item_usage import _build_template_drop_entries
        return _build_template_drop_entries(cfg, count)
    if cfg.get("open_directly", 0) == 1:
        from src.answer.item_usage import _display_icon_is_random
        usage_str = str(cfg.get("usage") or "")
        is_drop_template = usage_str in ("usgae_drop_template", "usage_drop_template")
        if is_drop_template or not _display_icon_is_random(cfg):
            # Fixed auto-open bundles (e.g. Decor Tokens Pack, Promise Crate,
            # Lucky Bags) auto-open upon receipt into their full contents.
            # Resolve them through the same preparation logic so callers
            # (shops, commissions, missions, battles) send the concrete drops
            # to the client rather than an unopenable wrapper box.
            from src.answer.item_usage import _prepare_item_usage
            client = _AutoOpenClient(0)
            plan = _prepare_item_usage(client, cfg, [], count)
            if plan is not None and plan.get("result", 1) == 0 and plan.get("drop_list"):
                result = []
                for d in plan["drop_list"]:
                    dt = int(d["type"])
                    di = int(d["id"])
                    dc = int(d["number"])
                    if dt in (DROP_TYPE_ITEM, DROP_TYPE_VITEM):
                        result.extend(resolve_virtual_item_drops(di, dc, dt, _depth + 1))
                    else:
                        result.append((dt, di, dc))
                return result
    if usage:
        # A REAL usable/selectable item (usage_drop*, usage_invitation, ...) --
        # e.g. General Blueprints, Selection Gear Skin Boxes, Letters, paid
        # packs. Its display_icon only PREVIEWS the options/contents; the item
        # itself must land in the depot and be opened/selected later through
        # the item-use flow (src/answer/item_usage.py), never be pre-resolved
        # into one random option here. (Virtual grant markers -- the mystery
        # 98/99 family and resource proxies -- carry no usage.)
        return [(orig_type, item_id, count)]
    if item_id in _FIXED_BUNDLE_VIRTUAL_IDS:
        # Fixed-bundle virtual (Cognitive Datapack / Black Friday Coupon &
        # Cubes): display_icon is the item's full contents, not a pool. Grant
        # every entry, each scaled by ``count``.
        entries = _parse_icon_list(cfg.get("display_icon"))
        if not entries:
            return [(orig_type, item_id, count)]
        result = []
        for e in entries:
            if not (isinstance(e, list) and len(e) >= 3):
                continue
            t, i, c = int(e[0]), int(e[1]), int(e[2])
            if t in (DROP_TYPE_ITEM, DROP_TYPE_VITEM):
                result.extend(resolve_virtual_item_drops(i, c * count, t, _depth + 1))
            else:
                result.append((t, i, c * count))
        return result
    entries = _parse_icon_list(cfg.get("display_icon"))
    if not entries:
        entries = _derived_mystery_pool(item_id, cfg)
    if not entries:
        entries = _parse_icon_list(_mystery_pool_override(item_id))
    if not entries:
        if int(cfg.get("type") or 0) in (98, 99):
            from src.logger.logger import log_event, LOG_LEVEL_WARN
            log_event("Items", "MysteryPool",
                      f"mystery box {item_id} ({cfg.get('name')}) has no "
                      f"resolvable pool; granting the raw virtual item",
                      LOG_LEVEL_WARN)
        return [(orig_type, item_id, count)]
    result = []
    for _ in range(count):
        e = entries[random.randint(0, len(entries) - 1)]
        if not (isinstance(e, list) and len(e) >= 3):
            continue
        t, i, c = int(e[0]), int(e[1]), int(e[2])
        if t in (DROP_TYPE_ITEM, DROP_TYPE_VITEM):
            # Only item-ish drops recurse into the ITEM config tables.
            # Resource (1) / ship (4) / skin (7) ids must be passed through
            # untouched: ship and item id ranges overlap, so looking a ship id
            # up in the item tables could resolve it into something else.
            result.extend(resolve_virtual_item_drops(i, c, t, _depth + 1))
        else:
            result.append((t, i, c))
    return result


def upsert_item_count(commander_id: int, type_id: int, count: int):
    with get_sync_session() as session:
        result = session.execute(
            select(CommanderItem).where(
                CommanderItem.commander_id == commander_id,
                CommanderItem.item_id == type_id,
            )
        )
        obj = result.scalar_one_or_none()
        if obj is None:
            obj = CommanderItem(commander_id=commander_id, item_id=type_id, count=count)
            session.add(obj)
        else:
            obj.count = count
        session.commit()


def get_commander_item_count(commander_id: int, type_id: int) -> int:
    from sqlalchemy import func
    with get_sync_session() as session:
        result = session.execute(
            select(func.coalesce(func.sum(CommanderItem.count), 0)).where(
                CommanderItem.commander_id == commander_id,
                CommanderItem.item_id == type_id,
            )
        )
        return result.scalar() or 0

def consume_commander_item(commander_id: int, type_id: int, count: int):
    consume_item(commander_id, type_id, count)

class Item(Base):
    __tablename__ = 'items'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, default='')
    rarity: Mapped[int] = mapped_column(BigInteger, default=0)
    shop_id: Mapped[int] = mapped_column(BigInteger, default=0)
    type: Mapped[int] = mapped_column(BigInteger, default=0)
    virtual_type: Mapped[int] = mapped_column(BigInteger, default=0)
