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

"""Tests switching between multiple devices."""

import asyncio
import collections
import contextlib
from typing import TypeAlias
from unittest import mock

from bumble import device
from bumble import hci
from bumble import hfp
from bumble import rfcomm
from bumble.profiles import gmap
from bumble.profiles import vcs
from mobly import test_runner
from typing_extensions import override

from navi.bumble_ext import a2dp as a2dp_ext
from navi.bumble_ext import ascs
from navi.bumble_ext import avrcp as avrcp_ext
from navi.bumble_ext import hfp as hfp_ext
from navi.bumble_ext import pacs
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import pyee_extensions

_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_A2DP_SERVICE_RECORD_HANDLE = 1
_AVRCP_CONTROLLER_RECORD_HANDLE = 2
_AVRCP_TARGET_RECORD_HANDLE = 3
_HFP_HF_SDP_HANDLE = 4
_HFP_AG_SDP_HANDLE = 5
_CALLER_NAME = "Pixel Bluetooth"
_CALLER_NUMBER = "123456789"
_SINK_ASE_ID = 1
_SOURCE_ASE_ID = 2
_PROPERTY_HF_FEATURES = "bluetooth.hfp.hf_client_features.config"
_PROPERTY_SWB_SUPPORTED = "bluetooth.hfp.swb.supported"
_SETUP_TIMEOUT_SECONDS = 15.0


_AudioCodec = hfp.AudioCodec
_Module: TypeAlias = bl4a_api.Module
_ScoState = android_constants.ScoState
_HfpAgAudioStateChange = bl4a_api.HfpAgAudioStateChanged
_CallState = android_constants.CallState
_CallbackHandler = bl4a_api.CallbackHandler
_AndroidProperty = android_constants.Property
_HfpState = android_constants.ConnectionState


async def _wait_for_ase_state(
    ase: ascs.AudioStreamEndpointCharacteristic,
    state: ascs.AudioStreamEndpointCharacteristic.State,
) -> None:
  """Waits for the ASE state to be changed to the specified state."""
  with pyee_extensions.EventTriggeredValueObserver(
      ase,
      event=ase.EVENT_STATE_CHANGE,
      value_producer=lambda: ase.state,
  ) as observer:
    await observer.wait_for_target_value(state)


class CoexTest(navi_test_base.MultiDevicesTestBase):

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    for i, ref in enumerate(self.refs):
      self.logger.info(
          "[REF-%d] Disable CTKD over Classic to avoid blocking SDP.", i
      )
      ref.config.classic_smp_enabled = False

    if self.dut.device.is_emulator:
      self.setprop_for_class_context(
          android_constants.Property.HFP_HF_ENABLED, "true"
      )

      self.setprop_for_class_context(_PROPERTY_HF_FEATURES, "0x1b5")

    if (
        self.dut.getprop(_AndroidProperty.BAP_UNICAST_CLIENT_ENABLED) == "true"
        and self.dut.bt.getHardware() != "cutf_cvm"
    ):
      self.setprop_for_class_context(
          _AndroidProperty.LEAUDIO_BYPASS_ALLOW_LIST, "true"
      )

  @override
  async def async_teardown_test(self) -> None:
    self.logger.info("[DUT] Stop audio.")
    self.dut.bt.audioStop()

    self.logger.info("[DUT] Reset audio attributes to default.")
    self.dut.bt.setAudioAttributes(None, False)

    await super().async_teardown_test()

  def _setup_headset_device(
      self,
      hf_configuration: hfp.HfConfiguration,
      a2dp_codecs: collections.abc.Sequence[a2dp_ext.A2dpCodec],
  ) -> None:
    """Setup HF and A2DP services on the REF device."""
    for i, ref in enumerate(self.refs):
      self.logger.info("[REF-%d] Setup HFP HF.", i)
      hfp_ext.HfProtocol.setup_server(
          ref.device,
          sdp_handle=_HFP_HF_SDP_HANDLE,
          configuration=hf_configuration,
      )

      self.logger.info("[REF-%d] Setup A2DP sink.", i)
      a2dp_ext.setup_sink_server(
          ref.device,
          [codec.get_default_capabilities() for codec in a2dp_codecs],
          _A2DP_SERVICE_RECORD_HANDLE,
      )

      self.logger.info("[REF-%d] Setup AVRCP.", i)
      avrcp_ext.setup_server(
          ref.device,
          avrcp_controller_handle=_AVRCP_CONTROLLER_RECORD_HANDLE,
          avrcp_target_handle=_AVRCP_TARGET_RECORD_HANDLE,
      )

  async def test_point_to_point_ag_and_a2dp(self) -> None:
    """Tests AG and A2DP connection to the same REF device.

    Test steps:
      1. Setup HF and A2DP on REF.
      2. Create bond from DUT.
      3. Wait for HFP and A2DP connected on DUT.
    """
    self.ref = self.refs[0]
    with (
        self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb_a2dp,
        self.dut.bl4a.register_callback(_Module.HFP_AG) as dut_cb_hfp,
    ):
      self._setup_headset_device(
          hf_configuration=hfp_ext.make_hf_configuration(),
          a2dp_codecs=[a2dp_ext.A2dpCodec.SBC],
      )

      self.logger.info("[DUT] Connect and pair REF.")
      await self.classic_connect_and_pair(connect_profiles=True)

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb_a2dp.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )

      self.logger.info("[DUT] Wait for HFP connected.")
      await dut_cb_hfp.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(self.ref.address)
      )

  @navi_test_base.named_parameterized(
      cvsd=dict(
          supported_audio_codecs=[_AudioCodec.CVSD],
      ),
      msbc=dict(
          supported_audio_codecs=[_AudioCodec.CVSD, _AudioCodec.MSBC],
      ),
      lc3_swb=dict(
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
  async def test_point_to_point_ag_call_during_a2dp(
      self,
      supported_audio_codecs: collections.abc.Sequence[hfp.AudioCodec],
      handle_audio_focus: bool = False,
  ) -> None:
    """Tests making an outgoing phone call while A2DP is playing.

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
    self.ref = self.refs[0]

    self.logger.info("[DUT] Set audio focus to %s.", handle_audio_focus)
    self.dut.bt.setAudioAttributes(None, handle_audio_focus)

    self._setup_headset_device(
        hf_configuration=hfp_ext.make_hf_configuration(
            supported_hf_features=[hfp.HfFeature.CODEC_NEGOTIATION],
            supported_audio_codecs=supported_audio_codecs,
        ),
        a2dp_codecs=[a2dp_ext.A2dpCodec.SBC],
    )

    dut_hfp_cb = self.dut.bl4a.register_callback(_Module.HFP_AG)
    dut_a2dp_cb = self.dut.bl4a.register_callback(_Module.A2DP)
    dut_player_cb = self.dut.bl4a.register_callback(_Module.PLAYER)
    self.test_case_context.push(dut_hfp_cb)
    self.test_case_context.push(dut_a2dp_cb)
    self.test_case_context.push(dut_player_cb)

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    self.logger.info("[DUT] Wait for A2DP connected.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
    )

    self.logger.info("[DUT] Wait for HFP connected.")
    await dut_hfp_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(self.ref.address)
    )

    self.logger.info("[DUT] Set repeat mode to all.")
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ALL)

    self.logger.info("[DUT] Start stream.")
    self.dut.bt.audioPlaySine()

    self.logger.info("[DUT] Check A2DP is playing.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.A2dpPlayingStateChanged(
            address=self.ref.address, state=android_constants.A2dpState.PLAYING
        ),
    )

    sco_links = asyncio.Queue[device.ScoLink]()
    self.ref.device.on(
        self.ref.device.EVENT_SCO_CONNECTION, sco_links.put_nowait
    )

    self.logger.info("[DUT] Add call.")
    call = self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.OUTGOING,
    )

    with call:
      self.logger.info("[DUT] Add a player for phone call.")
      phone_player = self.dut.bt.addPlayer()
      self.test_case_context.callback(self.dut.bt.removePlayer, phone_player)
      self.dut.bl4a.set_audio_attributes(
          bl4a_api.AudioAttributes(
              usage=bl4a_api.AudioAttributes.Usage.VOICE_COMMUNICATION,
              content_type=bl4a_api.AudioAttributes.ContentType.SPEECH,
          ),
          handle_audio_focus=False,
          player_id=phone_player,
      )
      self.dut.bt.audioPlaySine(phone_player)

      self.logger.info("[DUT] Check A2DP is not playing.")
      await dut_a2dp_cb.wait_for_event(
          bl4a_api.A2dpPlayingStateChanged(
              address=self.ref.address,
              state=android_constants.A2dpState.NOT_PLAYING,
          ),
      )

      self.logger.info("[DUT] Wait for SCO connected.")
      await dut_hfp_cb.wait_for_event(
          _HfpAgAudioStateChange(
              address=self.ref.address, state=_ScoState.CONNECTED
          ),
      )

      if handle_audio_focus:
        self.logger.info("[DUT] Wait for player paused.")
        await dut_player_cb.wait_for_event(
            bl4a_api.PlayerIsPlayingChanged(is_playing=False),
        )

      self.logger.info("[REF] Wait for SCO connected.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        sco_link = await sco_links.get()

      sco_disconnected = asyncio.Event()
      sco_link.once(
          sco_link.EVENT_DISCONNECTION, lambda *_: sco_disconnected.set()
      )

      self.logger.info("[DUT] Terminate call.")
      call.close()

    self.logger.info("[DUT] Wait for SCO disconnected.")
    await dut_hfp_cb.wait_for_event(
        _HfpAgAudioStateChange(
            address=self.ref.address, state=_ScoState.DISCONNECTED
        ),
    )

    self.logger.info("[REF] Wait for SCO disconnected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await sco_disconnected.wait()

    self.logger.info("[DUT] Wait for A2DP resume.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.A2dpPlayingStateChanged(
            address=self.ref.address, state=android_constants.A2dpState.PLAYING
        ),
    )

    if handle_audio_focus:
      self.logger.info("[DUT] Wait for player resumed.")
      await dut_player_cb.wait_for_event(
          bl4a_api.PlayerIsPlayingChanged(is_playing=True),
      )

  @navi_test_base.parameterized(
      hfp.AudioCodec.CVSD,
      hfp.AudioCodec.MSBC,
      hfp.AudioCodec.LC3_SWB,
  )
  async def test_multipoint_hf_call_during_a2dp(
      self,
      codec: hfp.AudioCodec,
  ) -> None:
    """Tests an incoming phone call from phone while A2DP is playing on buds.

    Test steps:
      1. Setup a2dp connection on REF-0 and hfp connection on REF-1.
      2. Play sine and check A2DP is playing on REF-0.
      3. Place an incoming call to REF-1.
      4. Check A2DP is stopped.
      5. Verify SCO connected.
      6. Terminate the call.
      7. Verify SCO disconnected.
      8. Verify A2DP resumed.

    Args:
      codec: Audio codec to be negotiated.
    """
    if self.dut.getprop(android_constants.Property.HFP_HF_ENABLED) != "true":
      self.skipTest("DUT does not have HFP HF enabled.")

    self.logger.info("[DUT] Enable audio focus handling.")
    self.dut.bl4a.set_audio_attributes(
        bl4a_api.AudioAttributes(usage=bl4a_api.AudioAttributes.Usage.MEDIA),
        handle_audio_focus=True,
    )

    self.logger.info("[REF-0] Setup A2DP sink.")
    a2dp_ext.setup_sink_server(
        self.refs[0].device,
        [a2dp_ext.A2dpCodec.SBC.get_default_capabilities()],
        _A2DP_SERVICE_RECORD_HANDLE,
    )
    avrcp_ext.setup_server(
        self.refs[0].device,
        avrcp_controller_handle=_AVRCP_CONTROLLER_RECORD_HANDLE,
        avrcp_target_handle=_AVRCP_TARGET_RECORD_HANDLE,
    )

    self.logger.info("[REF-1] Setup HFP AG.")
    ag_configuration = hfp_ext.make_ag_configuration(
        supported_audio_codecs=[
            hfp.AudioCodec.CVSD,
            hfp.AudioCodec.MSBC,
            hfp.AudioCodec.LC3_SWB,
        ],
        supported_ag_features=[
            hfp.AgFeature.ENHANCED_CALL_STATUS,
            hfp.AgFeature.CODEC_NEGOTIATION,
        ],
        supported_ag_indicators=[
            hfp.AgIndicatorState.call(),
            hfp.AgIndicatorState.callsetup(),
        ],
    )

    match codec:
      case hfp.AudioCodec.CVSD:
        esco_parameters = hfp.ESCO_PARAMETERS[
            hfp.DefaultCodecParameters.ESCO_CVSD_S4
        ]
      case hfp.AudioCodec.MSBC:
        esco_parameters = hfp.ESCO_PARAMETERS[
            hfp.DefaultCodecParameters.ESCO_MSBC_T2
        ]
      case hfp.AudioCodec.LC3_SWB:
        esco_parameters = hfp_ext.ESCO_PARAMETERS_LC3_T2
        if self.dut.getprop(_PROPERTY_SWB_SUPPORTED) != "true":
          self.skipTest("SWB is not supported on the device.")

    ref_hfp_protocols = asyncio.Queue[hfp.AgProtocol]()

    def on_dlc(dlc: rfcomm.DLC):
      ref_hfp_protocols.put_nowait(hfp.AgProtocol(dlc, ag_configuration))

    self.refs[1].device.sdp_service_records = {
        _HFP_AG_SDP_HANDLE: (
            hfp_ext.AudioGatewaySdpRecord(
                service_record_handle=_HFP_AG_SDP_HANDLE,
                rfcomm_channel=rfcomm.Server(self.refs[1].device).listen(
                    on_dlc
                ),
                version=hfp.ProfileVersion.V1_8,
                supported_features=hfp_ext.make_ag_sdp_features(
                    ag_configuration
                ),
            ).to_service_attributes()
        )
    }

    dut_hf_cb = self.dut.bl4a.register_callback(_Module.HFP_HF)
    dut_a2dp_cb = self.dut.bl4a.register_callback(_Module.A2DP)
    dut_player_cb = self.dut.bl4a.register_callback(_Module.PLAYER)
    dut_telecom_cb = self.dut.bl4a.register_callback(bl4a_api.Module.TELECOM)
    self.test_case_context.push(dut_hf_cb)
    self.test_case_context.push(dut_a2dp_cb)
    self.test_case_context.push(dut_player_cb)
    self.test_case_context.push(dut_telecom_cb)

    self.logger.info("[DUT] Connect and pair REF-0.")
    await self.classic_connect_and_pair(self.refs[0], connect_profiles=True)

    self.logger.info("[DUT] Wait for A2DP connected.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.refs[0].address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
    )

    self.logger.info("[DUT] Connect and pair REF-1.")
    await self.classic_connect_and_pair(
        self.refs[1],
        connect_profiles=True,
    )

    self.logger.info("[DUT] Wait for HFP connected to REF-1.")
    await dut_hf_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.refs[1].address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
    )

    self.logger.info("[REF-1] Wait for HFP AG protocol connected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_hfp_protocol = await ref_hfp_protocols.get()

    self.logger.info("[DUT] Set repeat mode to ALL.")
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ALL)

    self.logger.info("[DUT] Start stream.")
    self.dut.bt.audioPlaySine()

    self.logger.info("[DUT] Check A2DP is playing.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.A2dpPlayingStateChanged(
            address=self.refs[0].address,
            state=android_constants.A2dpState.PLAYING,
        ),
    )

    self.logger.info("[REF-1] Update call state.")
    call_info = hfp.CallInfo(
        index=1,
        direction=hfp.CallInfoDirection.MOBILE_TERMINATED_CALL,
        status=hfp.CallInfoStatus.INCOMING,
        mode=hfp.CallInfoMode.VOICE,
        multi_party=hfp.CallInfoMultiParty.NOT_IN_CONFERENCE,
        number="+1234567890",
    )
    ref_hfp_protocol.calls.append(call_info)
    ref_hfp_protocol.update_ag_indicator(
        hfp.AgIndicator.CALL_SETUP,
        hfp.CallSetupAgIndicator.INCOMING_CALL_PROCESS,
    )

    self.logger.info("[DUT] Wait for call ringing.")
    await dut_telecom_cb.wait_for_event(
        bl4a_api.CallStateChanged(
            handle=mock.ANY,
            name=mock.ANY,
            state=_CallState.RINGING,
        )
    )

    answered = asyncio.Event()
    ref_hfp_protocol.once(ref_hfp_protocol.EVENT_ANSWER, answered.set)

    self.logger.info("[DUT] Answer call.")
    self.dut.shell("input keyevent KEYCODE_CALL")

    self.logger.info("[REF] Wait for call answered.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await answered.wait()

    self.logger.info("[REF] Accept call.")
    call_info.status = hfp.CallInfoStatus.ACTIVE
    ref_hfp_protocol.update_ag_indicator(hfp.AgIndicator.CALL, 1)

    self.logger.info("[DUT] Wait for call state changed.")
    await dut_telecom_cb.wait_for_event(
        bl4a_api.CallStateChanged(
            handle=mock.ANY,
            name=mock.ANY,
            state=_CallState.ACTIVE,
        )
    )

    # Wait for A2DP to stop before setting up SCO, or some controllers may not
    # be able to accept the SCO connection when A2DP offloading is active.
    self.logger.info("[DUT] Check A2DP is not playing.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.A2dpPlayingStateChanged(
            address=self.refs[0].address,
            state=android_constants.A2dpState.NOT_PLAYING,
        ),
    )

    self.logger.info("[REF] Negotiate codec.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await ref_hfp_protocol.negotiate_codec(codec)

    sco_links = asyncio.Queue[device.ScoLink]()
    self.refs[1].device.on(
        self.refs[1].device.EVENT_SCO_CONNECTION, sco_links.put_nowait
    )

    self.logger.info("[REF] Create SCO.")
    connection = ref_hfp_protocol.dlc.multiplexer.l2cap_channel.connection
    await self.refs[1].device.send_command(
        hci.HCI_Enhanced_Setup_Synchronous_Connection_Command(
            connection_handle=connection.handle, **esco_parameters.asdict()
        )
    )

    self.logger.info("[DUT] Wait for SCO connected.")
    await dut_hf_cb.wait_for_event(
        bl4a_api.HfpHfAudioStateChanged(
            address=self.refs[1].address, state=_HfpState.CONNECTED
        ),
    )

    self.logger.info("[REF] Wait for SCO connected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      sco_link = await sco_links.get()

    self.logger.info("[REF] End call.")
    ref_hfp_protocol.calls.clear()
    ref_hfp_protocol.update_ag_indicator(hfp.AgIndicator.CALL, 0)

    self.logger.info("[DUT] Wait for call disconnected.")
    await dut_telecom_cb.wait_for_event(
        bl4a_api.CallStateChanged(
            handle=mock.ANY,
            name=mock.ANY,
            state=_CallState.DISCONNECTED,
        )
    )

    # DUT may disconnect SCO before REF.
    if sco_link in sco_link.device.sco_links.values():
      self.logger.info("[REF] Disconnect SCO.")
      with contextlib.suppress(hci.HCI_StatusError):
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          await sco_link.disconnect()

    self.logger.info("[DUT] Wait for SCO disconnected.")
    await dut_hf_cb.wait_for_event(
        bl4a_api.HfpHfAudioStateChanged(
            address=self.refs[1].address, state=_HfpState.DISCONNECTED
        ),
    )

    self.logger.info("[DUT] Wait for A2DP resume.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.A2dpPlayingStateChanged(
            address=self.refs[0].address,
            state=android_constants.A2dpState.PLAYING,
        ),
    )

  async def test_multidevice_a2dp_switch(self) -> None:
    """Tests DUT switch active a2dp devices.

    Test steps:
      1. Setup two A2DP devices.
      2. DUT pair with REF0.
      3. DUT pair with REF1.
      4. DUT switch active device to REF0.
      5. DUT switch active device to REF1.
    """
    if self.dut.bt.maxConnectedAudioDevices() < 2:
      self.skipTest("[DUT] Multi-device A2DP is not supported.")

    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_a2dp_cb:
      for i, ref in enumerate(self.refs):
        self.logger.info("[REF-%d] Setup A2DP", i)
        a2dp_ext.setup_sink_server(
            ref.device,
            [a2dp_ext.A2dpCodec.SBC.get_default_capabilities()],
            _A2DP_SERVICE_RECORD_HANDLE,
        )

        await self.classic_connect_and_pair(ref, connect_profiles=True)

        self.logger.info("[DUT] Wait for A2DP connected to REF-%d", i)
        await dut_a2dp_cb.wait_for_event(
            bl4a_api.ProfileActiveDeviceChanged(address=ref.address),
        )

    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_a2dp_cb:
      self.logger.info("[DUT] Start playing music.")
      self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)
      await asyncio.to_thread(self.dut.bt.audioPlaySine)

      if not self.dut.bt.isA2dpPlaying(self.refs[1].address):
        self.logger.info("[DUT] Wait for A2DP playing on REF-1.")
        await dut_a2dp_cb.wait_for_event(
            bl4a_api.A2dpPlayingStateChanged(
                self.refs[1].address, android_constants.A2dpState.PLAYING
            ),
        )

      # The default route should be REF-1.
      for i, ref in enumerate(self.refs):
        self.assertFalse(
            self.dut.bt.isA2dpPlaying(ref.address),
            f"A2DP is already playing on REF{i}.",
        )

        self.logger.info("[DUT] Switch to REF-%d", i)
        await asyncio.to_thread(
            self.dut.bt.setActiveDevice,
            ref.address,
            android_constants.ActiveDeviceUse.AUDIO,
        )

        self.logger.info("[DUT] Wait for A2DP connected to REF-%d", i)
        await dut_a2dp_cb.wait_for_event(
            bl4a_api.ProfileActiveDeviceChanged(ref.address)
        )

        if not self.dut.bt.isA2dpPlaying(ref.address):
          self.logger.info("[DUT] Wait for A2DP playing on REF-%d.", i)
          await dut_a2dp_cb.wait_for_event(
              bl4a_api.A2dpPlayingStateChanged(
                  ref.address, android_constants.A2dpState.PLAYING
              ),
          )

  async def test_multidevice_lea_switch(self) -> None:
    """Tests DUT switch active LEA devices.

    Test steps:
      1. Setup two LEA devices.
      2. DUT pair with REF0.
      3. DUT pair with REF1.
      4. Play music on DUT.
      5. Wait for music to start on REF1.
      6. DUT switch active device to REF0.
      7. DUT switch active device to REF1.
    """
    if self.dut.bt.maxConnectedAudioDevices() < 2:
      self.skipTest("[DUT] Multi-device LEA is not supported.")
    if not self.dut.is_le_audio_supported:
      self.skipTest("[DUT] Unicast client is not enabled")

    for ref in self.refs:
      ref.config.cis_enabled = True
      ref.device.cis_enabled = True

    async with self.assert_not_timeout(_SETUP_TIMEOUT_SECONDS):
      await asyncio.gather(
          *[ref.reset() for ref in self.refs],
      )

    self.logger.info("[DUT] Set audio attributes to media.")
    self.dut.bl4a.set_audio_attributes(
        bl4a_api.AudioAttributes(usage=bl4a_api.AudioAttributes.Usage.MEDIA),
        handle_audio_focus=False,
    )

    sink_ase = list[ascs.AudioStreamEndpointCharacteristic]()

    with self.dut.bl4a.register_callback(_Module.LE_AUDIO) as dut_lea_cb:
      for i, ref in enumerate(self.refs):
        self.logger.info("[REF-%d] Setup LEA", i)
        ref.device.add_service(pacs.make_pacs())
        ref_ascs = ascs.AudioStreamControlService(
            ref.device,
            sink_ase_id=[_SINK_ASE_ID],
            source_ase_id=[_SOURCE_ASE_ID],
        )
        ref_vcs = vcs.VolumeControlService(volume_setting=vcs.MAX_VOLUME // 2)
        ref.device.add_service(ref_ascs)
        ref.device.add_service(ref_vcs)
        ref.device.add_service(
            gmap.GamingAudioService(
                gmap_role=gmap.GmapRole.UNICAST_GAME_TERMINAL,
                ugt_features=(
                    gmap.UgtFeatures.UGT_SOURCE | gmap.UgtFeatures.UGT_SINK
                ),
            )
        )

        sink_ase.append(ref_ascs.ase_state_machines[_SINK_ASE_ID])

        self.logger.info("[DUT] Connect and pair REF-%d.", i)
        await self.le_connect_and_pair(
            ref_address_type=hci.OwnAddressType.RANDOM,
            ref=ref,
            connect_profiles=True,
        )

        self.logger.info("[DUT] Wait for LE Audio connected")
        await dut_lea_cb.wait_for_event(
            bl4a_api.ProfileConnectionStateChanged(
                address=ref.random_address,
                state=android_constants.ConnectionState.CONNECTED,
            ),
        )

        self.logger.info("[DUT] Wait for audio route ready")
        await dut_lea_cb.wait_for_event(
            bl4a_api.ProfileActiveDeviceChanged(ref.random_address)
        )

    self.logger.info("[DUT] Wait 1s to parse media context.")
    await asyncio.sleep(1.0)

    self.logger.info("[DUT] Set repeat mode to one.")
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)

    self.logger.info("[DUT] Start playing music.")
    self.dut.bt.audioPlaySine()

    # The default route should be REF-1.
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF-1] Wait for audio to start",
    ):
      await _wait_for_ase_state(
          sink_ase[1], ascs.AudioStreamEndpointCharacteristic.State.STREAMING
      )

    self.logger.info("[DUT] Wait 0.5s for ase to enter streaming.")
    await asyncio.sleep(0.5)

    for i, ref in enumerate(self.refs):
      with self.dut.bl4a.register_callback(_Module.LE_AUDIO) as dut_lea_cb:
        self.logger.info("[DUT] Switch audio route to REF-%d", i)
        await asyncio.to_thread(
            self.dut.bt.setActiveDevice,
            ref.random_address,
            android_constants.ActiveDeviceUse.ALL,
        )

        self.logger.info("[DUT] Wait for LEA set active on REF-%d", i)
        await dut_lea_cb.wait_for_event(
            bl4a_api.ProfileActiveDeviceChanged(ref.random_address)
        )

      self.logger.info("[DUT] Wait for LEA playing on REF-%d.", i)
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg=f"[REF-{i}] Wait for audio to start",
      ):
        await _wait_for_ase_state(
            sink_ase[i], ascs.AudioStreamEndpointCharacteristic.State.STREAMING
        )


if __name__ == "__main__":
  test_runner.main()
