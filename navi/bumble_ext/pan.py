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

"""Implementation of Personal Area Network (PAN) profile.

See:
https://www.bluetooth.com/specifications/specs/bluetooth-network-encapsulation-protocol-1-0/
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import struct
from typing import Callable, Type, cast

from bumble import core
from bumble import hci
from bumble import l2cap
from bumble import sdp
from bumble import utils
import bumble.device
from typing_extensions import Self

from navi.bumble_ext import bnep


_logger = logging.getLogger(__name__)


_LANGUAGE = 0x656E  # 0x656E uint16 “en” (English)
_ENCODING = 0x6A  # 0x006A uint16 UTF-8 encoding
_PRIMARY_LANGUAGE_BASE_ID = 0x100  # 0x0100 uint16 PrimaryLanguageBaseID


class AttributeId(utils.OpenIntEnum):
  """See Bluetooth Assigned Numbers.

  https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/service_discovery/attribute_ids/pan_profile.yaml.
  """

  IP_SUBNET = 0x0200
  SECURITY_DESCRIPTION = 0x030A
  NET_ACCESS_TYPE = 0x030B
  MAX_NET_ACCESS_RATE = 0x030C
  IPV4_SUBNET = 0x030D
  IPV6_SUBNET = 0x030E


class SecurityDescription(utils.OpenIntEnum):
  """See PAN Profile Specification, Section 8.1.1 NAP service records."""

  NONE = 0x0000
  SERVICE_LEVEL_ENFORCEDSECURITY = 0x0001
  IEEE_802_1X = 0x0002


class NetAccessType(utils.OpenIntEnum):
  """See PAN Profile Specification, Section 8.1.1 NAP service records."""

  PSTN = 0x0000
  ISDN = 0x0001
  DSL = 0x0002
  CABLE_MODEM = 0x0003
  ETHERNET_10MB = 0x0004
  ETHERNET_100MB = 0x0005
  TOKEN_RING_4_MB = 0x0006
  TOKEN_RING_16_MB = 0x0007
  TOKEN_RING_100_MB = 0x0008
  FDDI = 0x0009
  GSM = 0x000A
  CDMA = 0x000B
  GPRS = 0x000C
  CELLULAR_3G = 0x000D
  OTHER = 0xFFFE


@dataclasses.dataclass
class EthernetFrame:
  """A simple L2 Ethernet frame class."""

  protocol_type: int
  payload: bytes
  source_address: hci.Address | None = None
  destination_address: hci.Address | None = None

  def __bytes__(self) -> bytes:
    return (
        struct.pack(
            "!6s6sH",
            bytes(self.source_address) if self.source_address else bytes(6),
            (
                bytes(self.destination_address)
                if self.destination_address
                else bytes(6)
            ),
            self.protocol_type,
        )
        + self.payload
    )

  @classmethod
  def from_bytes(cls: Type[Self], pdu: bytes) -> Self:
    """Creates an Ethernet frame from the given bytes.

    Args:
      pdu: The bytes of the Ethernet frame.

    Returns:
      The Ethernet frame.

    Raises:
      ValueError: If the PDU is too short.
    """
    if len(pdu) < 14:
      raise ValueError("PDU is too short")

    source_address = hci.Address.parse_address_with_type(
        pdu, 0, hci.Address.RANDOM_DEVICE_ADDRESS
    )[1]
    destination_address = hci.Address.parse_address_with_type(
        pdu, 6, hci.Address.RANDOM_DEVICE_ADDRESS
    )[1]
    protocol_type = int.from_bytes(pdu[12:14], "big")
    payload = pdu[14:]
    return cls(
        source_address=source_address,
        destination_address=destination_address,
        protocol_type=protocol_type,
        payload=payload,
    )


def _make_generic_service_record(
    service_record_handle: int,
    service_class_uuid: core.UUID,
    service_name: str,
    service_description: str,
    security_description: SecurityDescription,
) -> list[sdp.ServiceAttribute]:
  return [
      sdp.ServiceAttribute(
          sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
          sdp.DataElement.unsigned_integer_32(service_record_handle),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([sdp.DataElement.uuid(service_class_uuid)]),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID),
                  sdp.DataElement.unsigned_integer_16(bnep.BNEP_PSM),
              ]),
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_BNEP_PROTOCOL_ID),
                  sdp.DataElement.unsigned_integer_16(0x0100),
              ]),
          ]),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_LANGUAGE_BASE_ATTRIBUTE_ID_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([
              sdp.DataElement.unsigned_integer_16(_LANGUAGE),
              sdp.DataElement.unsigned_integer_16(_ENCODING),
              sdp.DataElement.unsigned_integer_16(_PRIMARY_LANGUAGE_BASE_ID),
          ]),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([
              sdp.DataElement.uuid(service_class_uuid),
              sdp.DataElement.unsigned_integer_16(0x0100),
          ]),
      ),
      sdp.ServiceAttribute(
          _PRIMARY_LANGUAGE_BASE_ID + sdp.SDP_SERVICE_NAME_ATTRIBUTE_ID_OFFSET,
          sdp.DataElement.text_string(service_name.encode("utf-8")),
      ),
      sdp.ServiceAttribute(
          _PRIMARY_LANGUAGE_BASE_ID
          + sdp.SDP_SERVICE_DESCRIPTION_ATTRIBUTE_ID_OFFSET,
          sdp.DataElement.text_string(service_description.encode("utf-8")),
      ),
      sdp.ServiceAttribute(
          AttributeId.SECURITY_DESCRIPTION,
          sdp.DataElement.unsigned_integer_16(security_description.value),
      ),
  ]


def make_nap_service_record(
    service_record_handle: int,
    service_name: str = "Network Access Point Service",
    service_description: str = "Personal Ad-hoc which provides access to a network",
    security_description: SecurityDescription = SecurityDescription.NONE,
    net_access_type: NetAccessType = NetAccessType.ETHERNET_10MB,
    max_net_access_rate: int = 1_000_000,
) -> list[sdp.ServiceAttribute]:
  """Makes a NAP service record.

  Args:
    service_record_handle: The service record handle.
    service_name: The service name.
    service_description: The service description.
    security_description: The security description.
    net_access_type: The network access type.
    max_net_access_rate: The maximum network access rate.

  Returns:
    A list of SDP service attributes included in the service record.
  """
  return _make_generic_service_record(
      service_record_handle=service_record_handle,
      service_class_uuid=core.BT_NAP_SERVICE,
      service_name=service_name,
      service_description=service_description,
      security_description=security_description,
  ) + [
      sdp.ServiceAttribute(
          AttributeId.NET_ACCESS_TYPE,
          sdp.DataElement.unsigned_integer_16(net_access_type.value),
      ),
      sdp.ServiceAttribute(
          AttributeId.MAX_NET_ACCESS_RATE,
          sdp.DataElement.unsigned_integer_32(max_net_access_rate),
      ),
  ]


def make_gn_service_record(
    service_record_handle: int,
    service_name: str = "Group Ad-hoc Network Service",
    service_description: str = "Personal Group Ad-hoc Network Service",
    security_description: SecurityDescription = SecurityDescription.NONE,
) -> list[sdp.ServiceAttribute]:
  """Makes a GN service record.

  Args:
    service_record_handle: The service record handle.
    service_name: The service name.
    service_description: The service description.
    security_description: The security description.

  Returns:
    A list of SDP service attributes included in the service record.
  """
  return _make_generic_service_record(
      service_record_handle=service_record_handle,
      service_class_uuid=core.BT_GN_SERVICE,
      service_name=service_name,
      service_description=service_description,
      security_description=security_description,
  )


def make_panu_service_record(
    service_record_handle: int,
    service_name: str = "Personal Ad-hoc User Service",
    service_description: str = "Personal Ad-hoc User Service",
    security_description: SecurityDescription = SecurityDescription.NONE,
) -> list[sdp.ServiceAttribute]:
  """Makes a PANU service record.

  Args:
    service_record_handle: The service record handle.
    service_name: The service name.
    service_description: The service description.
    security_description: The security description.

  Returns:
    A list of SDP service attributes included in the service record.
  """
  return _make_generic_service_record(
      service_record_handle=service_record_handle,
      service_class_uuid=core.BT_PANU_SERVICE,
      service_name=service_name,
      service_description=service_description,
      security_description=security_description,
  )


async def find_supported_services(
    connection: bumble.device.Connection,
) -> list[core.UUID]:
  """Finds the supported services of the remote device."""
  async with sdp.Client(connection) as sdp_client:
    result = await sdp_client.search_attributes(
        uuids=[core.BT_BNEP_PROTOCOL_ID],
        attribute_ids=[sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID],
    )
    supported_services = []
    for attribute_lists in result:
      for attribute in attribute_lists:
        if attribute.id == sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID:
          supported_services.append(attribute.value.value[0].value)
    return supported_services


class Connection(utils.CompositeEventEmitter):
  """Connection between two PAN devices."""

  ethernet_sink: Callable[[EthernetFrame], None] | None = None
  source_service: core.UUID = core.BT_PANU_SERVICE
  destination_service: core.UUID = core.BT_PANU_SERVICE

  _connection_result: asyncio.Future[None] | None = None

  def __init__(self, l2cap_channel: l2cap.ClassicChannel) -> None:
    super().__init__()
    self.l2cap_channel = l2cap_channel
    self.l2cap_channel.sink = self._on_pdu

  @classmethod
  async def connect(
      cls: Type[Self],
      connection: bumble.device.Connection,
      source_service: core.UUID | None = None,
      destination_service: core.UUID | None = None,
  ) -> Self:
    """Connects to a PAN device.

    Args:
      connection: The connection to the PAN device.
      source_service: The source service to use.
      destination_service: The destination service to use.

    Returns:
      The connected PAN connection.
    """
    l2cap_channel = await connection.create_l2cap_channel(
        spec=l2cap.ClassicChannelSpec(psm=bnep.BNEP_PSM)
    )
    pan_connection = cls(l2cap_channel)
    pan_connection.source_service = source_service or core.BT_PANU_SERVICE
    pan_connection.destination_service = (
        destination_service or core.BT_PANU_SERVICE
    )
    pan_connection._connection_result = (
        asyncio.get_running_loop().create_future()
    )

    pan_connection.send_packet(
        bnep.Control(
            control_type=bnep.ControlType.SETUP_CONNECTION_REQUEST_MSG,
            payload=bytes([2])
            + pan_connection.destination_service.to_bytes()[::-1]
            + pan_connection.source_service.to_bytes()[::-1],
        )
    )
    await pan_connection._connection_result
    return pan_connection

  def send_ethernet_frame(
      self,
      packet: EthernetFrame,
      strip_source_address: bool = False,
      strip_destination_address: bool = False,
  ) -> None:
    """Sends an Ethernet frame to the remote device.

    Args:
      packet: The Ethernet frame to send.
      strip_source_address: Whether to strip the source address from the packet.
      strip_destination_address: Whether to strip the destination address from
        the packet.
    """
    _logger.debug(">> %s", packet)
    match strip_source_address, strip_destination_address:
      case True, True:
        packet = bnep.CompressedEthernet(
            networking_protocol_type=packet.protocol_type,
            payload=packet.payload,
        )
      case True, False:
        packet = bnep.CompressedEthernetDestOnly(
            destination_address=packet.destination_address,
            networking_protocol_type=packet.protocol_type,
            payload=packet.payload,
        )
      case False, True:
        packet = bnep.CompressedEthernetSourceOnly(
            source_address=packet.source_address,
            networking_protocol_type=packet.protocol_type,
            payload=packet.payload,
        )
      case False, False:
        packet = bnep.GeneralEthernet(
            source_address=packet.source_address,
            destination_address=packet.destination_address,
            networking_protocol_type=packet.protocol_type,
            payload=packet.payload,
        )
    self.send_packet(packet)

  def send_packet(self, packet: bnep.Packet) -> None:
    """Sends a BNEP packet to the remote device.

    Args:
      packet: The packet to send.
    """
    _logger.debug(">> %s", packet)
    self.l2cap_channel.send_pdu(packet)

  def _on_pdu(self, pdu: bytes) -> None:
    bnep_packet = bnep.Packet.from_bytes(pdu)
    _logger.debug("<< %s", bnep_packet)
    match bnep_packet.packet_type:
      case bnep.PacketType.CONTROL:
        self._on_bnep_control(cast(bnep.Control, bnep_packet))
      case bnep.PacketType.COMPRESSED_ETHERNET:
        self._on_bnep_compressed_ethernet(
            cast(bnep.CompressedEthernet, bnep_packet)
        )
      case bnep.PacketType.COMPRESSED_ETHERNET_SOURCE_ONLY:
        self._on_bnep_compressed_ethernet_source_only(
            cast(bnep.CompressedEthernetSourceOnly, bnep_packet)
        )
      case bnep.PacketType.COMPRESSED_ETHERNET_DEST_ONLY:
        self._on_bnep_compressed_ethernet_dest_only(
            cast(bnep.CompressedEthernetDestOnly, bnep_packet)
        )
      case bnep.PacketType.GENERAL_ETHERNET:
        self._on_bnep_general_ethernet(cast(bnep.GeneralEthernet, bnep_packet))
      case _:
        _logger.error("Unsupported BNEP packet type: %s", bnep_packet)

  def _on_bnep_control(self, packet: bnep.Control) -> None:
    if packet.control_type == bnep.ControlType.SETUP_CONNECTION_REQUEST_MSG:
      uuid_size = packet.payload[0]
      # PAN uses a reversed endianness.
      self.destination_service = core.UUID.from_bytes(
          packet.payload[1 : 1 + uuid_size][::-1]
      )
      self.source_service = core.UUID.from_bytes(
          packet.payload[1 + uuid_size : 1 + uuid_size * 2][::-1]
      )
      self.l2cap_channel.send_pdu(
          bnep.Control(
              control_type=bnep.ControlType.SETUP_CONNECTION_RESPONSE_MSG,
              payload=bnep.SetupConnectionResponseCode.OPERATION_SUCCESSFUL.to_bytes(
                  2, "big", signed=False
              ),
          )
      )
    elif packet.control_type == bnep.ControlType.SETUP_CONNECTION_RESPONSE_MSG:
      if not self._connection_result or self._connection_result.done():
        return
      response_code = int.from_bytes(packet.payload, "big")
      if response_code == bnep.SetupConnectionResponseCode.OPERATION_SUCCESSFUL:
        self._connection_result.set_result(None)
      else:
        self._connection_result.set_exception(bnep.BnepError(response_code))

  def _on_bnep_compressed_ethernet_dest_only(
      self, packet: bnep.CompressedEthernetDestOnly
  ) -> None:
    if self.ethernet_sink:
      self.ethernet_sink(  # pylint: disable=not-callable
          EthernetFrame(
              destination_address=packet.destination_address,
              protocol_type=packet.networking_protocol_type,
              payload=packet.payload,
          )
      )

  def _on_bnep_compressed_ethernet_source_only(
      self, packet: bnep.CompressedEthernetSourceOnly
  ) -> None:
    if self.ethernet_sink:
      self.ethernet_sink(  # pylint: disable=not-callable
          EthernetFrame(
              source_address=packet.source_address,
              protocol_type=packet.networking_protocol_type,
              payload=packet.payload,
          )
      )

  def _on_bnep_general_ethernet(self, packet: bnep.GeneralEthernet) -> None:
    if self.ethernet_sink:
      self.ethernet_sink(  # pylint: disable=not-callable
          EthernetFrame(
              source_address=packet.source_address,
              destination_address=packet.destination_address,
              protocol_type=packet.networking_protocol_type,
              payload=packet.payload,
          )
      )

  def _on_bnep_compressed_ethernet(
      self, packet: bnep.CompressedEthernet
  ) -> None:
    if self.ethernet_sink:
      self.ethernet_sink(  # pylint: disable=not-callable
          EthernetFrame(
              protocol_type=packet.networking_protocol_type,
              payload=packet.payload,
          )
      )


class Server(utils.CompositeEventEmitter):
  """PAN server."""

  connections: list[Connection] = []

  def __init__(self, device: bumble.device.Device) -> None:
    super().__init__()
    self.device = device
    self.device.create_l2cap_server(
        spec=l2cap.ClassicChannelSpec(psm=bnep.BNEP_PSM),
        handler=self._on_connection,
    )

  def _on_connection(self, channel: l2cap.ClassicChannel) -> None:
    connection = Connection(channel)
    self.emit("connection", connection)
