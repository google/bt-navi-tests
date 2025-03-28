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

"""Bluetooth Ranging Profile."""

from __future__ import annotations

import dataclasses
import enum
import struct
from typing import ClassVar

from bumble import core
from bumble import device
from bumble import hci
from bumble import utils
from typing_extensions import Self


# =============================================================================
# UUIDs
# =============================================================================
GATT_RANGING_SERVICE = core.UUID.from_16_bits(0x185B, 'Ranging Service')
GATT_RAS_FEATURES_CHARACTERISTIC = core.UUID.from_16_bits(
    0x2C14, 'RAS Features'
)
GATT_REAL_TIME_RANGING_DATA_CHARACTERISTIC = core.UUID.from_16_bits(
    0x2C15, 'Real-time Ranging Data'
)
GATT_ON_DEMAND_RANGING_DATA_CHARACTERISTIC = core.UUID.from_16_bits(
    0x2C16, 'On-demand Ranging Data'
)
GATT_RAS_CONTROL_POINT_CHARACTERISTIC = core.UUID.from_16_bits(
    0x2C17, 'RAS Control Point'
)
GATT_RANGING_DATA_READY_CHARACTERISTIC = core.UUID.from_16_bits(
    0x2C18, 'Ranging Data Ready'
)
GATT_RANGING_DATA_OVERWRITTEN_CHARACTERISTIC = core.UUID.from_16_bits(
    0x2C19, 'Ranging Data Overwritten'
)


class RasFeatures(enum.IntFlag):
  """Ranging Service - 3.1.1 RAS Features format."""

  REAL_TIME_RANGING_DATA = 0x01
  RETRIEVE_LOST_RANGING_DATA_SEGMENTS = 0x02
  ABORT_OPERATION = 0x04
  FILTER_RANGING_DATA = 0x08


# =============================================================================
# RAS Control Point Operations
# =============================================================================


class RasControlPointOpCode(utils.OpenIntEnum):
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  GET_RANGING_DATA = 0x00
  ACK_RANGING_DATA = 0x01
  RETRIEVE_LOST_RANGING_DATA_SEGMENTS = 0x02
  ABORT_OPERATION = 0x03
  SET_FILTER = 0x04


class RasControlPointResponseOpCode(utils.OpenIntEnum):
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  COMPLETE_RANGING_DATA_RESPONSE = 0x00
  COMPLETE_LOST_RANGING_DATA_RESPONSE = 0x01
  RESPONSE_CODE = 0x02


class RasControlPointResponseCode(utils.OpenIntEnum):
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  # RFU = 0x00

  # Normal response for a successful operation
  SUCCESS = 0x01
  # Normal response if an unsupported Op Code is received
  OP_CODE_NOT_SUPPORTED = 0x02
  # Normal response if Parameter received does not meet the requirements of the
  # service
  INVALID_PARAMETER = 0x03
  # Normal response for a successful write operation where the values written to
  # the RAS Control Point are being persisted.
  SUCCESS_PERSISTED = 0x04
  # Normal response if a request for Abort is unsuccessful
  ABORT_UNSUCCESSFUL = 0x05
  # Normal response if unable to complete a procedure for any reason
  PROCEDURE_NOT_COMPLETED = 0x06
  # Normal response if the Server is still busy with other requests
  SERVER_BUSY = 0x07
  # Normal response if the requested Ranging Counter is not found
  NO_RECORDS_FOUND = 0x08


@dataclasses.dataclass
class RasControlPointOperation:
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  op_code: ClassVar[RasControlPointOpCode]

  def __bytes__(self) -> bytes:
    raise NotImplementedError

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    match data[0]:
      case RasControlPointOpCode.GET_RANGING_DATA:
        return GetRangingDataOperation.from_bytes(data)
      case RasControlPointOpCode.ACK_RANGING_DATA:
        return AckRangingDataOperation.from_bytes(data)
      case RasControlPointOpCode.RETRIEVE_LOST_RANGING_DATA_SEGMENTS:
        return RetrieveLostRangingDataSegmentsOperation.from_bytes(data)
      case RasControlPointOpCode.ABORT_OPERATION:
        return AbortOperationOperation.from_bytes(data)
      case RasControlPointOpCode.SET_FILTER:
        return SetFilterOperation.from_bytes(data)
      case _:
        raise ValueError(f'Unsupported op code: {data[0]}')


@dataclasses.dataclass
class GetRangingDataOperation(RasControlPointOperation):
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  op_code = RasControlPointOpCode.GET_RANGING_DATA
  ranging_counter: int

  def __bytes__(self) -> bytes:
    return struct.pack('<BH', self.op_code, self.ranging_counter)

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    return cls(*struct.unpack_from('<H', data, 1))


@dataclasses.dataclass
class AckRangingDataOperation(RasControlPointOperation):
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  op_code = RasControlPointOpCode.ACK_RANGING_DATA
  ranging_counter: int

  def __bytes__(self) -> bytes:
    return struct.pack('<BH', self.op_code, self.ranging_counter)

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    return cls(*struct.unpack_from('<H', data, 1))


@dataclasses.dataclass
class RetrieveLostRangingDataSegmentsOperation(RasControlPointOperation):
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  op_code = RasControlPointOpCode.RETRIEVE_LOST_RANGING_DATA_SEGMENTS
  ranging_counter: int
  first_segment_index: int
  last_segment_index: int

  def __bytes__(self) -> bytes:
    return struct.pack(
        '<BHBB',
        self.op_code,
        self.ranging_counter,
        self.first_segment_index,
        self.last_segment_index,
    )

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    return cls(*struct.unpack_from('<HBB', data, 1))


@dataclasses.dataclass
class AbortOperationOperation(RasControlPointOperation):
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  op_code = RasControlPointOpCode.ABORT_OPERATION

  def __bytes__(self) -> bytes:
    return bytes([self.op_code])

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    return cls()


@dataclasses.dataclass
class SetFilterOperation(RasControlPointOperation):
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  filter_configuration: int
  op_code = RasControlPointOpCode.SET_FILTER

  def __bytes__(self) -> bytes:
    return struct.pack('<BH', self.op_code, self.filter_configuration)

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    return cls(*struct.unpack_from('<H', data, 1))


# =============================================================================
# RAS Control Point Operation Responses
# =============================================================================


@dataclasses.dataclass
class ControlPointOperationResponse:
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  op_code: ClassVar[RasControlPointResponseOpCode]

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    match data[0]:
      case RasControlPointResponseOpCode.COMPLETE_RANGING_DATA_RESPONSE:
        return CompleteRangingDataResponse.from_bytes(data)
      case RasControlPointResponseOpCode.COMPLETE_LOST_RANGING_DATA_RESPONSE:
        return CompleteLostRangingDataResponse.from_bytes(data)
      case RasControlPointResponseOpCode.RESPONSE_CODE:
        return CodeResponse.from_bytes(data)
      case _:
        raise ValueError(f'Unsupported op code: {data[0]}')

  def __bytes__(self) -> bytes:
    raise NotImplementedError


@dataclasses.dataclass
class CompleteRangingDataResponse(ControlPointOperationResponse):
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  op_code = RasControlPointResponseOpCode.COMPLETE_RANGING_DATA_RESPONSE
  ranging_counter: int

  def __bytes__(self) -> bytes:
    return struct.pack('<BH', self.op_code, self.ranging_counter)

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    return cls(*struct.unpack_from('<H', data, 1))


@dataclasses.dataclass
class CompleteLostRangingDataResponse(ControlPointOperationResponse):
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  op_code = RasControlPointResponseOpCode.COMPLETE_LOST_RANGING_DATA_RESPONSE
  ranging_counter: int
  first_segment_index: int
  last_segment_index: int

  def __bytes__(self) -> bytes:
    return struct.pack(
        '<BHBB',
        self.op_code,
        self.ranging_counter,
        self.first_segment_index,
        self.last_segment_index,
    )

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    return cls(*struct.unpack_from('<HBB', data, 1))


@dataclasses.dataclass
class CodeResponse(ControlPointOperationResponse):
  """Ranging Service - 3.3.1 RAS Control Point Op Codes and Parameters requirements."""

  op_code = RasControlPointResponseOpCode.RESPONSE_CODE
  value: RasControlPointResponseCode

  def __bytes__(self) -> bytes:
    return bytes([self.op_code, self.value])

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    return cls(*struct.unpack_from('<B', data, 1))


@dataclasses.dataclass
class SegmentationHeader:
  """Ranging Service - 3.2.1.1 Segmentation Header."""

  is_first: bool
  is_last: bool
  segment_index: int

  def __bytes__(self) -> bytes:
    return bytes([(
        ((self.segment_index & 0x3F) << 2)
        | (0x01 if self.is_first else 0x00)
        | (0x02 if self.is_last else 0x00)
    )])

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    """Parse Segmentation Header from bytes."""
    return cls(
        is_first=bool(data[0] & 0x01),
        is_last=bool(data[0] & 0x02),
        segment_index=data[0] >> 2,
    )


@dataclasses.dataclass
class RangingHeader:
  """Ranging Service - Table 3.7: Ranging Header structure."""

  configuration_id: int
  selected_tx_power: int
  antenna_paths_mask: int
  ranging_counter: int

  def __bytes__(self) -> bytes:
    return struct.pack(
        '<HbB',
        self.configuration_id << 12 | (self.ranging_counter & 0xFFF),
        self.selected_tx_power,
        self.antenna_paths_mask,
    )

  @classmethod
  def from_bytes(cls: type[Self], data: bytes) -> Self:
    """Parse Ranging Header from bytes."""
    (
        ranging_counter_and_configuration_id,
        selected_tx_power,
        antenna_paths_mask,
    ) = struct.unpack_from('<HbB', data)
    return cls(
        ranging_counter=ranging_counter_and_configuration_id & 0x3F,
        configuration_id=ranging_counter_and_configuration_id >> 12,
        selected_tx_power=selected_tx_power,
        antenna_paths_mask=antenna_paths_mask,
    )


# =============================================================================
# Ranging Data
# =============================================================================


@dataclasses.dataclass
class Step:
  """Ranging Service - Table 3.8: Subevent Header and Data structure."""

  mode: int
  data: bytes

  def __bytes__(self) -> bytes:
    return bytes([self.mode]) + self.data

  @classmethod
  def parse_from(
      cls: type[Self],
      data: bytes,
      config: device.ChannelSoundingConfig,
      num_antenna_paths: int,
      offset: int = 0,
  ) -> tuple[int, Self]:
    """Parse Step from bytes."""
    mode = data[offset]
    contain_sounding_sequence = config.rtt_type in (
        hci.RttType.SOUNDING_SEQUENCE_32_BIT,
        hci.RttType.SOUNDING_SEQUENCE_96_BIT,
    )
    is_initiator = config.role == hci.CsRole.INITIATOR

    match mode:
      case 0:
        length = 5 if is_initiator else 3
      case 1:
        length = 12 if contain_sounding_sequence else 6
      case 2:
        length = (num_antenna_paths + 1) * 4 + 1
      case 3:
        length = (num_antenna_paths + 1) * 4 + (
            13 if contain_sounding_sequence else 7
        )
      case _:
        raise core.InvalidPacketError(f'Unknown mode 0x{mode:02X}')
    return (offset + length + 1), cls(
        mode=mode, data=data[offset + 1 : offset + 1 + length]
    )


@dataclasses.dataclass
class Subevent:
  """Ranging Service - Table 3.8: Subevent Header and Data structure."""

  start_acl_connection_event: int
  frequency_compensation: int
  ranging_done_status: int
  subevent_done_status: int
  ranging_abort_reason: int
  subevent_abort_reason: int
  reference_power_level: int
  steps: list[Step] = dataclasses.field(default_factory=list)

  def __bytes__(self) -> bytes:
    return struct.pack(
        '<HHBBbB',
        self.start_acl_connection_event,
        self.frequency_compensation,
        self.ranging_done_status | self.subevent_done_status << 4,
        self.ranging_abort_reason | self.subevent_abort_reason << 4,
        self.reference_power_level,
        len(self.steps),
    ) + b''.join(map(bytes, self.steps))

  @classmethod
  def parse_from(
      cls: type[Self],
      data: bytes,
      config: device.ChannelSoundingConfig,
      num_antenna_paths: int,
      offset: int = 0,
  ) -> tuple[int, Self]:
    """Parse Subevent from bytes."""
    (
        start_acl_connection_event,
        frequency_compensation,
        ranging_done_status_and_subevent_done_status,
        ranging_abort_reason_and_subevent_abort_reason,
        reference_power_level,
        num_reported_steps,
    ) = struct.unpack_from('<HHBBbB', data, offset)
    offset += 8
    steps: list[Step] = []
    for _ in range(num_reported_steps):
      offset, step = Step.parse_from(
          data=data,
          config=config,
          num_antenna_paths=num_antenna_paths,
          offset=offset,
      )
      steps.append(step)
    return offset, cls(
        start_acl_connection_event=start_acl_connection_event,
        frequency_compensation=frequency_compensation,
        ranging_done_status=ranging_done_status_and_subevent_done_status & 0x0F,
        subevent_done_status=ranging_done_status_and_subevent_done_status >> 4,
        ranging_abort_reason=ranging_abort_reason_and_subevent_abort_reason
        & 0x0F,
        subevent_abort_reason=ranging_abort_reason_and_subevent_abort_reason
        >> 4,
        reference_power_level=reference_power_level,
        steps=steps,
    )


@dataclasses.dataclass
class RangingData:
  """Ranging Service - 3.2.1 Ranging Data format."""

  ranging_header: RangingHeader
  subevents: list[Subevent] = dataclasses.field(default_factory=list)

  def __bytes__(self) -> bytes:
    return bytes(self.ranging_header) + b''.join(map(bytes, self.subevents))

  @classmethod
  def from_bytes(
      cls: type[Self],
      data: bytes,
      config: device.ChannelSoundingConfig,
  ) -> Self:
    """Parse Ranging Data from bytes."""
    ranging_header = RangingHeader.from_bytes(data)
    num_antenna_paths = 0
    antenna_path_mask = ranging_header.antenna_paths_mask
    while antenna_path_mask > 0:
      if antenna_path_mask & 0x01:
        num_antenna_paths += 1
      antenna_path_mask >>= 1

    subevents: list[Subevent] = []
    offset = 4
    while offset < len(data):
      offset, subevent = Subevent.parse_from(
          data=data,
          config=config,
          num_antenna_paths=num_antenna_paths,
          offset=offset,
      )
      subevents.append(subevent)
    return cls(ranging_header=ranging_header, subevents=subevents)
