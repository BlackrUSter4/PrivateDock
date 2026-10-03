import asyncio
from typing import Optional

from src.connection.client import Client
from src.protobuf import protobuf
from src.db.async_lock import create_locked_task


def build_resource_snapshot(resources) -> list[dict]:
    if not resources:
        return []
    return [{"type": r.id, "num": r.count} for r in resources]


def send_player_resource_sync(client: Client) -> tuple[int, int, Optional[Exception]]:
    if client is None or client.commander is None:
        return 0, 11004, Exception("missing commander")
    from src.orm.resource import list_owned_resources_sync

    response = protobuf.SC_11004()
    for row in list_owned_resources_sync(client.commander.commander_id):
        response.resource_list.append(
            protobuf.RESOURCE(type=int(row["resource_id"]), num=int(row["amount"]))
        )
    create_locked_task(client.send_message(11004, response))
    return 0, 11004, None
