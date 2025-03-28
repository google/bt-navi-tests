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

"""Tests when remote device supports both HFP and A2DP."""

import asyncio
import datetime
from typing import TypeAlias, cast

from bumble import a2dp
from bumble import avdtp
from bumble import avrcp
from bumble import device
from bumble import hci
from bumble import hfp
from bumble import rfcomm
from mobly import test_runner
from typing_extensions import override

from navi.bumble_ext import a2dp as a2dp_ext
from navi.bumble_ext import hfp as hfp_ext
from navi.tests import navi_test_base
from navi.tests.smoke import a2dp_test
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants


_DEFAULT_STEP_TIMEOUT_SECONDS = 5.0
_A2DP_SERVICE_RECORD_HANDLE = 1
_AVRCP_CONTROLLER_RECORD_HANDLE = 2
_AVRCP_TARGET_RECORD_HANDLE = 3
_HFP_SDP_HANDLE = 4
_CALLER_NAME = "Pixel Bluetooth"
_CALLER_NUMBER = "123456789"

_AudioCodec = hfp.AudioCodec
_ConnectionState = android_constants.ConnectionState
_CallbackHandler: TypeAlias = bl4a_api.CallbackHandler
_Module: TypeAlias = bl4a_api.Module
_CallState = android_constants.CallState
_ScoState = android_constants.ScoState
_HfpAgAudioStateChange = bl4a_api.HfpAgAudioStateChanged


class ClassicHeadsetTest(navi_test_base.TwoDevicesTestBase):
  ref_hfp_protocol: hfp_ext.HfProtocol | None = None
  ref_sinks: dict[a2dp_ext.A2dpCodec, avdtp.LocalSink] = {}
  ref_avrcp_protocol: avrcp.Protocol
  ref_avrcp_delegator: a2dp_test.AvrcpDelegate
  ref_supports_lc3: bool

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    # Make sure Bumble is on.
    await self.ref.open()
    response = await self.ref.device.send_command(
        hci.HCI_Read_Local_Supported_Codecs_Command(),
        check_result=True,
    )
    supported_codecs = list(
        hci.CodecID(codec)
        for codec in response.return_parameters.standard_codec_ids
    )
    self.logger.info("[REF] Supported codecs: %s", supported_codecs)
    self.ref_supports_lc3 = hci.CodecID.LC3 in supported_codecs

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    self.ref_hfp_protocol = None
    self.ref_sinks.clear()

  @override
  async def async_teardown_test(self) -> None:
    await super().async_teardown_test()
    self.dut.bt.audioStop()
    # Reset audio attributes.
    self.dut.bt.setAudioAttributes(None, False)

  async def _wait_for_hfp_state(
      self,
      dut_hfp_ag_callback: _CallbackHandler,
      state: _ConnectionState,
  ) -> None:
    await dut_hfp_ag_callback.wait_for_event(
        callback_type=bl4a_api.ProfileConnectionStateChanged,
        predicate=lambda e: (
            e.address == self.ref.address and e.state == state
        ),
        timeout=datetime.timedelta(seconds=10),
    )

  async def _wait_for_sco_state(
      self,
      dut_hfp_ag_callback: _CallbackHandler,
      state: _ScoState,
  ) -> None:
    await dut_hfp_ag_callback.wait_for_event(
        callback_type=_HfpAgAudioStateChange,
        predicate=lambda e: (
            e.address == self.ref.address and e.state == state
        ),
    )

  async def _wait_for_sco_available(
      self,
      dut_audio_callback: _CallbackHandler,
  ) -> None:
    await dut_audio_callback.wait_for_event(
        callback_type=bl4a_api.AudioDeviceAdded,
        predicate=lambda e: (
            e.address == self.ref.address
            and e.device_type == android_constants.AudioDeviceType.BLUETOOTH_SCO
        ),
    )

  @classmethod
  def _default_hfp_configuration(cls) -> hfp.HfConfiguration:
    return hfp.HfConfiguration(
        supported_hf_features=[],
        supported_hf_indicators=[],
        supported_audio_codecs=[
            _AudioCodec.CVSD,
            _AudioCodec.MSBC,
        ],
    )

  def _setup_headset_device(
      self,
      hfp_configuration: hfp.HfConfiguration,
      a2dp_codecs: list[a2dp_ext.A2dpCodec],
      auto_accept_sco: bool = True,
  ) -> None:
    """Setup HFP and A2DP servicer on the REF device."""

    # Setup HFP
    def on_dlc(dlc: rfcomm.DLC) -> None:
      self.logger.info("[REF] HFP DLC connected %s.", dlc)
      self.ref_hfp_protocol = hfp_ext.HfProtocol(dlc, hfp_configuration)
      dlc.multiplexer.l2cap_channel.connection.abort_on(
          "disconnection", self.ref_hfp_protocol.run()
      )

    # Create and register a server.
    rfcomm_server = rfcomm.Server(self.ref.device)

    # Listen for incoming DLC connections.
    channel_number = rfcomm_server.listen(on_dlc)
    self.logger.info(
        "[REF] Listening for RFCOMM connection on channel %s.", channel_number
    )
    self.ref.device.sdp_service_records = {
        _HFP_SDP_HANDLE: hfp.make_hf_sdp_records(
            service_record_handle=_HFP_SDP_HANDLE,
            rfcomm_channel=channel_number,
            configuration=hfp_configuration,
        ),
        _A2DP_SERVICE_RECORD_HANDLE: a2dp.make_audio_sink_service_sdp_records(
            _A2DP_SERVICE_RECORD_HANDLE
        ),
        _AVRCP_CONTROLLER_RECORD_HANDLE: (
            avrcp.make_controller_service_sdp_records(
                _AVRCP_CONTROLLER_RECORD_HANDLE
            )
        ),
        _AVRCP_TARGET_RECORD_HANDLE: avrcp.make_target_service_sdp_records(
            _AVRCP_TARGET_RECORD_HANDLE
        ),
    }
    if auto_accept_sco:

      def on_sco_request(connection: device.Connection, link_type: int) -> None:
        del link_type
        connection.abort_on("disconnection", self._accept_sco_request())

      self.ref.device.on("sco_request", on_sco_request)

    def on_avdtp_connection(server: avdtp.Protocol) -> None:
      for codec in a2dp_codecs:
        self.ref_sinks[codec] = server.add_sink(
            codec.get_default_capabilities()
        )

    avdtp_listener = avdtp.Listener.for_device(self.ref.device)
    avdtp_listener.on("connection", on_avdtp_connection)

    self.ref_avrcp_delegator = a2dp_test.AvrcpDelegate(
        supported_events=(avrcp.EventId.VOLUME_CHANGED,)
    )
    self.ref_avrcp_protocol = avrcp.Protocol(self.ref_avrcp_delegator)
    self.ref_avrcp_protocol.listen(self.ref.device)

  async def test_pair_and_connect(self) -> None:
    """Tests HFP connection establishment right after a pairing session.

    Test steps:
      1. Setup HFP and A2DP on REF.
      2. Create bond from DUT.
      3. Wait HFP and A2DP connected on DUT.
      (Android should autoconnect HFP as AG)
    """
    with (
        self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb_a2dp,
        self.dut.bl4a.register_callback(_Module.HFP_AG) as dut_cb_hfp,
    ):
      self._setup_headset_device(
          hfp_configuration=self._default_hfp_configuration(),
          a2dp_codecs=[a2dp_ext.A2dpCodec.SBC],
      )
      self.logger.info("[DUT] Connect and pair REF.")
      await self.classic_connect_and_pair()
      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb_a2dp.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: (e.state == _ConnectionState.CONNECTED),
      )
      self.logger.info("[DUT] Wait for HFP connected.")
      await self._wait_for_hfp_state(dut_cb_hfp, _ConnectionState.CONNECTED)

  async def _accept_sco_request(self) -> None:
    """Accepts Bumble SCO request."""
    if not self.ref_hfp_protocol:
      self.fail("HFP protocol not setup.")
    connection = self.ref_hfp_protocol.dlc.multiplexer.l2cap_channel.connection
    await self.ref.device.send_command(
        hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
            bd_addr=connection.peer_address,
            **(await self.ref_hfp_protocol.get_esco_parameters()).asdict(),
        )
    )

  @navi_test_base.named_parameterized(
      cvsd=dict(
          supported_audio_codecs=[_AudioCodec.CVSD],
      ),
      cvsd_and_msbc=dict(
          supported_audio_codecs=[_AudioCodec.CVSD, _AudioCodec.MSBC],
      ),
      cvsd_msbc_and_lc3_swb=dict(
          supported_audio_codecs=[
              _AudioCodec.LC3_SWB,
              _AudioCodec.CVSD,
              _AudioCodec.MSBC,
          ],
      ),
      handle_audio_focus=dict(
          supported_audio_codecs=[
              _AudioCodec.LC3_SWB,
              _AudioCodec.CVSD,
              _AudioCodec.MSBC,
          ],
          handle_audio_focus=True,
      ),
  )
  async def test_call_during_a2dp_playback(
      self,
      supported_audio_codecs: list[hfp.AudioCodec],
      handle_audio_focus: bool = False,
  ) -> None:
    """Tests making an outgoing phone call, observing SCO connection status.

    Test steps:
      1. Setup HFP and A2DP connection.
      2. Play sine and check A2DP is playing.
      3. Place an outgoing call.
      4. Check A2DP is stopped.
      5. Verify SCO connected.
      6. Terminate the call.
      7. Verify SCO disconnected.
      8. Verify A2DP resumed.

    Args:
      supported_audio_codecs: Audio codecs supported by REF device.
      handle_audio_focus: Whether to enable audio focus handling.
    """
    if (
        _AudioCodec.LC3_SWB in supported_audio_codecs
        and not self.ref_supports_lc3
    ):
      self.skipTest("LC3 not supported on REF.")

    # Enable audio focus handling.
    self.dut.bt.setAudioAttributes(None, handle_audio_focus)

    # [REF] Setup HFP.
    hfp_configuration = hfp.HfConfiguration(
        supported_hf_features=[hfp.HfFeature.CODEC_NEGOTIATION],
        supported_hf_indicators=[],
        supported_audio_codecs=supported_audio_codecs,
    )
    self._setup_headset_device(
        hfp_configuration=hfp_configuration,
        a2dp_codecs=[a2dp_ext.A2dpCodec.SBC],
        auto_accept_sco=True,
    )

    dut_hfp_cb = self.dut.bl4a.register_callback(_Module.HFP_AG)
    dut_a2dp_cb = self.dut.bl4a.register_callback(_Module.A2DP)
    dut_audio_cb = self.dut.bl4a.register_callback(_Module.AUDIO)
    self.close_after_test.append(dut_hfp_cb)
    self.close_after_test.append(dut_a2dp_cb)
    self.close_after_test.append(dut_audio_cb)

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair()

    self.logger.info("[DUT] Wait for A2DP connected.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged,
        lambda e: (e.state == _ConnectionState.CONNECTED),
    )
    self.logger.info("[DUT] Wait for HFP connected.")
    await self._wait_for_hfp_state(dut_hfp_cb, _ConnectionState.CONNECTED)

    self.logger.info("[DUT] Start stream.")
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ALL)
    self.dut.bt.audioPlaySine()

    self.logger.info("[DUT] Check A2DP is playing.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.A2dpPlayingStateChanged,
        lambda e: (e.state == android_constants.A2dpState.PLAYING),
    )

    self.logger.info("[DUT] Wait for SCO device available.")
    await self._wait_for_sco_available(dut_audio_cb)

    sco_links = asyncio.Queue[device.ScoLink]()
    self.ref.device.on("sco_connection", sco_links.put_nowait)

    dut_player_cb = self.dut.bl4a.register_callback(_Module.PLAYER)
    self.close_after_test.append(dut_player_cb)

    self.logger.info("[DUT] Add call.")
    call = self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.OUTGOING,
    )
    with call:
      self.logger.info("[DUT] Check A2DP is not playing.")
      await dut_a2dp_cb.wait_for_event(
          bl4a_api.A2dpPlayingStateChanged,
          lambda e: (e.state == android_constants.A2dpState.NOT_PLAYING),
      )
      self.logger.info("[DUT] Wait for SCO connected.")
      await self._wait_for_sco_state(dut_hfp_cb, _ScoState.CONNECTED)
      if handle_audio_focus:
        self.logger.info("[DUT] Wait for player paused.")
        await dut_player_cb.wait_for_event(
            bl4a_api.PlayerIsPlayingChanged,
            lambda e: (not cast(bl4a_api.PlayerIsPlayingChanged, e).is_playing),
        )

      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.logger.info("[REF] Wait for SCO connected.")
        sco_link = await sco_links.get()

      sco_disconnected = asyncio.Event()
      sco_link.once("disconnection", lambda *_: sco_disconnected.set())

      self.logger.info("[DUT] Terminate call.")
      call.close()

    self.logger.info("[DUT] Wait for SCO disconnected.")
    await self._wait_for_sco_state(dut_hfp_cb, _ScoState.DISCONNECTED)
    self.logger.info("[REF] Wait for SCO disconnected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await sco_disconnected.wait()

    self.logger.info("[DUT] Wait for A2DP resume.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.A2dpPlayingStateChanged,
        lambda e: (e.state == android_constants.A2dpState.PLAYING),
    )
    if handle_audio_focus:
      self.logger.info("[DUT] Wait for player resumed.")
      await dut_player_cb.wait_for_event(
          bl4a_api.PlayerIsPlayingChanged,
          lambda e: (cast(bl4a_api.PlayerIsPlayingChanged, e).is_playing),
      )


if __name__ == "__main__":
  test_runner.main()
