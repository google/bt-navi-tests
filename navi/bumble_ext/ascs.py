#  Copyright 2026 Google LLC
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

"""LE Audio - Audio Stream Control Service."""

from __future__ import annotations

from collections.abc import Sequence
import logging
import struct
from typing import TypeAlias

from bumble import colors
from bumble import data_types
from bumble import device
from bumble import gatt
from bumble import gatt_adapters
from bumble import gatt_client
from bumble import hci
from bumble.profiles import ascs
from bumble.profiles import bap
from bumble.profiles import le_audio

logger = logging.getLogger(__name__)

AudioRole = ascs.AudioRole
AseResponseCode = ascs.AseResponseCode
AseReasonCode = ascs.AseReasonCode


def make_bap_announcement(
    announcement_type: bap.AnnouncementType,
    available_audio_contexts: bap.ContextType = bap.ContextType(0xFFFF),
    metadata: bytes = b'',
) -> data_types.ServiceData16BitUUID:
  """Make BAP announcement service data."""
  return data_types.ServiceData16BitUUID(
      gatt.GATT_AUDIO_STREAM_CONTROL_SERVICE,
      struct.pack(
          '<BIB',
          announcement_type,
          available_audio_contexts,
          len(metadata),
      )
      + metadata,
  )


def make_cap_announcement(
    announcement_type: bap.AnnouncementType,
) -> data_types.ServiceData16BitUUID:
  """Make CAP announcement service data."""
  return data_types.ServiceData16BitUUID(
      gatt.GATT_COMMON_AUDIO_SERVICE, bytes([announcement_type])
  )


class AudioStreamEndpointCharacteristic(gatt.Characteristic):
  """Audio Stream Endpoint Characteristic."""

  State: TypeAlias = ascs.AseStateMachine.State  # pylint: disable=invalid-name

  EVENT_STATE_CHANGE = 'state_change'

  cis_link: device.CisLink | None = None

  # Additional parameters in CODEC_CONFIGURED State
  preferred_framing = 0  # Unframed PDU supported
  preferred_phy = 0
  preferred_retransmission_number = 13
  preferred_max_transport_latency = 100
  supported_presentation_delay_min = 0
  supported_presentation_delay_max = 0
  preferred_presentation_delay_min = 0
  preferred_presentation_delay_max = 0
  codec_id = hci.CodingFormat(hci.CodecID.LC3)
  codec_specific_configuration: bap.CodecSpecificConfiguration | bytes = b''

  # Additional parameters in QOS_CONFIGURED State
  cig_id = 0
  cis_id = 0
  sdu_interval = 0
  framing = 0
  phy = 0
  max_sdu = 0
  retransmission_number = 0
  max_transport_latency = 0
  presentation_delay = 0

  # Additional parameters in ENABLING, STREAMING, DISABLING State
  metadata: le_audio.Metadata

  def __init__(
      self,
      role: ascs.AudioRole,
      ase_id: int,
      service: AudioStreamControlService,
  ) -> None:
    self.service = service
    self.ase_id = ase_id
    self._state = AudioStreamEndpointCharacteristic.State.IDLE
    self.role = role
    self.metadata = le_audio.Metadata()

    uuid = (
        gatt.GATT_SINK_ASE_CHARACTERISTIC
        if role == ascs.AudioRole.SINK
        else gatt.GATT_SOURCE_ASE_CHARACTERISTIC
    )
    super().__init__(
        uuid=uuid,
        properties=gatt.Characteristic.Properties.READ
        | gatt.Characteristic.Properties.NOTIFY,
        permissions=gatt.Characteristic.Permissions.READABLE,
        value=gatt.CharacteristicValue(read=self.on_read),
    )

    self.service.device.on(
        self.service.device.EVENT_CIS_REQUEST, self.on_cis_request
    )
    self.service.device.on(
        self.service.device.EVENT_CIS_ESTABLISHMENT, self.on_cis_establishment
    )

  async def on_cis_request(self, cis_link: device.CisLink) -> None:
    if (
        cis_link.cig_id == self.cig_id
        and cis_link.cis_id == self.cis_id
        and self.state == self.State.ENABLING
    ):
      await self.service.device.accept_cis_request(cis_link)

  async def on_cis_establishment(self, cis_link: device.CisLink) -> None:
    if (
        cis_link.cig_id == self.cig_id
        and cis_link.cis_id == self.cis_id
        and self.state == self.State.ENABLING
    ):
      cis_link.on(cis_link.EVENT_DISCONNECTION, self.on_cis_disconnection)
      self.cis_link = cis_link

      await cis_link.setup_data_path(
          direction=device.CisLink.Direction(self.role.value)
      )
      if self.role == ascs.AudioRole.SINK:
        self.state = self.State.STREAMING
        await self.service.device.notify_subscribers(self, self.data_value)

  def on_cis_disconnection(self, reason: int) -> None:
    del reason  # Unused.
    self.cis_link = None

  async def on_config_codec(
      self,
      target_latency: int,
      target_phy: int,
      codec_id: hci.CodingFormat,
      codec_specific_configuration: bytes,
  ) -> tuple[ascs.AseResponseCode, ascs.AseReasonCode]:
    if self.state not in (
        self.State.IDLE,
        self.State.CODEC_CONFIGURED,
        self.State.QOS_CONFIGURED,
    ):
      return (
          ascs.AseResponseCode.INVALID_ASE_STATE_MACHINE_TRANSITION,
          ascs.AseReasonCode.NONE,
      )

    self.max_transport_latency = target_latency
    self.phy = target_phy
    self.codec_id = codec_id
    if codec_id.codec_id == hci.CodecID.VENDOR_SPECIFIC:
      self.codec_specific_configuration = codec_specific_configuration
    else:
      self.codec_specific_configuration = (
          bap.CodecSpecificConfiguration.from_bytes(
              codec_specific_configuration
          )
      )

    self.state = self.State.CODEC_CONFIGURED

    return (ascs.AseResponseCode.SUCCESS, ascs.AseReasonCode.NONE)

  async def on_config_qos(
      self,
      cig_id: int,
      cis_id: int,
      sdu_interval: int,
      framing: int,
      phy: int,
      max_sdu: int,
      retransmission_number: int,
      max_transport_latency: int,
      presentation_delay: int,
  ) -> tuple[ascs.AseResponseCode, ascs.AseReasonCode]:
    if self.state not in (
        AudioStreamEndpointCharacteristic.State.CODEC_CONFIGURED,
        AudioStreamEndpointCharacteristic.State.QOS_CONFIGURED,
    ):
      return (
          ascs.AseResponseCode.INVALID_ASE_STATE_MACHINE_TRANSITION,
          ascs.AseReasonCode.NONE,
      )

    self.cig_id = cig_id
    self.cis_id = cis_id
    self.sdu_interval = sdu_interval
    self.framing = framing
    self.phy = phy
    self.max_sdu = max_sdu
    self.retransmission_number = retransmission_number
    self.max_transport_latency = max_transport_latency
    self.presentation_delay = presentation_delay

    self.state = self.State.QOS_CONFIGURED

    return (ascs.AseResponseCode.SUCCESS, ascs.AseReasonCode.NONE)

  async def on_enable(
      self, metadata: bytes
  ) -> tuple[ascs.AseResponseCode, ascs.AseReasonCode]:
    if self.state != AudioStreamEndpointCharacteristic.State.QOS_CONFIGURED:
      return (
          ascs.AseResponseCode.INVALID_ASE_STATE_MACHINE_TRANSITION,
          ascs.AseReasonCode.NONE,
      )

    self.metadata = le_audio.Metadata.from_bytes(metadata)
    self.state = self.State.ENABLING
    # CIS could be established before enable.
    if cis_link := next(
        (
            cis_link
            for cis_link in self.service.device.cis_links.values()
            if cis_link.cig_id == self.cig_id and cis_link.cis_id == self.cis_id
        ),
        None,
    ):
      # Notify state change to ENABLING before transferring to STREAMING.
      await self.service.device.notify_subscribers(self, self.data_value)
      await self.on_cis_establishment(cis_link)

    return (ascs.AseResponseCode.SUCCESS, ascs.AseReasonCode.NONE)

  async def on_receiver_start_ready(
      self,
  ) -> tuple[ascs.AseResponseCode, ascs.AseReasonCode]:
    if self.state != AudioStreamEndpointCharacteristic.State.ENABLING:
      return (
          ascs.AseResponseCode.INVALID_ASE_STATE_MACHINE_TRANSITION,
          ascs.AseReasonCode.NONE,
      )
    self.state = self.State.STREAMING
    return (ascs.AseResponseCode.SUCCESS, ascs.AseReasonCode.NONE)

  async def on_disable(self) -> tuple[ascs.AseResponseCode, ascs.AseReasonCode]:
    if self.state not in (
        AudioStreamEndpointCharacteristic.State.ENABLING,
        AudioStreamEndpointCharacteristic.State.STREAMING,
    ):
      return (
          ascs.AseResponseCode.INVALID_ASE_STATE_MACHINE_TRANSITION,
          ascs.AseReasonCode.NONE,
      )
    if self.role == ascs.AudioRole.SINK:
      self.state = self.State.QOS_CONFIGURED
    else:
      self.state = self.State.DISABLING
    return (ascs.AseResponseCode.SUCCESS, ascs.AseReasonCode.NONE)

  async def on_receiver_stop_ready(
      self,
  ) -> tuple[ascs.AseResponseCode, ascs.AseReasonCode]:
    if (
        self.role != ascs.AudioRole.SOURCE
        or self.state != AudioStreamEndpointCharacteristic.State.DISABLING
    ):
      return (
          ascs.AseResponseCode.INVALID_ASE_STATE_MACHINE_TRANSITION,
          ascs.AseReasonCode.NONE,
      )
    self.state = self.State.QOS_CONFIGURED
    return (ascs.AseResponseCode.SUCCESS, ascs.AseReasonCode.NONE)

  async def on_update_metadata(
      self, metadata: bytes
  ) -> tuple[ascs.AseResponseCode, ascs.AseReasonCode]:
    if self.state not in (
        AudioStreamEndpointCharacteristic.State.ENABLING,
        AudioStreamEndpointCharacteristic.State.STREAMING,
    ):
      return (
          ascs.AseResponseCode.INVALID_ASE_STATE_MACHINE_TRANSITION,
          ascs.AseReasonCode.NONE,
      )
    self.metadata = le_audio.Metadata.from_bytes(metadata)
    return (ascs.AseResponseCode.SUCCESS, ascs.AseReasonCode.NONE)

  async def on_release(self) -> tuple[ascs.AseResponseCode, ascs.AseReasonCode]:
    if self.state == AudioStreamEndpointCharacteristic.State.IDLE:
      return (
          ascs.AseResponseCode.INVALID_ASE_STATE_MACHINE_TRANSITION,
          ascs.AseReasonCode.NONE,
      )
    self.state = self.State.RELEASING
    # Notify state change to RELEASING before transferring to IDLE.
    await self.service.device.notify_subscribers(self, self.data_value)
    self.state = self.State.IDLE
    return (ascs.AseResponseCode.SUCCESS, ascs.AseReasonCode.NONE)

  @property
  def state(self) -> State:
    return self._state

  @state.setter
  def state(self, new_state: State) -> None:
    logger.debug(
        '%s state change -> %s', self, colors.color(new_state.name, 'cyan')
    )
    self._state = new_state
    self.emit(self.EVENT_STATE_CHANGE)

  @property
  def data_value(self) -> bytes:
    """Returns ascs.ASE_ID, ascs.ASE_STATE, and ASE Additional Parameters."""

    if self.state == self.State.CODEC_CONFIGURED:
      codec_specific_configuration_bytes = bytes(
          self.codec_specific_configuration
      )
      additional_parameters = (
          struct.pack(
              '<BBBH',
              self.preferred_framing,
              self.preferred_phy,
              self.preferred_retransmission_number,
              self.preferred_max_transport_latency,
          )
          + self.supported_presentation_delay_min.to_bytes(3, 'little')
          + self.supported_presentation_delay_max.to_bytes(3, 'little')
          + self.preferred_presentation_delay_min.to_bytes(3, 'little')
          + self.preferred_presentation_delay_max.to_bytes(3, 'little')
          + bytes(self.codec_id)
          + bytes([len(codec_specific_configuration_bytes)])
          + codec_specific_configuration_bytes
      )
    elif self.state == self.State.QOS_CONFIGURED:
      additional_parameters = (
          bytes([self.cig_id, self.cis_id])
          + self.sdu_interval.to_bytes(3, 'little')
          + struct.pack(
              '<BBHBH',
              self.framing,
              self.phy,
              self.max_sdu,
              self.retransmission_number,
              self.max_transport_latency,
          )
          + self.presentation_delay.to_bytes(3, 'little')
      )
    elif self.state in (
        self.State.ENABLING,
        self.State.STREAMING,
        self.State.DISABLING,
    ):
      metadata_bytes = bytes(self.metadata)
      additional_parameters = (
          bytes([self.cig_id, self.cis_id, len(metadata_bytes)])
          + metadata_bytes
      )
    else:
      additional_parameters = b''

    return bytes([self.ase_id, self.state]) + additional_parameters

  @data_value.setter
  def data_value(self, new_value: bytes) -> None:
    # Readonly. Do nothing in the setter, but required by the interface.
    del new_value  # Unused.

  def on_read(self, connection: device.Connection) -> bytes:
    del connection  # Unused.
    return self.data_value

  def __str__(self) -> str:
    return (
        f'AseStateMachine(id={self.ase_id}, role={self.role.name} '
        f'state={self._state.name})'
    )


# -----------------------------------------------------------------------------
class AudioStreamControlService(gatt.TemplateService):
  """Audio Stream Control Service."""

  UUID = gatt.GATT_AUDIO_STREAM_CONTROL_SERVICE

  ase_state_machines: dict[int, AudioStreamEndpointCharacteristic]
  ase_control_point: gatt_adapters.SerializableCharacteristicAdapter[  # pyrefly: ignore[bad-specialization]
      ascs.ASE_Operation
  ]
  _active_client: device.Connection | None = None

  def __init__(
      self,
      device: device.Device,  # pylint: disable=redefined-outer-name
      source_ase_id: Sequence[int] = (),
      sink_ase_id: Sequence[int] = (),
  ) -> None:
    self.device = device
    self.ase_state_machines = {
        **{
            id: AudioStreamEndpointCharacteristic(
                role=ascs.AudioRole.SINK, ase_id=id, service=self
            )
            for id in sink_ase_id
        },
        **{
            id: AudioStreamEndpointCharacteristic(
                role=ascs.AudioRole.SOURCE, ase_id=id, service=self
            )
            for id in source_ase_id
        },
    }  # ASE state machines, by ASE ID

    self.ase_control_point = gatt_adapters.SerializableCharacteristicAdapter[  # pyrefly: ignore[bad-specialization]
        ascs.ASE_Operation
    ](
        gatt.Characteristic(
            uuid=gatt.GATT_ASE_CONTROL_POINT_CHARACTERISTIC,
            properties=gatt.Characteristic.Properties.WRITE
            | gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE
            | gatt.Characteristic.Properties.NOTIFY,
            permissions=gatt.Characteristic.Permissions.WRITEABLE,
            value=gatt.CharacteristicValue(
                write=self.on_write_ase_control_point
            ),
        ),
        ascs.ASE_Operation,
    )

    super().__init__(
        [self.ase_control_point, *self.ase_state_machines.values()]
    )

  async def on_operation(
      self, opcode: ascs.ASE_Operation.Opcode, ase_id: int, args
  ) -> tuple[int, ascs.AseResponseCode, ascs.AseReasonCode]:
    if ase := self.ase_state_machines.get(ase_id):
      match opcode:
        case ascs.ASE_Operation.Opcode.CONFIG_CODEC:
          result = await ase.on_config_codec(*args)
        case ascs.ASE_Operation.Opcode.CONFIG_QOS:
          result = await ase.on_config_qos(*args)
        case ascs.ASE_Operation.Opcode.ENABLE:
          result = await ase.on_enable(*args)
        case ascs.ASE_Operation.Opcode.UPDATE_METADATA:
          result = await ase.on_update_metadata(*args)
        case ascs.ASE_Operation.Opcode.RELEASE:
          result = await ase.on_release(*args)
        case ascs.ASE_Operation.Opcode.RECEIVER_START_READY:
          result = await ase.on_receiver_start_ready(*args)
        case ascs.ASE_Operation.Opcode.RECEIVER_STOP_READY:
          result = await ase.on_receiver_stop_ready(*args)
        case ascs.ASE_Operation.Opcode.DISABLE:
          result = await ase.on_disable(*args)
        case _:
          result = (
              ascs.AseResponseCode.UNSUPPORTED_OPCODE,
              ascs.AseReasonCode.NONE,
          )
      return (ase_id, result[0], result[1])
    else:
      return (
          ase_id,
          ascs.AseResponseCode.INVALID_ASE_ID,
          ascs.AseReasonCode.NONE,
      )

  def _on_client_disconnected(self, reason: int) -> None:
    del reason  # Unused.
    for ase in self.ase_state_machines.values():
      ase.state = AudioStreamEndpointCharacteristic.State.IDLE
    self._active_client = None

  async def on_write_ase_control_point(
      self, connection: device.Connection, operation: ascs.ASE_Operation
  ) -> None:
    if not self._active_client and connection:
      self._active_client = connection
      connection.once(
          connection.EVENT_DISCONNECTION, self._on_client_disconnected
      )

    responses: list[tuple[int, ascs.AseResponseCode, ascs.AseReasonCode]]
    logger.debug('*** ASCS Write %s ***', operation)

    if isinstance(operation, ascs.ASE_Config_Codec):
      responses = [
          await self.on_operation(operation.op_code, ase_id, args)
          for ase_id, *args in zip(
              operation.ase_id,
              operation.target_latency,
              operation.target_phy,
              operation.codec_id,
              operation.codec_specific_configuration,
          )
      ]
    elif isinstance(operation, ascs.ASE_Config_QOS):
      responses = [
          await self.on_operation(operation.op_code, ase_id, args)
          for ase_id, *args in zip(
              operation.ase_id,
              operation.cig_id,
              operation.cis_id,
              operation.sdu_interval,
              operation.framing,
              operation.phy,
              operation.max_sdu,
              operation.retransmission_number,
              operation.max_transport_latency,
              operation.presentation_delay,
          )
      ]
    elif isinstance(operation, (ascs.ASE_Enable, ascs.ASE_Update_Metadata)):
      responses = [
          await self.on_operation(operation.op_code, ase_id, args)
          for ase_id, *args in zip(
              operation.ase_id,
              operation.metadata,
          )
      ]
    elif isinstance(
        operation,
        (
            ascs.ASE_Receiver_Start_Ready,
            ascs.ASE_Disable,
            ascs.ASE_Receiver_Stop_Ready,
            ascs.ASE_Release,
        ),
    ):
      responses = [
          await self.on_operation(operation.op_code, ase_id, [])
          for ase_id in operation.ase_id
      ]
    else:
      responses = [
          (
              ase_id,
              ascs.AseResponseCode.UNSUPPORTED_OPCODE,
              ascs.AseReasonCode.NONE,
          )
          for ase_id in operation.ase_id
      ]

    control_point_notification = bytes(
        [operation.op_code, len(responses)]
    ) + b''.join(map(bytes, responses))
    await self.device.notify_subscribers(
        self.ase_control_point, control_point_notification
    )

    for ase_id, *_ in responses:
      if ase := self.ase_state_machines.get(ase_id):
        await self.device.notify_subscribers(ase, ase.data_value)


# -----------------------------------------------------------------------------
class AudioStreamControlServiceProxy(gatt_client.ProfileServiceProxy):
  """Audio Stream Control Service Proxy."""

  SERVICE_CLASS = AudioStreamControlService

  sink_ase: list[gatt_client.CharacteristicProxy[bytes]]
  source_ase: list[gatt_client.CharacteristicProxy[bytes]]
  ase_control_point: gatt_adapters.SerializableCharacteristicProxyAdapter[  # pyrefly: ignore[bad-specialization]
      ascs.ASE_Operation
  ]

  def __init__(self, service_proxy: gatt_client.ServiceProxy):
    self.service_proxy = service_proxy

    self.sink_ase = service_proxy.get_characteristics_by_uuid(
        gatt.GATT_SINK_ASE_CHARACTERISTIC
    )
    self.source_ase = service_proxy.get_characteristics_by_uuid(
        gatt.GATT_SOURCE_ASE_CHARACTERISTIC
    )
    self.ase_control_point = (
        gatt_adapters.SerializableCharacteristicProxyAdapter[  # pyrefly: ignore[bad-specialization]
            ascs.ASE_Operation
        ](
            service_proxy.get_characteristics_by_uuid(
                gatt.GATT_ASE_CONTROL_POINT_CHARACTERISTIC
            )[0],
            ascs.ASE_Operation,
        )
    )
