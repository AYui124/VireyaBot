"""SnowLuma configuration-to-gateway composition.

This module is the only place that couples the database configuration service to
OneBot transport. It does not start a connection or expose raw payloads to the
agent layer.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.onebot import SnowLumaConfig, SnowLumaGateway
from app.repository import read_snowluma_runtime_settings


async def build_snowluma_gateway(session: AsyncSession) -> SnowLumaGateway:
    values = await read_snowluma_runtime_settings(session)
    return SnowLumaGateway(SnowLumaConfig.from_settings(values))


async def send_private_message(gateway: SnowLumaGateway, user_id: str | int, message: object):
    return await gateway.call("send_private_msg", {"user_id": user_id, "message": message})


async def send_group_message(gateway: SnowLumaGateway, group_id: str | int, message: object):
    return await gateway.call("send_group_msg", {"group_id": group_id, "message": message})
