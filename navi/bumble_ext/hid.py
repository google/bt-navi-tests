#  Copyright 2025 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""Bluetooth Human Interface Device (HID) profile extended implementation."""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import logging
from typing import Any, Generic, Self, TypeAlias, TypeVar

from bumble import core
from bumble import device
from bumble import hid
from bumble import l2cap
from bumble import sdp
import pyee
from typing_extensions import override

_logger = logging.getLogger(__name__)

Message: TypeAlias = hid.Message


class AttributeId(enum.IntEnum):
  """SDP attribute IDs."""

  SERVICE_NAME = 0x0100
  SERVICE_DESCRIPTION = 0x0101
  PROVIDER_NAME = 0x0102
  DEVICE_RELEASE_NUMBER = 0x0200  # [DEPRECATED]
  PARSER_VERSION = 0x0201
  DEVICE_SUBCLASS = 0x0202
  COUNTRY_CODE = 0x0203
  VIRTUAL_CABLE = 0x0204
  RECONNECT_INITIATE = 0x0205
  DESCRIPTOR_LIST = 0x0206
  LANGID_BASE_LIST = 0x0207
  SDP_DISABLE = 0x0208  # [DEPRECATED]
  BATTERY_POWER = 0x0209
  REMOTE_WAKE = 0x020A
  PROFILE_VERSION = 0x020B  # [DEPRECATED]
  SUPERVISION_TIMEOUT = 0x020C
  NORMALLY_CONNECTABLE = 0x020D
  BOOT_DEVICE = 0x020E
  SSR_HOST_MAX_LATENCY = 0x020F
  SSR_HOST_MIN_TIMEOUT = 0x0210


@dataclasses.dataclass
class SdpInformation:
  """HID Device SDP information."""

  service_record_handle: int
  version_number: int
  parser_version: int
  device_subclass: int
  country_code: int
  virtual_cable: bool
  reconnect_initiate: bool
  report_descriptor_type: int
  report_map: bytes
  langid_base_language: int
  langid_base_bluetooth_string_offset: int
  boot_device: bool
  battery_power: bool | None = None
  remote_wake: bool | None = None
  supervision_timeout: int | None = None
  normally_connectable: bool | None = None
  service_name: bytes | None = None
  service_description: bytes | None = None
  provider_name: bytes | None = None
  ssr_host_max_latency: int | None = None
  ssr_host_min_timeout: int | None = None


def make_device_sdp_record(
    service_record_handle: int,
    report_map: bytes,
    version_number: int = 0x0101,  # 0x0101 uint16 version number (v1.1)
    service_name: bytes = b"Bumble HID",
    service_description: bytes = b"Bumble",
    provider_name: bytes = b"Bumble",
    parser_version: int = 0x0111,  # uint16 0x0111 (v1.1.1)
    device_subclass: int = 0xC0,  # Combo keyboard/pointing device
    country_code: int = 0x21,  # 0x21 Uint8, USA
    virtual_cable: bool = True,  # Virtual cable enabled
    reconnect_initiate: bool = True,  #  Reconnect initiate enabled
    report_descriptor_type: int = 0x22,  # 0x22 Type = Report Descriptor
    langid_base_language: int = 0x0409,  # 0x0409 Language = en_US
    langid_base_bluetooth_string_offset: int = 0x100,  # 0x0100 Default
    battery_power: bool | None = True,  #  Battery power enabled
    remote_wake: bool | None = True,  #  Remote wake enabled
    supervision_timeout: int | None = 0xC80,  # uint16 0xC80 (2s)
    normally_connectable: bool | None = True,  #  Normally connectable enabled
    boot_device: bool = True,  #  Boot device support enabled
    ssr_host_max_latency: int | None = 0x640,  # uint16 0x640 (1s)
    ssr_host_min_timeout: int | None = 0xC80,  # uint16 0xC80 (2s)
) -> list[sdp.ServiceAttribute]:
  """Makes the SDP record of the device."""
  attributes = [
      sdp.ServiceAttribute(
          sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
          sdp.DataElement.unsigned_integer_32(service_record_handle),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_BROWSE_GROUP_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence(
              [sdp.DataElement.uuid(sdp.SDP_PUBLIC_BROWSE_ROOT)]
          ),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([
              sdp.DataElement.uuid(core.BT_HUMAN_INTERFACE_DEVICE_SERVICE),
          ]),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID),
                  sdp.DataElement.unsigned_integer_16(hid.HID_CONTROL_PSM),
              ]),
              sdp.DataElement.sequence(
                  [sdp.DataElement.uuid(core.BT_HIDP_PROTOCOL_ID)]
              ),
          ]),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_LANGUAGE_BASE_ATTRIBUTE_ID_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([
              sdp.DataElement.unsigned_integer_16(0x656E),  # "en"
              sdp.DataElement.unsigned_integer_16(0x6A),
              sdp.DataElement.unsigned_integer_16(0x0100),
          ]),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_HUMAN_INTERFACE_DEVICE_SERVICE),
                  sdp.DataElement.unsigned_integer_16(version_number),
              ]),
          ]),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_ADDITIONAL_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([
              sdp.DataElement.sequence([
                  sdp.DataElement.sequence([
                      sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID),
                      sdp.DataElement.unsigned_integer_16(
                          hid.HID_INTERRUPT_PSM
                      ),
                  ]),
                  sdp.DataElement.sequence([
                      sdp.DataElement.uuid(core.BT_HIDP_PROTOCOL_ID),
                  ]),
              ]),
          ]),
      ),
      sdp.ServiceAttribute(
          AttributeId.SERVICE_NAME,
          sdp.DataElement(sdp.DataElement.TEXT_STRING, service_name),
      ),
      sdp.ServiceAttribute(
          AttributeId.SERVICE_DESCRIPTION,
          sdp.DataElement(sdp.DataElement.TEXT_STRING, service_description),
      ),
      sdp.ServiceAttribute(
          AttributeId.PROVIDER_NAME,
          sdp.DataElement(sdp.DataElement.TEXT_STRING, provider_name),
      ),
      sdp.ServiceAttribute(
          AttributeId.PARSER_VERSION,
          sdp.DataElement.unsigned_integer_32(parser_version),
      ),
      sdp.ServiceAttribute(
          AttributeId.DEVICE_SUBCLASS,
          sdp.DataElement.unsigned_integer_32(device_subclass),
      ),
      sdp.ServiceAttribute(
          AttributeId.COUNTRY_CODE,
          sdp.DataElement.unsigned_integer_32(country_code),
      ),
      sdp.ServiceAttribute(
          AttributeId.VIRTUAL_CABLE,
          sdp.DataElement.boolean(virtual_cable),
      ),
      sdp.ServiceAttribute(
          AttributeId.RECONNECT_INITIATE,
          sdp.DataElement.boolean(reconnect_initiate),
      ),
      sdp.ServiceAttribute(
          AttributeId.DESCRIPTOR_LIST,
          sdp.DataElement.sequence([
              sdp.DataElement.sequence([
                  sdp.DataElement.unsigned_integer_16(report_descriptor_type),
                  sdp.DataElement(sdp.DataElement.TEXT_STRING, report_map),
              ]),
          ]),
      ),
      sdp.ServiceAttribute(
          AttributeId.LANGID_BASE_LIST,
          sdp.DataElement.sequence([
              sdp.DataElement.sequence([
                  sdp.DataElement.unsigned_integer_16(langid_base_language),
                  sdp.DataElement.unsigned_integer_16(
                      langid_base_bluetooth_string_offset
                  ),
              ]),
          ]),
      ),
      sdp.ServiceAttribute(
          AttributeId.BOOT_DEVICE,
          sdp.DataElement.boolean(boot_device),
      ),
  ]
  if battery_power is not None:
    attributes.append(
        sdp.ServiceAttribute(
            AttributeId.BATTERY_POWER,
            sdp.DataElement.boolean(battery_power),
        )
    )
  if remote_wake is not None:
    attributes.append(
        sdp.ServiceAttribute(
            AttributeId.REMOTE_WAKE,
            sdp.DataElement.boolean(remote_wake),
        )
    )
  if supervision_timeout is not None:
    attributes.append(
        sdp.ServiceAttribute(
            AttributeId.SUPERVISION_TIMEOUT,
            sdp.DataElement.unsigned_integer_16(supervision_timeout),
        )
    )
  if normally_connectable is not None:
    attributes.append(
        sdp.ServiceAttribute(
            AttributeId.NORMALLY_CONNECTABLE,
            sdp.DataElement.boolean(normally_connectable),
        )
    )
  if ssr_host_max_latency is not None:
    attributes.append(
        sdp.ServiceAttribute(
            AttributeId.SSR_HOST_MAX_LATENCY,
            sdp.DataElement.unsigned_integer_16(ssr_host_max_latency),
        )
    )
  if ssr_host_min_timeout is not None:
    attributes.append(
        sdp.ServiceAttribute(
            AttributeId.SSR_HOST_MIN_TIMEOUT,
            sdp.DataElement.unsigned_integer_16(ssr_host_min_timeout),
        )
    )
  return attributes


async def find_device_sdp_record(
    connection: device.Connection,
) -> SdpInformation | None:
  """Finds the SDP record of the device."""

  async with sdp.Client(connection) as sdp_client:
    service_record_handles = await sdp_client.search_services(
        [core.BT_HUMAN_INTERFACE_DEVICE_SERVICE]
    )
    if not service_record_handles:
      return None
    if len(service_record_handles) > 1:
      _logger.info(
          "Remote has more than one HID SDP records, only return the first one."
      )

    service_record_handle = service_record_handles[0]
    attr: dict[str, Any] = {"service_record_handle": service_record_handle}

    attributes = await sdp_client.get_attributes(
        service_record_handle, [(0x0000, 0xFFFF)]
    )
    for attribute in attributes:
      match attribute.id:
        case sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID:
          attr["version_number"] = attribute.value.value[0].value[1].value
        case AttributeId.SERVICE_NAME:
          attr["service_name"] = attribute.value.value
        case AttributeId.SERVICE_DESCRIPTION:
          attr["service_description"] = attribute.value.value
        case AttributeId.PROVIDER_NAME:
          attr["provider_name"] = attribute.value.value
        case AttributeId.PARSER_VERSION:
          attr["parser_version"] = attribute.value.value
        case AttributeId.DEVICE_SUBCLASS:
          attr["device_subclass"] = attribute.value.value
        case AttributeId.COUNTRY_CODE:
          attr["country_code"] = attribute.value.value
        case AttributeId.VIRTUAL_CABLE:
          attr["virtual_cable"] = attribute.value.value
        case AttributeId.RECONNECT_INITIATE:
          attr["reconnect_initiate"] = attribute.value.value
        case AttributeId.DESCRIPTOR_LIST:
          attr["report_descriptor_type"] = (
              attribute.value.value[0].value[0].value
          )
          attr["report_map"] = attribute.value.value[0].value[1].value
        case AttributeId.BATTERY_POWER:
          attr["battery_power"] = attribute.value.value
        case AttributeId.REMOTE_WAKE:
          attr["remote_wake"] = attribute.value.value
        case AttributeId.SUPERVISION_TIMEOUT:
          attr["supervision_timeout"] = attribute.value.value
        case AttributeId.NORMALLY_CONNECTABLE:
          attr["normally_connectable"] = attribute.value.value
        case AttributeId.LANGID_BASE_LIST:
          attr["langid_base_language"] = attribute.value.value[0].value[0].value
          attr["langid_base_bluetooth_string_offset"] = (
              attribute.value.value[0].value[1].value
          )
        case AttributeId.BOOT_DEVICE:
          attr["boot_device"] = attribute.value.value
        case AttributeId.SSR_HOST_MAX_LATENCY:
          attr["ssr_host_max_latency"] = attribute.value.value
        case AttributeId.SSR_HOST_MIN_TIMEOUT:
          attr["ssr_host_min_timeout"] = attribute.value.value
        case _:
          pass

    try:
      return SdpInformation(**attr)
    except TypeError:
      _logger.exception("Cannot build SDP information")
      return None


class BaseProtocol(pyee.EventEmitter):
  """Base class for HID Protocol. Device and Host should inherit this class."""

  class Role(enum.IntEnum):
    HOST = 0x00
    DEVICE = 0x01

  class Event(enum.StrEnum):
    """HID Device Events."""

    SUSPEND = "suspend"
    CONTROL_DATA = "control_data"
    INTERRUPT_DATA = "interrupt_data"
    EXIT_SUSPEND = "exit_suspend"
    VIRTUAL_CABLE_UNPLUG = "virtual_cable_unplug"
    HANDSHAKE = "handshake"

  role: Role

  def __init__(
      self,
      control_channel: l2cap.ClassicChannel,
      interrupt_channel: l2cap.ClassicChannel,
  ) -> None:
    super().__init__()
    self.control_channel = control_channel
    self.interrupt_channel = interrupt_channel
    self.control_channel.sink = self._on_control_pdu
    self.interrupt_channel.sink = self._on_interrupt_pdu

  @classmethod
  async def connect(cls: type[Self], connection: device.Connection) -> Self:
    """Connects to the HID peer."""

    control_channel = await connection.create_l2cap_channel(
        spec=l2cap.ClassicChannelSpec(psm=hid.HID_CONTROL_PSM)
    )
    interrupt_channel = await connection.create_l2cap_channel(
        spec=l2cap.ClassicChannelSpec(psm=hid.HID_INTERRUPT_PSM)
    )
    return cls(
        control_channel=control_channel, interrupt_channel=interrupt_channel
    )

  def _on_control_pdu(self, pdu: bytes) -> None:
    raise NotImplementedError()

  def _on_interrupt_pdu(self, pdu: bytes) -> None:
    self.emit(self.Event.INTERRUPT_DATA, pdu)

  def send_data(self, data: bytes) -> None:
    raise NotImplementedError()

  def virtual_cable_unplug(self) -> None:
    msg = hid.VirtualCableUnplug()
    self.control_channel.send_pdu(msg)


class DeviceProtocol(BaseProtocol):
  """HID Device."""

  role = BaseProtocol.Role.DEVICE

  @override
  def _on_control_pdu(self, pdu: bytes) -> None:
    param = pdu[0] & 0x0F
    message_type = pdu[0] >> 4

    match message_type, param:
      case Message.MessageType.GET_REPORT, _:
        self.send_handshake_message(Message.Handshake.SUCCESSFUL)
      case Message.MessageType.SET_REPORT, _:
        self.send_handshake_message(Message.Handshake.SUCCESSFUL)
      case Message.MessageType.GET_PROTOCOL, _:
        self.send_handshake_message(Message.Handshake.SUCCESSFUL)
      case Message.MessageType.SET_PROTOCOL, _:
        self.send_handshake_message(Message.Handshake.SUCCESSFUL)
      case Message.MessageType.DATA, _:
        self.emit(self.Event.CONTROL_DATA, pdu)
      case Message.MessageType.CONTROL, Message.ControlCommand.SUSPEND:
        self.emit(self.Event.SUSPEND)
      case Message.MessageType.CONTROL, Message.ControlCommand.EXIT_SUSPEND:
        self.emit(self.Event.EXIT_SUSPEND)
      case (
          Message.MessageType.CONTROL,
          Message.ControlCommand.VIRTUAL_CABLE_UNPLUG,
      ):
        self.emit(self.Event.VIRTUAL_CABLE_UNPLUG)
      case _:
        self.send_handshake_message(Message.Handshake.ERR_UNSUPPORTED_REQUEST)

  def send_handshake_message(self, result_code: int) -> None:
    msg = hid.SendHandshakeMessage(result_code)
    self.control_channel.send_pdu(msg)

  def send_control_data(self, report_type: int, data: bytes):
    msg = hid.SendControlData(report_type=report_type, data=data)
    self.control_channel.send_pdu(msg)

  @override
  def send_data(self, data: bytes) -> None:
    msg = hid.SendData(data, Message.ReportType.INPUT_REPORT)
    self.interrupt_channel.send_pdu(msg)


class HostProtocol(BaseProtocol):
  """HID Host."""

  def get_report(
      self, report_type: int, report_id: int, buffer_size: int
  ) -> None:
    msg = hid.GetReportMessage(
        report_type=report_type, report_id=report_id, buffer_size=buffer_size
    )
    self.control_channel.send_pdu(bytes(msg))

  def set_report(self, report_type: int, data: bytes) -> None:
    msg = hid.SetReportMessage(report_type=report_type, data=data)
    self.control_channel.send_pdu(bytes(msg))

  def get_protocol(self) -> None:
    msg = hid.GetProtocolMessage()
    self.control_channel.send_pdu(bytes(msg))

  def set_protocol(self, protocol_mode: int) -> None:
    msg = hid.SetProtocolMessage(protocol_mode=protocol_mode)
    self.control_channel.send_pdu(bytes(msg))

  def suspend(self) -> None:
    msg = hid.Suspend()
    self.control_channel.send_pdu(bytes(msg))

  def exit_suspend(self) -> None:
    msg = hid.ExitSuspend()
    self.control_channel.send_pdu(bytes(msg))

  @override
  def _on_control_pdu(self, pdu: bytes) -> None:
    param = pdu[0] & 0x0F
    message_type = pdu[0] >> 4
    match message_type, param:
      case Message.MessageType.HANDSHAKE, _:
        self.emit(self.Event.HANDSHAKE, Message.Handshake(param))
      case Message.MessageType.DATA, _:
        self.emit(self.Event.CONTROL_DATA, pdu)
      case (
          Message.MessageType.CONTROL,
          Message.ControlCommand.VIRTUAL_CABLE_UNPLUG,
      ):
        self.emit(self.Event.VIRTUAL_CABLE_UNPLUG)

  @override
  def send_data(self, data: bytes) -> None:
    msg = hid.SendData(data, Message.ReportType.OUTPUT_REPORT)
    self.interrupt_channel.send_pdu(msg)


_Protocol = TypeVar("_Protocol", bound=BaseProtocol)


class Server(Generic[_Protocol]):
  """HID Server."""

  _pending_control_channels: dict[device.Connection, l2cap.ClassicChannel] = {}

  def __init__(
      self,
      bumble_device: device.Device,
      protocol_factory: type[_Protocol],
  ) -> None:
    self._control_channel_server = bumble_device.create_l2cap_server(
        spec=l2cap.ClassicChannelSpec(psm=hid.HID_CONTROL_PSM),
        handler=self._on_control_channel,
    )
    self._interrupt_channel_server = bumble_device.create_l2cap_server(
        spec=l2cap.ClassicChannelSpec(psm=hid.HID_INTERRUPT_PSM),
        handler=self._on_interrupt_channel,
    )
    self.protocol_factory = protocol_factory
    self._pending_connections = asyncio.Queue()

  def _on_control_channel(self, channel: l2cap.ClassicChannel) -> None:
    self._pending_control_channels[channel.connection] = channel

  def _on_interrupt_channel(self, channel: l2cap.ClassicChannel) -> None:
    if control_channel := self._pending_control_channels.pop(
        channel.connection, None
    ):
      protocol = self.protocol_factory(
          control_channel=control_channel, interrupt_channel=channel
      )
      self._pending_connections.put_nowait(protocol)
    else:
      raise core.InvalidStateError("No pending control channel")

  async def wait_connection(self) -> _Protocol:
    return await self._pending_connections.get()
