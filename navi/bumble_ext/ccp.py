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

"""Bluetooth LE Audio Call Control Profile (CCP) Bumble Implementation."""

import asyncio
from collections.abc import Sequence
import dataclasses
import enum
import struct
from typing import Self, Tuple, Type, TypeAlias

from bumble import core
from bumble import device
from bumble import gatt
from bumble import gatt_client
from bumble import utils

from navi.bumble_ext import gatt_helper


class CharacteristicUuid:
  """UUIDs of CCP/TBS characteristics.

  https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/uuids/characteristic_uuids.yaml
  """

  BEARER_PROVIDER_NAME = core.UUID.from_16_bits(0x2BB3)
  BEARER_UCI = core.UUID.from_16_bits(0x2BB4)
  BEARER_TECHNOLOGY = core.UUID.from_16_bits(0x2BB5)
  BEARER_URI_SCHEMES_SUPPORTED_LIST = core.UUID.from_16_bits(0x2BB6)
  BEARER_SIGNAL_STRENGTH = core.UUID.from_16_bits(0x2BB7)
  BEARER_SIGNAL_STRENGTH_REPORTING_INTERVAL = core.UUID.from_16_bits(0x2BB8)
  BEARER_LIST_CURRENT_CALLS = core.UUID.from_16_bits(0x2BB9)
  CONTENT_CONTROL_ID = core.UUID.from_16_bits(0x2BBA)
  STATUS_FLAGS = core.UUID.from_16_bits(0x2BBB)
  INCOMING_CALL_TARGET_BEARER_URI = core.UUID.from_16_bits(0x2BBC)
  CALL_STATE = core.UUID.from_16_bits(0x2BBD)
  CALL_CONTROL_POINT = core.UUID.from_16_bits(0x2BBE)
  CALL_CONTROL_POINT_OPTIONAL_OPCODES = core.UUID.from_16_bits(0x2BBF)
  TERMINATION_REASON = core.UUID.from_16_bits(0x2BC0)
  INCOMING_CALL = core.UUID.from_16_bits(0x2BC1)
  CALL_FRIENDLY_NAME = core.UUID.from_16_bits(0x2BC2)


class TbsError(core.ProtocolError):

  def __init__(
      self, error_code: int | None, error_name: str = '', details: str = ''
  ):
    super().__init__(error_code, 'TBS', error_name, details)


class BearerTechnology(utils.OpenIntEnum):
  """See TBS spec 1.0, Section 3.3. Bearer Technology."""

  TECH_3G = 0x01
  TECH_4G = 0x02
  TECH_LTE = 0x03
  TECH_WIFI = 0x04
  TECH_5G = 0x05
  TECH_GSM = 0x06
  TECH_CDMA = 0x07
  TECH_2G = 0x08
  TECH_WCDMA = 0x09


class StatusFlag(enum.IntFlag):
  """See TBS spec 1.0, Section 3.9. Status Flags."""

  INBAND_RINGTONE = 0x01
  SILENT_MODE = 0x02

  def __bytes__(self) -> bytes:
    return struct.pack('<H', self.value)

  @classmethod
  def from_bytes(cls: Type[Self], data: bytes) -> Self:
    return cls(struct.unpack('<H', data)[0])


class CallState(utils.OpenIntEnum):
  """See TBS spec 1.0, Section 3.11. Call State."""

  INCOMING = 0x00
  DIALING = 0x01
  ALERTING = 0x02
  ACTIVE = 0x03
  LOCALLY_HELD = 0x04
  REMOTELY_HELD = 0x05
  LOCALLY_AND_REMOTELY_HELD = 0x06


class CallFlag(enum.IntFlag):
  """See TBS spec 1.0, Section 3.11. Call State."""

  IS_OUTGOING = 0x01
  INFO_WITHHELD_BY_SERVER = 0x02
  INFO_WITHHELD_BY_NETWORK = 0x04


class CallControlPointOpcode(utils.OpenIntEnum):
  """See TBS spec 1.0, Section 3.12. Call Control Point Opcode."""

  ACCEPT = 0x00
  TERMINATE = 0x01
  LOCAL_HOLD = 0x02
  LOCAL_RETRIEVE = 0x03
  ORIGINATE = 0x04
  JOIN = 0x05


class ControlPointResultCode(utils.OpenIntEnum):
  """See TBS spec 1.0, Section 3.12.2. Call Control Point Notification."""

  SUCCESS = 0x00
  OPCODE_NOT_SUPPORTED = 0x01
  OPERATION_NOT_POSSIBLE = 0x02
  INVALID_CALL_INDEX = 0x03
  STATE_MISMATCH = 0x04
  LACK_OF_RESOURCES = 0x05
  INVALID_OUTGOING_URI = 0x06


@dataclasses.dataclass(frozen=True)
class CallInfo:
  """See TBS spec 1.0, Section 3.7. Bearer List Current Calls."""

  call_index: int
  call_state: CallState
  call_flags: CallFlag
  call_uri: str

  @classmethod
  def from_bytes(cls: Type[Self], data: bytes) -> Self:
    return cls(
        call_index=data[1],
        call_state=CallState(data[2]),
        call_flags=CallFlag(data[3]),
        call_uri=data[4:].decode(),
    )

  @classmethod
  def parse_list(cls: Type[Self], data: bytes) -> list[Self]:
    calls = []
    offset = 0
    while offset < len(data):
      length = data[offset]
      calls.append(cls.from_bytes(data[offset : offset + length + 1]))
      offset += length + 1
    return calls

  def to_bytes(self) -> bytes:
    call_uri_bytes = self.call_uri.encode('utf-8')
    return (
        bytes([
            len(call_uri_bytes) + 3,
            self.call_index,
            self.call_state,
            self.call_flags,
        ])
        + call_uri_bytes
    )

  def __bytes__(self) -> bytes:
    return self.to_bytes()


_Properties: TypeAlias = gatt.Characteristic.Properties
_CHARACTERISTICS: list[Tuple[str, gatt.UUID, _Properties]] = [
    (
        'bearer_provider_name',
        CharacteristicUuid.BEARER_PROVIDER_NAME,
        _Properties.READ | _Properties.NOTIFY,
    ),
    (
        'bearer_uci',
        CharacteristicUuid.BEARER_UCI,
        _Properties.READ,
    ),
    (
        'bearer_technology',
        CharacteristicUuid.BEARER_TECHNOLOGY,
        _Properties.READ | _Properties.NOTIFY,
    ),
    (
        'bearer_uri_schemes_supported_list',
        CharacteristicUuid.BEARER_URI_SCHEMES_SUPPORTED_LIST,
        _Properties.READ,
    ),
    (
        'bearer_signal_strength',
        CharacteristicUuid.BEARER_SIGNAL_STRENGTH,
        _Properties.READ | _Properties.NOTIFY,
    ),
    (
        'bearer_signal_strength_reporting_interval',
        CharacteristicUuid.BEARER_SIGNAL_STRENGTH_REPORTING_INTERVAL,
        _Properties.READ
        | _Properties.WRITE
        | _Properties.WRITE_WITHOUT_RESPONSE,
    ),
    (
        'bearer_list_current_calls',
        CharacteristicUuid.BEARER_LIST_CURRENT_CALLS,
        _Properties.READ | _Properties.NOTIFY,
    ),
    (
        'content_control_id',
        CharacteristicUuid.CONTENT_CONTROL_ID,
        _Properties.READ,
    ),
    (
        'status_flags',
        CharacteristicUuid.STATUS_FLAGS,
        _Properties.READ | _Properties.NOTIFY,
    ),
    (
        'incoming_call_target_bearer_uri',
        CharacteristicUuid.INCOMING_CALL_TARGET_BEARER_URI,
        _Properties.READ | _Properties.NOTIFY,
    ),
    (
        'call_state',
        CharacteristicUuid.CALL_STATE,
        _Properties.READ | _Properties.NOTIFY,
    ),
    (
        'call_control_point',
        CharacteristicUuid.CALL_CONTROL_POINT,
        _Properties.WRITE
        | _Properties.WRITE_WITHOUT_RESPONSE
        | _Properties.NOTIFY,
    ),
    (
        'call_control_point_optional_opcodes',
        CharacteristicUuid.CALL_CONTROL_POINT_OPTIONAL_OPCODES,
        _Properties.READ,
    ),
    (
        'termination_reason',
        CharacteristicUuid.TERMINATION_REASON,
        _Properties.NOTIFY,
    ),
    (
        'incoming_call',
        CharacteristicUuid.INCOMING_CALL,
        _Properties.READ | _Properties.NOTIFY,
    ),
    (
        'call_friendly_name',
        CharacteristicUuid.CALL_FRIENDLY_NAME,
        _Properties.READ | _Properties.NOTIFY,
    ),
]


class TelephoneBearerService(gatt.TemplateService):
  """Telephone Bearer Service server implementation, only for testing currently."""

  UUID = gatt.GATT_TELEPHONE_BEARER_SERVICE

  call_control_point: gatt.Characteristic

  def __init__(self) -> None:
    characteristics = []
    for field, uuid, properties in _CHARACTERISTICS:
      characteristic = gatt.Characteristic(
          uuid=uuid,
          properties=properties,
          permissions=gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION
          | gatt.Characteristic.Permissions.WRITE_REQUIRES_ENCRYPTION,
          value=bytes(0),
      )
      characteristics.append(characteristic)
      setattr(self, field, characteristic)

    self.call_control_point.value = gatt.CharacteristicValue(
        write=self._on_call_control_point
    )

    super().__init__(characteristics)

  async def _on_call_control_point(
      self, connection: device.Connection | None, data: bytes
  ) -> None:
    opcode = data[0]
    match opcode:
      case (
          CallControlPointOpcode.ACCEPT
          | CallControlPointOpcode.TERMINATE
          | CallControlPointOpcode.LOCAL_HOLD
          | CallControlPointOpcode.LOCAL_RETRIEVE
          | CallControlPointOpcode.JOIN
      ):
        call_index = data[1]
        result_code = ControlPointResultCode.SUCCESS.value
      case CallControlPointOpcode.ORIGINATE:
        call_index = 1
        result_code = ControlPointResultCode.SUCCESS.value
      case _:
        call_index = 0
        result_code = ControlPointResultCode.OPCODE_NOT_SUPPORTED.value

    assert connection
    await connection.device.notify_subscriber(
        connection,
        self.call_control_point,
        bytes([opcode, call_index, result_code]),
    )


class GenericTelephoneBearerService(TelephoneBearerService):
  UUID = gatt.GATT_GENERIC_TELEPHONE_BEARER_SERVICE


class TelephoneBearerServiceProxy(gatt_client.ProfileServiceProxy):
  """Telephone Bearer Service client implementation."""

  SERVICE_CLASS = TelephoneBearerService

  call_control_point: gatt_client.CharacteristicProxy

  bearer_uci: bytes
  bearer_uri_schemes_supported_list: bytes
  content_control_id: bytes
  call_control_point_optional_opcodes: bytes

  bearer_provider_name: gatt_helper.MutableCharacteristicState
  bearer_technology: gatt_helper.MutableCharacteristicState
  bearer_signal_strength: gatt_helper.MutableCharacteristicState | None = None
  bearer_signal_strength_reporting_interval: (
      gatt_helper.MutableCharacteristicState | None
  ) = None
  bearer_list_current_calls: gatt_helper.MutableCharacteristicState
  status_flags: gatt_helper.MutableCharacteristicState
  incoming_call_target_bearer_uri: (
      gatt_helper.MutableCharacteristicState | None
  ) = None
  call_state: gatt_helper.MutableCharacteristicState
  termination_reason: gatt_helper.MutableCharacteristicState
  incoming_call: gatt_helper.MutableCharacteristicState
  call_friendly_name: gatt_helper.MutableCharacteristicState | None = None

  def __init__(self, service_proxy: gatt_client.ServiceProxy) -> None:
    self.service_proxy = service_proxy
    self.lock = asyncio.Lock()
    self._call_control_point_notifications = asyncio.Queue[bytes]()

  async def read_and_subscribe_characteristics(self) -> None:
    for field, uuid, properties, *_ in _CHARACTERISTICS:
      characteristics = self.service_proxy.get_characteristics_by_uuid(uuid)
      if not characteristics:
        if hasattr(self, field):
          continue
        else:
          raise gatt.InvalidServiceError(
              f'Characteristic {uuid} not found in service'
          )
      characteristic = characteristics[0]
      if properties & _Properties.NOTIFY and properties & _Properties.READ:
        state = await gatt_helper.MutableCharacteristicState.create(
            characteristic
        )
        setattr(self, field, state)
      elif properties & _Properties.READ:
        value = await characteristic.read_value()
        setattr(self, field, value)
      elif uuid == CharacteristicUuid.CALL_CONTROL_POINT:
        self.call_control_point = characteristic
        await self.call_control_point.subscribe(
            self._call_control_point_notifications.put_nowait
        )

  async def _write_control_point(
      self, opcode: CallControlPointOpcode, parameters: bytes
  ) -> int:
    async with self.lock:
      await self.call_control_point.write_value(
          bytes([opcode.value]) + parameters,
          with_response=False,
      )
      result_opcode, call_index, result_code = (
          await self._call_control_point_notifications.get()
      )

    if result_opcode != opcode.value:
      raise core.InvalidStateError(
          f'Expected {opcode} notification, but get {result_opcode}'
      )
    if result_code != ControlPointResultCode.SUCCESS.value:
      raise TbsError(
          result_code, details=ControlPointResultCode(result_code).name
      )
    return call_index

  async def accept(self, call_index: int) -> None:
    await self._write_control_point(
        CallControlPointOpcode.ACCEPT, bytes([call_index])
    )

  async def terminate(self, call_index: int) -> None:
    await self._write_control_point(
        CallControlPointOpcode.TERMINATE, bytes([call_index])
    )

  async def local_hold(self, call_index: int) -> None:
    await self._write_control_point(
        CallControlPointOpcode.LOCAL_HOLD, bytes([call_index])
    )

  async def local_retrieve(self, call_index: int) -> None:
    await self._write_control_point(
        CallControlPointOpcode.LOCAL_RETRIEVE, bytes([call_index])
    )

  async def originate(self, uri: str) -> int:
    return await self._write_control_point(
        CallControlPointOpcode.ORIGINATE, uri.encode('utf-8')
    )

  async def join(self, call_indices: Sequence[int]) -> int:
    return await self._write_control_point(
        CallControlPointOpcode.JOIN, bytes(call_indices)
    )


class GenericTelephoneBearerServiceProxy(TelephoneBearerServiceProxy):
  SERVICE_CLASS = GenericTelephoneBearerService
