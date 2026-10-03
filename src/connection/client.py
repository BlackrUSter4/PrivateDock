import asyncio
from dataclasses import dataclass
from typing import Optional

from src.orm.commander import create_commander


@dataclass
class ClientMetrics:
    queue_max: int = 0
    queue_blocks: int = 0
    handler_errors: int = 0
    write_errors: int = 0
    packets: int = 0


@dataclass
class MetricsSnapshot:
    queue_max: int = 0
    queue_blocks: int = 0
    handler_errors: int = 0
    write_errors: int = 0
    packets: int = 0


class Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, server=None):
        self.reader = reader
        self.writer = writer
        self.ip = writer.get_extra_info("peername")[0] if writer.get_extra_info("peername") else "0.0.0.0"
        self.port = writer.get_extra_info("peername")[1] if writer.get_extra_info("peername") else 0
        self.server = server
        self.packet_index = 0
        self.commander = None
        self.auth_arg2 = 0
        self.connected_at = None
        self.previous_login_at = None
        self._login_backup_scheduled = False
        self._closed = False
        self._metrics = ClientMetrics()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._buffer = bytearray()

    @property
    def hash(self) -> int:
        h = 0
        for c in f"{self.ip}:{self.port}":
            h += ord(c)
        return h & 0xFFFFFFFF

    def start_dispatcher(self):
        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._dispatcher_task = asyncio.create_task(self._dispatch_loop())

    async def enqueue_packet(self, packet: bytes):
        try:
            await asyncio.wait_for(self._queue.put(packet), timeout=None)
        except asyncio.QueueFull:
            self._metrics.queue_blocks += 1

    async def _dispatch_loop(self):
        while not self._closed:
            packet = await self._queue.get()
            if self._closed:
                break
            self._metrics.packets += 1
            if self.server:
                await self.server.dispatcher(packet, self, len(packet))

    async def flush(self):
        if self._buffer and self.writer and not self._closed:
            try:
                self.writer.write(bytes(self._buffer))
                await self.writer.drain()
            except Exception:
                self._metrics.write_errors += 1
            finally:
                self._buffer.clear()

    async def send_message(self, packet_id: int, message) -> tuple[int, int, Optional[Exception]]:
        from src.connection.server import send_proto_message
        return await send_proto_message(packet_id, self, message)

    def write_to_buffer(self, data: bytes):
        # Skip 0-byte protobuf payloads (header size=5 means empty payload)
        #if len(data) == 7 and data[0] == 0 and data[1] == 5:
        #    return
        self._buffer.extend(data)

    def record_handler_error(self):
        self._metrics.handler_errors += 1

    async def disconnect(self, reason: int):
        from src.connection.server import send_proto_message
        from src.protobuf import protobuf
        await send_proto_message(10999, self, protobuf.SC_10999(reason=reason))
        await self.close()

    async def close(self):
        if self._closed:
            return
        if self.commander is not None:
            try:
                from src.orm.active_commander import unregister_active_commander, unregister_active_client
                unregister_active_commander(self.commander)
                unregister_active_client(self.commander.commander_id)
            except Exception:
                pass
        self._closed = True
        if self._dispatcher_task and not self._dispatcher_task.done():
            self._dispatcher_task.cancel()
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

    async def close_with_error(self):
        await self.close()

    def is_closed(self) -> bool:
        return self._closed

    def create_commander(self, arg2: int, nickname: str = None, extra_ship_ids: list[int] = None) -> int:
        account_id = asyncio.run(create_commander(self, arg2, nickname, extra_ship_ids))
        self.commander_id = account_id
        return account_id

    def get_commander(self, accountId: int):
        self.commander_id = accountId

    def create_commander_with_starter(self, arg2: int, nickname: str, ship_id: int) -> int:
        return self.create_commander(arg2, nickname, [ship_id])

    @property
    def metrics_snapshot(self) -> MetricsSnapshot:
        return MetricsSnapshot(
            queue_max=self._metrics.queue_max,
            queue_blocks=self._metrics.queue_blocks,
            handler_errors=self._metrics.handler_errors,
            write_errors=self._metrics.write_errors,
            packets=self._metrics.packets,
        )
