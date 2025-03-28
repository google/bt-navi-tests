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

"""Implementation of Bluetooth Network Encapsulation Protocol (BNEP).

See:
https://www.bluetooth.com/specifications/specs/bluetooth-network-encapsulation-protocol-1-0/
"""

from __future__ import annotations

import dataclasses
import struct
from typing import Final, Type

from bumble import core
from bumble import hci
from bumble import utils
from typing_extensions import Self


class BnepError(core.ProtocolError):

  def __init__(self, error_code: int):
    super().__init__(error_code=error_code, error_namespace='BNEP')


def _parse_random_address(data: bytes, offset: int) -> hci.Address:
  return hci.Address.parse_address_with_type(
      data, offset, hci.Address.RANDOM_DEVICE_ADDRESS
  )[1]


BNEP_PSM = 0x000F


class PacketType(utils.OpenIntEnum):
  """See BNEP Specification, 2.4.1. BNEP Type Values."""

  GENERAL_ETHERNET = 0x00
  CONTROL = 0x01
  COMPRESSED_ETHERNET = 0x02
  COMPRESSED_ETHERNET_SOURCE_ONLY = 0x03
  COMPRESSED_ETHERNET_DEST_ONLY = 0x04


class ControlType(utils.OpenIntEnum):
  """See BNEP Specification, 2.6.1. BNEP Control Type Values."""

  CONTROL_COMMAND_NOT_UNDERSTOOD = 0x00
  SETUP_CONNECTION_REQUEST_MSG = 0x01
  SETUP_CONNECTION_RESPONSE_MSG = 0x02
  FILTER_NET_TYPE_SET_MSG = 0x03
  FILTER_NET_TYPE_RESPONSE_MSG = 0x04
  FILTER_MULTI_ADDR_SET_MSG = 0x05
  FILTER_MULTI_ADDR_RESPONSE_MSG = 0x06


class SetupConnectionResponseCode(utils.OpenIntEnum):
  """See BNEP Specification, 2.6.3.2.1. Response Messages."""

  OPERATION_SUCCESSFUL = 0x0000
  OPERATION_FAILED_INVALID_DESTINATION_SERVICE_UUID = 0x0001
  OPERATION_FAILED_INVALID_SOURCE_SERVICE_UUID = 0x0002
  OPERATION_FAILED_INVALID_SERVICE_UUID_SIZE = 0x0003
  OPERATION_FAILED_CONNECTION_NOT_ALLOWED = 0x0004


@dataclasses.dataclass(frozen=True, kw_only=True)
class Packet:
  """See BNEP Specification, 2.4. BNEP Header Formats."""

  packet_type: PacketType
  extension_flag: int = 0
  payload: bytes = b''

  @classmethod
  def from_bytes(cls: Type[Self], pdu: bytes) -> Packet:
    """Creates a packet from the given bytes.

    Args:
      pdu: The bytes of the packet.

    Returns:
      A packet of the appropriate type.
    """
    packet_type = pdu[0] & 0x7F
    match packet_type:
      case PacketType.GENERAL_ETHERNET:
        return GeneralEthernet.from_bytes(pdu)
      case PacketType.CONTROL:
        return Control.from_bytes(pdu)
      case PacketType.COMPRESSED_ETHERNET:
        return CompressedEthernet.from_bytes(pdu)
      case PacketType.COMPRESSED_ETHERNET_SOURCE_ONLY:
        return CompressedEthernetSourceOnly.from_bytes(pdu)
      case PacketType.COMPRESSED_ETHERNET_DEST_ONLY:
        return CompressedEthernetDestOnly.from_bytes(pdu)
      case _:
        raise core.InvalidPacketError(
            f'Invalid BNEP packet type: {packet_type}'
        )

  def __bytes__(self) -> bytes:
    raise NotImplementedError('Base Packet cannot be serialized.')


@dataclasses.dataclass(frozen=True, kw_only=True)
class GeneralEthernet(Packet):
  """See BNEP Specification, 2.5 BNEP_GENERAL_ETHERNET Packet Type Header Format."""

  destination_address: hci.Address
  source_address: hci.Address
  networking_protocol_type: int

  packet_type: Final[PacketType] = PacketType.GENERAL_ETHERNET

  @classmethod
  def from_bytes(cls: Type[Self], pdu: bytes) -> Self:
    return cls(
        extension_flag=pdu[0] >> 7,
        destination_address=_parse_random_address(pdu, 1),
        source_address=_parse_random_address(pdu, 7),
        networking_protocol_type=struct.unpack_from('>H', pdu, 13)[0],
        payload=pdu[15:],
    )

  def __bytes__(self) -> bytes:
    return (
        struct.pack(
            '>B6s6sH',
            self.packet_type.value | self.extension_flag << 7,
            self.destination_address.address_bytes,
            self.source_address.address_bytes,
            self.networking_protocol_type,
        )
        + self.payload
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class Control(Packet):
  """See BNEP Specification, 2.6 BNEP_CONTROL Packet Type Header Format."""

  control_type: int

  packet_type: Final[PacketType] = PacketType.CONTROL

  @classmethod
  def from_bytes(cls: Type[Self], pdu: bytes) -> Self:
    return cls(
        extension_flag=pdu[0] >> 7,
        control_type=pdu[1],
        payload=pdu[2:],
    )

  def __bytes__(self) -> bytes:
    return (
        bytes([
            self.packet_type.value | self.extension_flag << 7,
            self.control_type,
        ])
        + self.payload
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class CompressedEthernet(Packet):
  """See BNEP Specification, 2.7 BNEP_COMPRESSED_ETHERNET Packet Type Header Format."""

  networking_protocol_type: int

  packet_type: Final[PacketType] = PacketType.COMPRESSED_ETHERNET

  @classmethod
  def from_bytes(cls: Type[Self], pdu: bytes) -> Self:
    return cls(
        extension_flag=pdu[0] >> 7,
        networking_protocol_type=struct.unpack_from('>H', pdu, 1)[0],
        payload=pdu[3:],
    )

  def __bytes__(self) -> bytes:
    return (
        struct.pack(
            '>BH',
            self.packet_type.value | self.extension_flag << 7,
            self.networking_protocol_type,
        )
        + self.payload
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class CompressedEthernetSourceOnly(Packet):
  """See BNEP Specification, 2.8 BNEP_COMPRESSED_ETHERNET_SOURCE_ONLY Packet Type Header Format."""

  source_address: hci.Address
  networking_protocol_type: int

  packet_type: Final[PacketType] = PacketType.COMPRESSED_ETHERNET_SOURCE_ONLY

  @classmethod
  def from_bytes(cls: Type[Self], pdu: bytes) -> Self:
    return cls(
        extension_flag=pdu[0] >> 7,
        source_address=_parse_random_address(pdu, 1),
        networking_protocol_type=struct.unpack_from('>H', pdu, 7)[0],
        payload=pdu[9:],
    )

  def __bytes__(self) -> bytes:
    return (
        struct.pack(
            '>B6sH',
            self.packet_type.value | self.extension_flag << 7,
            self.source_address.address_bytes,
            self.networking_protocol_type,
        )
        + self.payload
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class CompressedEthernetDestOnly(Packet):
  """See BNEP Specification, 2.9 BNEP_COMPRESSED_ETHERNET_DEST_ONLY Packet Type Header Format."""

  destination_address: hci.Address
  networking_protocol_type: int

  packet_type: Final[PacketType] = PacketType.COMPRESSED_ETHERNET_DEST_ONLY

  @classmethod
  def from_bytes(cls: Type[Self], pdu: bytes) -> Self:
    return cls(
        extension_flag=pdu[0] >> 7,
        destination_address=_parse_random_address(pdu, 1),
        networking_protocol_type=struct.unpack_from('>H', pdu, 7)[0],
        payload=pdu[9:],
    )

  def __bytes__(self) -> bytes:
    return (
        struct.pack(
            '>B6sH',
            self.packet_type.value | self.extension_flag << 7,
            self.destination_address.address_bytes,
            self.networking_protocol_type,
        )
        + self.payload
    )
