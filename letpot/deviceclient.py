"""Python client for LetPot hydrophonic gardens."""

import asyncio
import dataclasses
from datetime import time
from hashlib import md5, sha256
import logging
import os
import time as systime
import ssl
from typing import Callable
import aiomqtt

from letpot.converters import CONVERTERS, LetPotDeviceConverter
from letpot.exceptions import LetPotAuthenticationException, LetPotException
from letpot.models import AuthenticationInfo, LetPotDeviceStatus

_LOGGER = logging.getLogger(__name__)


def _create_ssl_context() -> ssl.SSLContext:
    """Create a SSL context for the MQTT connection, avoids a blocking call later."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS)
    context.load_default_certs()
    return context


_SSL_CONTEXT = _create_ssl_context()


class LetPotDeviceClient:
    """Client for connecting to LetPot device."""

    BROKER_HOST = "broker.letpot.net"
    MTU = 128

    _client: aiomqtt.Client | None = None
    _connection_attempts: int = 0
    _converter: LetPotDeviceConverter | None = None
    _message_id: int = 0
    _user_id: str | None = None
    _email: str | None = None
    _device_serial: str | None = None
    last_status: LetPotDeviceStatus | None = None

    def __init__(self, info: AuthenticationInfo, device_serial: str) -> None:
        self._user_id = info.user_id
        self._email = info.email
        self._device_serial = device_serial

        device_type = self._device_serial[:5]
        for converter in CONVERTERS:
            if converter.supports_type(device_type):
                self._converter = converter
                break

    def _generate_client_id(self) -> str:
        """Generate a client identifier for the connection."""
        return f"LetPot_{round(systime.time() * 1000)}_{os.urandom(4).hex()[:8]}"

    def _generate_message_packets(
        self, maintype: int, subtype: int, message: list[int]
    ) -> list[str]:
        """Convert a message to one or more packets with the message payload."""
        length = len(message)
        max_packet_size = self.MTU - 6
        num_packets = (length + max_packet_size - 1) // max_packet_size

        packets = []
        for n in range(num_packets):
            start = n * max_packet_size
            end = min(start + max_packet_size, length)
            payload = message[start:end]

            if n < num_packets - 1:
                packet = [
                    (subtype << 2) | maintype,
                    16,
                    self._message_id,
                    len(payload) + 4,
                    length % 256,
                    length // 256,
                    *payload,
                ]
            else:
                packet = [
                    (subtype << 2) | maintype,
                    0,
                    self._message_id,
                    len(payload),
                    *payload,
                ]

            packets.append("".join(f"{byte:02x}" for byte in packet))
            self._message_id += 1

        return packets

    async def _handle_messages(
        self, callback: Callable[[LetPotDeviceStatus], None]
    ) -> None:
        """Process incoming messages from the broker."""
        async for message in self._client.messages:
            if self._converter is not None:
                status = self._converter.convert_hex_to_status(message.payload)
                if status is not None:
                    self.last_status = status
                    callback(status)

    async def _publish(self, message: list[int]) -> None:
        """Publish a message to the device command topic."""
        if self._client is None:
            raise LetPotException("Missing client to publish message with")

        messages = self._generate_message_packets(
            1, 19, message
        )  # maintype 1: data, subtype 19: custom
        topic = f"{self._device_serial}/cmd"
        for publish_message in messages:
            await self._client.publish(topic, payload=publish_message)

    async def subscribe(self, callback: Callable[[LetPotDeviceStatus], None]) -> None:
        """Subscribe to state updates for this device."""
        username = f"{self._email}__letpot_v3"
        password = sha256(
            f"{self._user_id}|{md5(username.encode()).hexdigest()}".encode()
        ).hexdigest()
        while True:
            try:
                async with (
                    aiomqtt.Client(
                        hostname=self.BROKER_HOST,
                        port=443,
                        username=username,
                        password=password,
                        identifier=self._generate_client_id(),
                        protocol=aiomqtt.ProtocolVersion.V5,
                        transport="websockets",
                        tls_context=_SSL_CONTEXT,
                        tls_insecure=False,
                        websocket_path="/mqttwss",
                    ) as client,
                    asyncio.TaskGroup() as tg,
                ):
                    self._client = client
                    self._connection_attempts = 0
                    self._message_id = 0

                    await client.subscribe(f"{self._device_serial}/data")

                    tg.create_task(self._handle_messages(callback))
                    tg.create_task(
                        self._publish(self._converter.get_current_status_message())
                    )
            except aiomqtt.MqttError as err:
                self._client = None

                if isinstance(err, aiomqtt.MqttCodeError):
                    if err.rc in [4, 5, 134, 135]:
                        msg = "MQTT auth error"
                        _LOGGER.error("%s: %s", msg, err)
                        raise LetPotAuthenticationException(msg) from err

                self._connection_attempts += 1
                reconnect_interval = min(self._connection_attempts * 15, 600)
                _LOGGER.error(
                    "MQTT error, reconnecting in %i seconds: %s",
                    reconnect_interval,
                    err,
                )

                await asyncio.sleep(reconnect_interval)
            finally:
                self._client = None

    async def set_light_brightness(self, level: int) -> None:
        """Set the light brightness for this device (brightness level)."""
        device_type = self._device_serial[:5]
        if level not in self._converter.get_light_brightness_levels(device_type):
            raise LetPotException(
                f"Device doesn't support setting light brightness to {level}"
            )
        if self.last_status is None:
            raise LetPotException("Client doesn't have a status to update")

        new_status = dataclasses.replace(self.last_status, light_brightness=level)
        await self._publish(self._converter.get_update_status_message(new_status))

    async def set_light_mode(self, mode: int) -> None:
        """Set the light mode for this device (flower/vegetable)."""
        if self.last_status is None:
            raise LetPotException("Client doesn't have a status to update")

        new_status = dataclasses.replace(self.last_status, light_mode=mode)
        await self._publish(self._converter.get_update_status_message(new_status))

    async def set_light_schedule(self, start: time | None, end: time | None) -> None:
        """Set the light schedule for this device (start time and/or end time)."""
        if self.last_status is None:
            raise LetPotException("Client doesn't have a status to update")

        start_time = self.last_status.light_schedule_start if start is None else start
        end_time = self.last_status.light_schedule_end if end is None else end
        new_status = dataclasses.replace(
            self.last_status,
            light_schedule_start=start_time,
            light_schedule_end=end_time,
        )
        await self._publish(self._converter.get_update_status_message(new_status))

    async def set_plant_days(self, days: int) -> None:
        """Set the plant days counter for this device (number of days)."""
        if self.last_status is None:
            raise LetPotException("Client doesn't have a status to update")

        new_status = dataclasses.replace(self.last_status, plant_days=days)
        await self._publish(self._converter.get_update_status_message(new_status))

    async def set_power(self, on: bool) -> None:
        """Set the general power for this device (on/off)."""
        if self.last_status is None:
            raise LetPotException("Client doesn't have a status to update")

        new_status = dataclasses.replace(self.last_status, system_on=on)
        await self._publish(self._converter.get_update_status_message(new_status))

    async def set_pump_mode(self, on: bool) -> None:
        """Set the pump mode for this device (on (scheduled)/off)."""
        if self.last_status is None:
            raise LetPotException("Client doesn't have a status to update")

        new_status = dataclasses.replace(self.last_status, pump_mode=1 if on else 0)
        await self._publish(self._converter.get_update_status_message(new_status))

    async def set_sound(self, on: bool) -> None:
        """Set the alarm sound for this device (on/off)."""
        if self.last_status is None:
            raise LetPotException("Client doesn't have a status to update")

        new_status = dataclasses.replace(self.last_status, system_sound=on)
        await self._publish(self._converter.get_update_status_message(new_status))
