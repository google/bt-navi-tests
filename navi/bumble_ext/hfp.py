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

"""Extended Bumble implementation of HFP protocol."""

import logging

from bumble import hci
from bumble import hfp
from typing_extensions import override

_logger = logging.getLogger(__name__)

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


class HfProtocol(hfp.HfProtocol):
  """Customized HF Protocol."""
  controller_supported_codecs: list[hci.CodingFormat] | None = None

  @override
  async def setup_codec_connection(self, codec_id: int) -> None:
    self.active_codec = hfp.AudioCodec(codec_id)
    self.emit("codec_negotiation", self.active_codec)

    # Answer codec negotiation in the background, because ACL packets cannot be
    # sent during SCO setup.
    connection = self.dlc.multiplexer.l2cap_channel.connection
    connection.abort_on(
        "disconnection", self.execute_command(f"AT+BCS={codec_id}")
    )

  async def _get_controller_supported_codecs(self) -> list[hci.CodingFormat]:
    """Returns codecs supported by the controller."""
    device = self.dlc.multiplexer.l2cap_channel.connection.device
    try:
      response = await device.send_command(
          hci.HCI_Read_Local_Supported_Codecs_Command(), check_result=True
      )
    except hci.HCI_Error:
      _logger.debug("Failed to read supported codecs")
      return []

    controller_supported_codecs = list(
        hci.CodecID(codec)
        for codec in response.return_parameters.standard_codec_ids
    )
    _logger.debug("Supported codecs: %s", controller_supported_codecs)
    return controller_supported_codecs

  async def get_esco_parameters(self) -> hfp.EscoParameters:
    """Returns the ESCO parameters for the active codec.

    Returns:
      The ESCO parameters for the active codec.

    Raises:
      ValueError: If the active codec is not supported.
    """
    if self.controller_supported_codecs is None:
      self.controller_supported_codecs = (
          await self._get_controller_supported_codecs()
      )

    match self.active_codec:
      case hfp.AudioCodec.CVSD:
        # It's not common that the controller doesn't support CVSD.
        return hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S4]
      case hfp.AudioCodec.MSBC:
        if hci.CodecID.MSBC in self.controller_supported_codecs:
          return hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_MSBC_T2]
        else:
          return ESCO_PARAMETERS_T2_TRANSPARENT
      case hfp.AudioCodec.LC3_SWB:
        if hci.CodecID.LC3 in self.controller_supported_codecs:
          return ESCO_PARAMETERS_LC3_T2
        else:
          return ESCO_PARAMETERS_T2_TRANSPARENT
      case _:
        raise ValueError(f"Unsupported codec: {self.active_codec}")
