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

"""Extended Bumble implementation of HFP protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
import dataclasses
import enum
import logging
from typing import Literal, Self, overload

from bumble import core
from bumble import device as device_lib
from bumble import hci
from bumble import hfp
from bumble import rfcomm
from bumble import sdp
from typing_extensions import override

from navi.bumble_ext import rfcomm as rfcomm_ext

_logger = logging.getLogger(__name__)

SCO_H2_HEADER = (b"\x01\x08", b"\x01\x38", b"\x01\xC8", b"\x01\xF8")
SCO_H2_HEADER_SIZE = 2


ESCO_PARAMETERS_LC3_T2 = hfp.EscoParameters(
    transmit_coding_format=hci.CodingFormat(hci.CodecID.LC3),
    receive_coding_format=hci.CodingFormat(hci.CodecID.LC3),
    max_latency=0x000D,
    packet_type=(
        hci.HCI_Enhanced_Setup_Synchronous_Connection_Command.PacketType.NO_3_EV3
        | hci.HCI_Enhanced_Setup_Synchronous_Connection_Command.PacketType.NO_2_EV5
        | hci.HCI_Enhanced_Setup_Synchronous_Connection_Command.PacketType.NO_3_EV5
    ),
    input_bandwidth=64000,
    output_bandwidth=64000,
    retransmission_effort=hci.HCI_Enhanced_Setup_Synchronous_Connection_Command.RetransmissionEffort.OPTIMIZE_FOR_QUALITY,
)

ESCO_PARAMETERS_T2_TRANSPARENT = hfp.EscoParameters(
    transmit_coding_format=hci.CodingFormat(hci.CodecID.TRANSPARENT),
    receive_coding_format=hci.CodingFormat(hci.CodecID.TRANSPARENT),
    max_latency=0x000D,
    packet_type=(
        hci.HCI_Enhanced_Setup_Synchronous_Connection_Command.PacketType.NO_3_EV3
        | hci.HCI_Enhanced_Setup_Synchronous_Connection_Command.PacketType.NO_2_EV5
        | hci.HCI_Enhanced_Setup_Synchronous_Connection_Command.PacketType.NO_3_EV5
    ),
    input_coding_format=hci.CodingFormat(hci.CodecID.TRANSPARENT),
    output_coding_format=hci.CodingFormat(hci.CodecID.TRANSPARENT),
    input_bandwidth=8000,
    output_bandwidth=8000,
    retransmission_effort=hci.HCI_Enhanced_Setup_Synchronous_Connection_Command.RetransmissionEffort.OPTIMIZE_FOR_QUALITY,
)


@enum.unique
class SinkAudioPolicy(enum.IntEnum):
  UNCONFIGURED = 0
  ALLOWED = 1
  NOT_ALLOWED = 2


_HF_FEATURE_TO_SDP_FEATURE = {
    hfp.HfFeature.EC_NR: hfp.HfSdpFeature.EC_NR,
    hfp.HfFeature.THREE_WAY_CALLING: hfp.HfSdpFeature.THREE_WAY_CALLING,
    hfp.HfFeature.CLI_PRESENTATION_CAPABILITY: (
        hfp.HfSdpFeature.CLI_PRESENTATION_CAPABILITY
    ),
    hfp.HfFeature.VOICE_RECOGNITION_ACTIVATION: (
        hfp.HfSdpFeature.VOICE_RECOGNITION_ACTIVATION
    ),
    hfp.HfFeature.REMOTE_VOLUME_CONTROL: hfp.HfSdpFeature.REMOTE_VOLUME_CONTROL,
    hfp.HfFeature.ENHANCED_VOICE_RECOGNITION_STATUS: (
        hfp.HfSdpFeature.ENHANCED_VOICE_RECOGNITION_STATUS
    ),
    hfp.HfFeature.VOICE_RECOGNITION_TEXT: (
        hfp.HfSdpFeature.VOICE_RECOGNITION_TEXT
    ),
}

_AG_FEATURE_TO_SDP_FEATURE = {
    hfp.AgFeature.EC_NR: hfp.AgSdpFeature.EC_NR,
    hfp.AgFeature.THREE_WAY_CALLING: hfp.AgSdpFeature.THREE_WAY_CALLING,
    hfp.AgFeature.ENHANCED_VOICE_RECOGNITION_STATUS: (
        hfp.AgSdpFeature.ENHANCED_VOICE_RECOGNITION_STATUS
    ),
    hfp.AgFeature.VOICE_RECOGNITION_TEXT: (
        hfp.AgSdpFeature.VOICE_RECOGNITION_TEXT
    ),
    hfp.AgFeature.IN_BAND_RING_TONE_CAPABILITY: (
        hfp.AgSdpFeature.IN_BAND_RING_TONE_CAPABILITY
    ),
    hfp.AgFeature.VOICE_RECOGNITION_FUNCTION: (
        hfp.AgSdpFeature.VOICE_RECOGNITION_FUNCTION
    ),
}


def make_hf_sdp_features(
    configuration: hfp.HfConfiguration,
) -> hfp.HfSdpFeature:
  """Compose HF SDP features from HF configuration."""

  features = hfp.HfSdpFeature(0)

  for hf_feature, sdp_feature in _HF_FEATURE_TO_SDP_FEATURE.items():
    if hf_feature in configuration.supported_hf_features:
      features |= sdp_feature

  if hfp.AudioCodec.MSBC in configuration.supported_audio_codecs:
    features |= hfp.HfSdpFeature.WIDE_BAND_SPEECH
  return features


def make_ag_sdp_features(
    configuration: hfp.AgConfiguration,
) -> hfp.AgSdpFeature:
  """Compose AG SDP features from AG configuration."""

  features = hfp.AgSdpFeature(0)

  for ag_feature, sdp_feature in _AG_FEATURE_TO_SDP_FEATURE.items():
    if ag_feature in configuration.supported_ag_features:
      features |= sdp_feature

  if hfp.AudioCodec.MSBC in configuration.supported_audio_codecs:
    features |= hfp.AgSdpFeature.WIDE_BAND_SPEECH

  return features


@dataclasses.dataclass
class HandsfreeSdpRecord:
  """Hands-free SDP record."""

  service_record_handle: int
  rfcomm_channel: int
  version: hfp.ProfileVersion
  supported_features: hfp.HfSdpFeature

  def to_service_attributes(self) -> list[sdp.ServiceAttribute]:
    """Converts the SDP record to a list of SDP service attributes.

    The record exposes the features supported in the input configuration,
    and the allocated RFCOMM channel.

    Returns:
      A list of SDP service attributes.
    """
    return [
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_32(self.service_record_handle),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.uuid(core.BT_HANDSFREE_SERVICE),
                sdp.DataElement.uuid(core.BT_GENERIC_AUDIO_SERVICE),
            ]),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence(
                    [sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID)]
                ),
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(core.BT_RFCOMM_PROTOCOL_ID),
                    sdp.DataElement.unsigned_integer_8(self.rfcomm_channel),
                ]),
            ]),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(core.BT_HANDSFREE_SERVICE),
                    sdp.DataElement.unsigned_integer_16(self.version),
                ])
            ]),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_16(self.supported_features),
        ),
    ]

  @classmethod
  async def find(
      cls,
      connection: device_lib.Connection,
  ) -> list[Self]:
    """Searches all Hands-Free SDP records from remote device.

    Args:
        connection: ACL connection to make SDP search.

    Returns:
        A list of Hands-Free SDP records.
    """
    records = []
    async with sdp.Client(connection) as sdp_client:
      search_result = await sdp_client.search_attributes(
          uuids=[core.BT_HANDSFREE_SERVICE],
          attribute_ids=[
              sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
              sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID,
              sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
          ],
      )
      for attribute_lists in search_result:
        channel: int | None = None
        version: hfp.ProfileVersion | None = None
        features: hfp.HfSdpFeature | None = None
        service_record_handle: int | None = None
        for attribute in attribute_lists:
          match attribute.id:
            case sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID:
              service_record_handle = attribute.value.value
            case sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID:
              # The layout is:
              #   [[L2CAP_PROTOCOL], [RFCOMM_PROTOCOL, RFCOMM_CHANNEL]].
              protocol_descriptor_list = attribute.value.value
              channel = protocol_descriptor_list[1].value[1].value
            case sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID:
              profile_descriptor_list = attribute.value.value
              version = hfp.ProfileVersion(
                  profile_descriptor_list[0].value[1].value
              )
            case sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID:
              features = hfp.HfSdpFeature(attribute.value.value)
            case sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID:
              class_id_list = attribute.value.value
              uuid = class_id_list[0].value
              # AG record may also contain HF UUID in its profile descriptor
              # list.
              # If found, skip this record.
              if uuid == core.BT_HANDSFREE_AUDIO_GATEWAY_SERVICE:
                channel = None
                version = None
                features = None
                service_record_handle = None
                break

        if (
            channel is None
            or version is None
            or features is None
            or service_record_handle is None
        ):
          continue
        records.append(
            cls(
                service_record_handle=service_record_handle,
                rfcomm_channel=channel,
                version=version,
                supported_features=features,
            )
        )
    return records


@dataclasses.dataclass
class AudioGatewaySdpRecord:
  """Audio-gateway SDP record."""

  service_record_handle: int
  rfcomm_channel: int
  version: hfp.ProfileVersion
  supported_features: hfp.AgSdpFeature

  def to_service_attributes(self) -> list[sdp.ServiceAttribute]:
    """Converts the SDP record to a list of SDP service attributes.

    The record exposes the features supported in the input configuration,
    and the allocated RFCOMM channel.

    Returns:
      A list of SDP service attributes.
    """

    return [
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_32(self.service_record_handle),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.uuid(core.BT_HANDSFREE_AUDIO_GATEWAY_SERVICE),
                sdp.DataElement.uuid(core.BT_GENERIC_AUDIO_SERVICE),
            ]),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence(
                    [sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID)]
                ),
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(core.BT_RFCOMM_PROTOCOL_ID),
                    sdp.DataElement.unsigned_integer_8(self.rfcomm_channel),
                ]),
            ]),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            sdp.DataElement.sequence([
                sdp.DataElement.sequence([
                    sdp.DataElement.uuid(
                        core.BT_HANDSFREE_AUDIO_GATEWAY_SERVICE
                    ),
                    sdp.DataElement.unsigned_integer_16(self.version),
                ])
            ]),
        ),
        sdp.ServiceAttribute(
            sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_16(self.supported_features),
        ),
    ]

  @classmethod
  async def find(
      cls,
      connection: device_lib.Connection,
  ) -> list[AudioGatewaySdpRecord]:
    """Searches all Audio-Gateway SDP record from remote device.

    Args:
        connection: ACL connection to make SDP search.

    Returns:
        A list of Audio-Gateway SDP records.
    """
    records: list[AudioGatewaySdpRecord] = []
    async with sdp.Client(connection) as sdp_client:
      search_result = await sdp_client.search_attributes(
          uuids=[core.BT_HANDSFREE_AUDIO_GATEWAY_SERVICE],
          attribute_ids=[
              sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
              sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
              sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID,
          ],
      )
      for attribute_lists in search_result:
        channel: int | None = None
        version: hfp.ProfileVersion | None = None
        features: hfp.AgSdpFeature | None = None
        service_record_handle: int | None = None
        for attribute in attribute_lists:
          match attribute.id:
            case sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID:
              service_record_handle = attribute.value.value
            case sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID:
              # The layout is:
              #   [[L2CAP_PROTOCOL], [RFCOMM_PROTOCOL, RFCOMM_CHANNEL]].
              protocol_descriptor_list = attribute.value.value
              channel = protocol_descriptor_list[1].value[1].value
            case sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID:
              profile_descriptor_list = attribute.value.value
              version = hfp.ProfileVersion(
                  profile_descriptor_list[0].value[1].value
              )
            case sdp.SDP_SUPPORTED_FEATURES_ATTRIBUTE_ID:
              features = hfp.AgSdpFeature(attribute.value.value)
        if (
            channel is None
            or version is None
            or features is None
            or service_record_handle is None
        ):
          continue
        records.append(
            cls(
                service_record_handle=service_record_handle,
                rfcomm_channel=channel,
                version=version,
                supported_features=features,
            )
        )
    return records


def make_hf_configuration(
    supported_hf_features: Sequence[hfp.HfFeature] = (),
    supported_hf_indicators: Sequence[hfp.HfIndicator] = (),
    supported_audio_codecs: Sequence[hfp.AudioCodec] = (
        hfp.AudioCodec.CVSD,
        hfp.AudioCodec.MSBC,
    ),
) -> hfp.HfConfiguration:
  """Creates an HFP HF configuration.

  Args:
    supported_hf_features: A list of supported HF features.
    supported_hf_indicators: A list of supported HF indicators.
    supported_audio_codecs: A list of supported audio codecs. If empty, defaults
      to CVSD and MSBC.

  Returns:
    An HfConfiguration object.
  """
  return hfp.HfConfiguration(
      supported_hf_features=supported_hf_features,
      supported_hf_indicators=supported_hf_indicators,
      supported_audio_codecs=supported_audio_codecs,
  )


def make_ag_configuration(
    supported_ag_features: Iterable[hfp.AgFeature] = (
        hfp.AgFeature.ENHANCED_CALL_STATUS,
    ),
    supported_ag_indicators: Sequence[hfp.AgIndicatorState] = (
        hfp.AgIndicatorState.call(),
        hfp.AgIndicatorState.callsetup(),
        hfp.AgIndicatorState.service(),
        hfp.AgIndicatorState.signal(),
        hfp.AgIndicatorState.roam(),
        hfp.AgIndicatorState.callheld(),
        hfp.AgIndicatorState.battchg(),
    ),
    supported_hf_indicators: Iterable[hfp.HfIndicator] = (),
    supported_ag_call_hold_operations: Iterable[hfp.CallHoldOperation] = (),
    supported_audio_codecs: Iterable[hfp.AudioCodec] = (hfp.AudioCodec.CVSD,),
) -> hfp.AgConfiguration:
  """Creates an HFP AG configuration.

  Args:
    supported_ag_features: A list of supported AG features.
    supported_ag_indicators: A list of supported AG indicators.
    supported_hf_indicators: A list of supported HF indicators.
    supported_ag_call_hold_operations: A list of supported AG call hold
      operations.
    supported_audio_codecs: A list of supported audio codecs. If empty, defaults
      to CVSD.

  Returns:
    An AG Configuration object.
  """
  return hfp.AgConfiguration(
      supported_ag_features=supported_ag_features,
      supported_ag_indicators=supported_ag_indicators,
      supported_hf_indicators=supported_hf_indicators,
      supported_ag_call_hold_operations=supported_ag_call_hold_operations,
      supported_audio_codecs=supported_audio_codecs,
  )


class HfProtocol(hfp.HfProtocol):
  """Customized HF Protocol."""

  controller_supported_codecs: list[hci.CodecID] | None = None
  slc_initialized: asyncio.Event

  @classmethod
  def setup_server(
      cls,
      device: device_lib.Device,
      sdp_handle: int,
      configuration: hfp.HfConfiguration,
      auto_accept_sco_request: bool = True,
  ) -> asyncio.Queue["HfProtocol"]:
    """Creates a HFP server on the given device.

    Args:
      device: The device to create the HFP server on.
      sdp_handle: The SDP handle to use for the HFP server.
      configuration: The configuration to use for the HFP server.
      auto_accept_sco_request: Whether to automatically accept SCO requests.

    Returns:
      A queue of HFP protocols.
    """
    protocol_queue = asyncio.Queue[HfProtocol]()

    def on_dlc(dlc: rfcomm.DLC) -> None:
      _logger.info("[REF] HFP DLC connected %s.", dlc)
      hfp_protocol = cls(dlc, configuration, auto_accept_sco_request)
      protocol_queue.put_nowait(hfp_protocol)
      dlc.multiplexer.l2cap_channel.connection.cancel_on_disconnection(
          hfp_protocol.run()
      )

    # Create and register a server.
    rfcomm_server = rfcomm_ext.get_rfcomm_server(device) or rfcomm.Server(
        device
    )

    # Listen for incoming DLC connections.
    channel_number = rfcomm_server.listen(on_dlc)
    _logger.info(
        "[REF] Listening for RFCOMM connection on channel %s.", channel_number
    )
    device.sdp_service_records[sdp_handle] = HandsfreeSdpRecord(
        service_record_handle=sdp_handle,
        rfcomm_channel=channel_number,
        version=hfp.ProfileVersion.V1_8,
        supported_features=make_hf_sdp_features(configuration),
    ).to_service_attributes()
    return protocol_queue

  def __init__(
      self,
      dlc: rfcomm.DLC,
      configuration: hfp.HfConfiguration,
      auto_accept_sco_request: bool = True,
  ) -> None:
    self.auto_accept_sco_request = auto_accept_sco_request
    self.slc_initialized = asyncio.Event()
    if auto_accept_sco_request:
      device = dlc.multiplexer.l2cap_channel.connection.device
      device.on(device.EVENT_SCO_REQUEST, self._on_sco_request)
      dlc.once(dlc.EVENT_CLOSE, self._on_disconnection)

    super().__init__(dlc=dlc, configuration=configuration)

    self.speaker_volume_condition = asyncio.Condition()
    self.speaker_volume = 7
    self.on(self.EVENT_SPEAKER_VOLUME, self._on_speaker_volume)

    self.microphone_volume_condition = asyncio.Condition()
    self.microphone_volume = 7
    self.on(self.EVENT_MICROPHONE_VOLUME, self._on_microphone_volume)

  @override
  async def initiate_slc(self) -> None:
    await super().initiate_slc()
    self.slc_initialized.set()

  @overload
  @override
  async def execute_command(  # pyrefly: ignore[invalid-overload]
      self,
      cmd: str,
      timeout: float = 10.0,
      *,
      response_type: Literal[hfp.AtResponseType.NONE] = hfp.AtResponseType.NONE,
  ) -> None:
    ...

  @overload
  @override
  async def execute_command(  # pyrefly: ignore[invalid-overload]
      self,
      cmd: str,
      timeout: float = 10.0,
      *,
      response_type: Literal[hfp.AtResponseType.SINGLE],
  ) -> hfp.AtResponse:
    ...

  @overload
  @override
  async def execute_command(  # pyrefly: ignore[invalid-overload]
      self,
      cmd: str,
      timeout: float = 10.0,
      *,
      response_type: Literal[hfp.AtResponseType.MULTIPLE],
  ) -> list[hfp.AtResponse]:
    ...

  @override
  async def execute_command(
      self,
      cmd: str,
      timeout: float = 10.0,
      response_type: hfp.AtResponseType = hfp.AtResponseType.NONE,
  ) -> None | hfp.AtResponse | list[hfp.AtResponse]:
    return await super().execute_command(
        cmd, timeout=timeout, response_type=response_type  # type: ignore[call-overload]
    )

  @override
  async def setup_codec_connection(self, codec_id: int) -> None:
    self.active_codec = hfp.AudioCodec(codec_id)
    self.emit("codec_negotiation", self.active_codec)

    # Answer codec negotiation in the background, because ACL packets cannot be
    # sent during SCO setup.
    connection = self.dlc.multiplexer.l2cap_channel.connection
    connection.cancel_on_disconnection(
        self.execute_command(f"AT+BCS={codec_id}")
    )

  async def _on_speaker_volume(self, volume_level: int) -> None:
    async with self.speaker_volume_condition:
      self.speaker_volume = volume_level
      self.speaker_volume_condition.notify_all()

  async def _on_microphone_volume(self, volume_level: int) -> None:
    async with self.microphone_volume_condition:
      self.microphone_volume = volume_level
      self.microphone_volume_condition.notify_all()

  def _on_disconnection(self) -> None:
    device = self.dlc.multiplexer.l2cap_channel.connection.device
    device.remove_listener(device.EVENT_SCO_REQUEST, self._on_sco_request)

  async def _on_sco_request(
      self, connection: device_lib.Connection, link_type: int
  ) -> None:
    """Called when a SCO request is received."""
    del link_type
    await self.accept_sco_request(connection)

  async def accept_sco_request(
      self, connection: device_lib.Connection | None = None
  ) -> None:
    """Accepts Bumble SCO request."""
    connection = connection or self.dlc.multiplexer.l2cap_channel.connection
    await connection.device.send_command(
        hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
            bd_addr=connection.peer_address,
            **(self.get_esco_parameters()).asdict(),
        )
    )

  def get_esco_parameters(self) -> hfp.EscoParameters:
    """Returns the ESCO parameters for the active codec.

    Returns:
      The ESCO parameters for the active codec.

    Raises:
      ValueError: If the active codec is not supported.
    """
    match self.active_codec:
      case hfp.AudioCodec.CVSD:
        # It's not common that the controller doesn't support CVSD.
        return hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S4]
      case hfp.AudioCodec.MSBC | hfp.AudioCodec.LC3_SWB:
        return ESCO_PARAMETERS_T2_TRANSPARENT
      case _:
        raise ValueError(f"Unsupported codec: {self.active_codec}")

  async def set_sink_audio_policy(
      self,
      call_establish: SinkAudioPolicy,
      connecting_time: SinkAudioPolicy,
      in_band_ringtone: SinkAudioPolicy,
      timeout: float = 10.0,
  ) -> None:
    """Sends AT+ANDROID=SINKAUDIOPOLICY command."""
    await self.execute_command(
        f"AT+ANDROID=SINKAUDIOPOLICY,{call_establish.value},{connecting_time.value},{in_band_ringtone.value}",
        timeout=timeout,
    )
