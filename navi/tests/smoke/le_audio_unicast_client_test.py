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

import asyncio
import contextlib
import decimal
import struct
from typing import TypeAlias, cast

from bumble import core
from bumble import device
from bumble import hci
from bumble.profiles import ascs
from bumble.profiles import bap
from bumble.profiles import le_audio
from bumble.profiles import mcp
from bumble.profiles import pacs
from bumble.profiles import vcs
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.utils import resources
from navi.bumble_ext import ccp
from navi.bumble_ext import gatt_helper
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import pyee_extensions

_PROPERTY_UNICAST_CLIENT_ENABLED = (
    "bluetooth.profile.bap.unicast.client.enabled"
)
_PROPERTY_VCP_ENABLED = "bluetooth.profile.vcp.controller.enabled"
_PROPERTY_MCP_ENABLED = "bluetooth.profile.mcp.server.enabled"
_PROPERTY_CCP_ENABLED = "bluetooth.profile.ccp.server.enabled"
_PROPERTY_BYPASS_ALLOW_LIST = "persist.bluetooth.leaudio.bypass_allow_list"
_PROPERTY_LEAUDIO_ALLOW_LIST = "persist.bluetooth.leaudio.allow_list"
_DEFAUILT_ADVERTISING_PARAMETERS = device.AdvertisingParameters(
    own_address_type=hci.OwnAddressType.RANDOM,
    primary_advertising_interval_min=20,
    primary_advertising_interval_max=20,
)
_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_DEFAULT_RETRY_COUNT = 2
_STREAMING_TIME_SECONDS = 1.0
_PREPARE_TIME_SECONDS = 0.5
_CALLER_NAME = "Pixel Bluetooth"
_CALLER_NUMBER = "123456789"
_SAMPLE_AUDIO_FILE_PATH = "navi/tests/smoke/data/bach_cello.mp3"
_SAMPLE_AUDIO_FILE_RESOURCE_PATH = resources.GetResourceFilename(
    _SAMPLE_AUDIO_FILE_PATH
)
_SAMPLE_AUDIO_FILE_DEVICE_PATH = "/storage/self/primary/Music/sample.mp3"
_SINK_ASE_ID = 1
_SOURCE_ASE_ID = 2

_ConnectionState = android_constants.ConnectionState
_Direction = constants.Direction
_TestRole = constants.TestRole
_StreamType = android_constants.StreamType
_McpOpcode: TypeAlias = mcp.MediaControlPointOpcode
_CallState: TypeAlias = android_constants.CallState


async def _wait_for_ase_state(
    ase: ascs.AseStateMachine, state: ascs.AseStateMachine.State
) -> None:
  """Waits for the ASE state to be changed to the specified state."""
  with pyee_extensions.EventTriggeredValueObserver(
      ase,
      event="state_change",
      value_producer=lambda: ase.state,
  ) as observer:
    await observer.wait_for_target_value(state)


class LeAudioUnicastClientTest(navi_test_base.TwoDevicesTestBase):
  """Tests for LE Audio Unicast client.

  When running this test, please make sure the ref device supports CIS
  Peripheral.

  Supported devices are:
  - Pixel 8 and later
  - Pixel 8a and later
  - Pixel Watch 3 and later

  Unsupported devices are:
  - Pixel 7 and earlier
  - Pixel 7a and earlier
  - Pixel Watch 1, 2, Fitbit Ace LTE (P11)
  """

  ref_ascs: ascs.AudioStreamControlService
  dut_vcp_enabled: bool
  dut_mcp_enabled: bool
  dut_ccp_enabled: bool

  @classmethod
  def _default_pacs(cls) -> pacs.PublishedAudioCapabilitiesService:
    return pacs.PublishedAudioCapabilitiesService(
        supported_source_context=bap.ContextType(0xFFFF),
        available_source_context=bap.ContextType(0xFFFF),
        supported_sink_context=bap.ContextType(0xFFFF),
        available_sink_context=bap.ContextType(0xFFFF),
        sink_audio_locations=(
            bap.AudioLocation.FRONT_LEFT | bap.AudioLocation.FRONT_RIGHT
        ),
        source_audio_locations=(bap.AudioLocation.FRONT_LEFT),
        sink_pac=[
            pacs.PacRecord(
                coding_format=hci.CodingFormat(hci.CodecID.LC3),
                codec_specific_capabilities=bap.CodecSpecificCapabilities(
                    supported_sampling_frequencies=(
                        bap.SupportedSamplingFrequency.FREQ_16000
                        | bap.SupportedSamplingFrequency.FREQ_32000
                        | bap.SupportedSamplingFrequency.FREQ_48000
                    ),
                    supported_frame_durations=(
                        bap.SupportedFrameDuration.DURATION_10000_US_SUPPORTED
                    ),
                    supported_audio_channel_count=[1, 2],
                    min_octets_per_codec_frame=26,
                    max_octets_per_codec_frame=240,
                    supported_max_codec_frames_per_sdu=2,
                ),
            )
        ],
        source_pac=[
            pacs.PacRecord(
                coding_format=hci.CodingFormat(hci.CodecID.LC3),
                codec_specific_capabilities=bap.CodecSpecificCapabilities(
                    supported_sampling_frequencies=(
                        bap.SupportedSamplingFrequency.FREQ_16000
                        | bap.SupportedSamplingFrequency.FREQ_32000
                    ),
                    supported_frame_durations=(
                        bap.SupportedFrameDuration.DURATION_10000_US_SUPPORTED
                    ),
                    supported_audio_channel_count=[1],
                    min_octets_per_codec_frame=13,
                    max_octets_per_codec_frame=120,
                    supported_max_codec_frames_per_sdu=1,
                ),
            )
        ],
    )

  def _setup_unicast_server(self) -> None:
    self.ref.device.add_service(self._default_pacs())
    self.ref_ascs = ascs.AudioStreamControlService(
        self.ref.device,
        sink_ase_id=[_SINK_ASE_ID],
        source_ase_id=[_SOURCE_ASE_ID],
    )
    self.ref_vcs = vcs.VolumeControlService(volume_setting=vcp.MAX_VOLUME // 2)
    self.ref.device.add_service(self.ref_ascs)
    self.ref.device.add_service(self.ref_vcs)

  async def _prepare_paired_devices(self) -> None:
    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO) as dut_lea_cb,
        self.dut.bl4a.register_callback(bl4a_api.Module.AUDIO) as dut_audio_cb,
    ):
      self.logger.info("[DUT] Pair with REF")
      await self.le_connect_and_pair(ref_address_type=hci.OwnAddressType.RANDOM)

      self.logger.info("[DUT] Wait for LE Audio connected")
      event = await dut_lea_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: e.state == _ConnectionState.CONNECTED,
      )
      self.assertEqual(event.address, self.ref.random_address)
      self.logger.info("[DUT] Wait for audio route ready")
      await dut_audio_cb.wait_for_event(
          bl4a_api.AudioDeviceAdded,
          lambda e: e.device_type
          == android_constants.AudioDeviceType.BLE_HEADSET,
      )

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.getprop(_PROPERTY_UNICAST_CLIENT_ENABLED) != "true":
      raise signals.TestAbortClass("Unicast client is not enabled")

    if self.dut.getprop(_PROPERTY_BYPASS_ALLOW_LIST) != "true":
      # Allow list will not be used in the test, but here we still check if the
      # allow list is empty to make sure DUT is ready to use LE Audio.
      allowlist = [
          model
          for model in self.dut.getprop(_PROPERTY_LEAUDIO_ALLOW_LIST).split(",")
          # Filter empty string.
          if model
      ]
      if not allowlist:
        raise signals.TestAbortClass(
            "Allow list is empty, DUT is probably not ready to use LE Audio."
        )

    self.ref.config.cis_enabled = True
    self.ref.device.cis_enabled = True
    self.dut_vcp_enabled = self.dut.getprop(_PROPERTY_VCP_ENABLED) == "true"
    self.dut_mcp_enabled = self.dut.getprop(_PROPERTY_MCP_ENABLED) == "true"
    self.dut_ccp_enabled = self.dut.getprop(_PROPERTY_CCP_ENABLED) == "true"

    self.dut.adb.push([
        _SAMPLE_AUDIO_FILE_RESOURCE_PATH,
        f"/data/media/{self.dut.adb.current_user_id}/Music/sample.mp3",
    ])

  @override
  async def async_setup_test(self) -> None:
    # Disable the allow list to allow the connect LE Audio to Bumble.
    self.dut.setprop(_PROPERTY_BYPASS_ALLOW_LIST, "true")
    await super().async_setup_test()
    self._setup_unicast_server()
    await self._prepare_paired_devices()

  @override
  async def async_teardown_test(self) -> None:
    await super().async_teardown_test()
    # Make sure audio is stopped before starting the test.
    await asyncio.to_thread(self.dut.bt.audioStop)
    # Reset to the default value.
    self.dut.bt.setHandleAudioBecomingNoisy(False)

  @navi_test_base.named_parameterized(
      ("active", True),
      ("passive", False),
  )
  @navi_test_base.retry(_DEFAULT_RETRY_COUNT)
  async def test_reconnect(self, is_active: bool) -> None:
    """Tests to reconnect the LE Audio Unicast server.

    Args:
      is_active: True if reconnect is actively initialized by DUT, otherwise TA
        will be used to perform the reconnection passively.
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO) as dut_cb:
      self.logger.info("[DUT] Disconnect REF")
      self.dut.bt.disconnect(self.ref.random_address)

      self.logger.info("[DUT] Wait for LE Audio disconnected")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: e.state == _ConnectionState.DISCONNECTED,
      )

      self.logger.info("[REF] Start advertising")
      await self.ref.device.create_advertising_set(
          advertising_parameters=_DEFAUILT_ADVERTISING_PARAMETERS,
          advertising_data=bytes(
              bap.UnicastServerAdvertisingData(
                  announcement_type=bap.AnnouncementType.GENERAL
                  if is_active
                  else bap.AnnouncementType.TARGETED,
              )
          ),
      )
      if is_active:
        self.logger.info("[DUT] Reconnect REF")
        self.dut.bt.connect(self.ref.random_address)

      self.logger.info("[DUT] Wait for LE Audio connected")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: e.state == _ConnectionState.CONNECTED,
      )

  async def test_unidirectional_audio_stream(self) -> None:
    """Tests unidirectional audio stream between DUT and REF.

    Test steps:
      1. [Optional] Wait for audio streaming to stop if it is already streaming.
      2. Start audio streaming from DUT.
      3. Wait for audio streaming to start from REF.
      4. Stop audio streaming from DUT.
      5. Wait for audio streaming to stop from REF.
    """
    sink_ase = self.ref_ascs.ase_state_machines[_SINK_ASE_ID]

    # Make sure audio is not streaming.
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to stop",
    ):
      await _wait_for_ase_state(sink_ase, ascs.AseStateMachine.State.IDLE)

    self.logger.info("[DUT] Start audio streaming")
    await asyncio.to_thread(self.dut.bt.audioPlaySine)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to start",
    ):
      await _wait_for_ase_state(sink_ase, ascs.AseStateMachine.State.STREAMING)

    # Streaming for 1 second.
    await asyncio.sleep(_STREAMING_TIME_SECONDS)

    self.logger.info("[DUT] Stop audio streaming")
    await asyncio.to_thread(self.dut.bt.audioStop)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to stop",
    ):
      await _wait_for_ase_state(sink_ase, ascs.AseStateMachine.State.IDLE)

  async def test_bidirectional_audio_stream(self) -> None:
    """Tests bidirectional audio stream between DUT and REF.

    Test steps:
      1. [Optional] Wait for audio streaming to stop if it is already streaming.
      2. Put a call on DUT to make conversational audio context.
      3. Start audio streaming from DUT.
      4. Wait for audio streaming to start from REF.
      5. Stop audio streaming from DUT.
      6. Wait for audio streaming to stop from REF.
    """
    dut_telecom_cb = self.dut.bl4a.register_callback(bl4a_api.Module.TELECOM)
    self.close_after_test.append(dut_telecom_cb)
    call = self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.OUTGOING,
    )

    with call:
      await dut_telecom_cb.wait_for_event(
          bl4a_api.CallStateChanged,
          lambda e: (e.state in (_CallState.CONNECTING, _CallState.DIALING)),
      )

      # Make sure audio is not streaming.
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Wait for audio to stop",
      ):
        for ase in self.ref_ascs.ase_state_machines.values():
          await _wait_for_ase_state(ase, ascs.AseStateMachine.State.IDLE)

      self.logger.info("[DUT] Start audio streaming")
      await asyncio.to_thread(self.dut.bt.audioPlaySine)

      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Wait for audio to start",
      ):
        # With current configuration, all ASEs will be active in bidirectional
        # streaming.
        for ase in self.ref_ascs.ase_state_machines.values():
          await _wait_for_ase_state(ase, ascs.AseStateMachine.State.STREAMING)

      # Streaming for 1 second.
      await asyncio.sleep(_STREAMING_TIME_SECONDS)

      self.logger.info("[DUT] Stop audio streaming")
      await asyncio.to_thread(self.dut.bt.audioStop)

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to stop",
    ):
      for ase in self.ref_ascs.ase_state_machines.values():
        await _wait_for_ase_state(ase, ascs.AseStateMachine.State.IDLE)

  async def test_reconnect_during_call(self) -> None:
    """Tests reconnecting during a call. Call audio should be routed to Unicast.

    Test steps:
      1. Disconnect REF.
      2. Put a call on DUT.
      3. Reconnect REF.
      4. Wait for audio streaming to start from REF.
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO) as dut_cb:
      self.logger.info("[DUT] Disconnect REF")
      self.dut.bt.disconnect(self.ref.random_address)

      self.logger.info("[DUT] Wait for LE Audio disconnected")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: e.state == _ConnectionState.DISCONNECTED,
      )

    with contextlib.ExitStack() as stack:
      dut_telecom_cb = self.dut.bl4a.register_callback(bl4a_api.Module.TELECOM)
      stack.enter_context(dut_telecom_cb)
      self.logger.info("[DUT] Put a call")
      call = self.dut.bl4a.make_phone_call(
          _CALLER_NAME,
          _CALLER_NUMBER,
          constants.Direction.OUTGOING,
      )
      stack.enter_context(call)
      await dut_telecom_cb.wait_for_event(
          bl4a_api.CallStateChanged,
          lambda e: (e.state in (_CallState.CONNECTING, _CallState.DIALING)),
      )
      # Start audio streaming from DUT.
      self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)
      self.dut.bt.audioPlaySine()

      dut_leaudio_cb = self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO)
      stack.enter_context(dut_leaudio_cb)

      self.logger.info("[REF] Start advertising")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await self.ref.device.create_advertising_set(
            advertising_parameters=_DEFAUILT_ADVERTISING_PARAMETERS,
            advertising_data=bytes(
                bap.UnicastServerAdvertisingData(
                    announcement_type=bap.AnnouncementType.TARGETED
                )
            ),
        )
      self.logger.info("[DUT] Wait for LE Audio connected")
      await dut_leaudio_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: e.state == _ConnectionState.CONNECTED,
      )

      self.logger.info("[REF] Wait for streaming to start")
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Wait for audio to start",
      ):
        for ase in self.ref_ascs.ase_state_machines.values():
          await _wait_for_ase_state(ase, ascs.AseStateMachine.State.STREAMING)

  async def test_reconfiguration(self) -> None:
    """Tests reconfiguration from media to conversational.

    Test steps:
      1. [Optional] Wait for audio streaming to stop if it is already streaming.
      2. Start audio streaming from DUT.
      3. Wait for audio streaming to start from REF.
      4. Put a call on DUT to trigger reconfiguration.
      5. Wait for ASE to be reconfigured.
    """
    sink_ase = self.ref_ascs.ase_state_machines[_SINK_ASE_ID]

    # Make sure audio is not streaming.
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to stop",
    ):
      await _wait_for_ase_state(sink_ase, ascs.AseStateMachine.State.IDLE)

    self.logger.info("[DUT] Start audio streaming")
    await asyncio.to_thread(self.dut.bt.audioPlaySine)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to start",
    ):
      await _wait_for_ase_state(sink_ase, ascs.AseStateMachine.State.STREAMING)
    self.assertIsInstance(sink_ase.metadata, le_audio.Metadata)
    get_audio_context = lambda: next(
        entry
        for entry in sink_ase.metadata.entries
        if entry.tag == le_audio.Metadata.Tag.STREAMING_AUDIO_CONTEXTS
    )
    context_type = struct.unpack_from("<H", get_audio_context().data)[0]
    self.assertNotEqual(context_type, bap.ContextType.PROHIBITED)
    self.assertFalse(context_type & bap.ContextType.CONVERSATIONAL)

    # Streaming for 1 second.
    await asyncio.sleep(_STREAMING_TIME_SECONDS)

    call = self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.OUTGOING,
    )
    with call:
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[DUT] Wait for ASE to be released",
      ):
        await _wait_for_ase_state(sink_ase, ascs.AseStateMachine.State.IDLE)
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[DUT] Wait for ASE to be reconfigured",
      ):
        await _wait_for_ase_state(
            sink_ase, ascs.AseStateMachine.State.STREAMING
        )
      context_type = struct.unpack_from("<H", get_audio_context().data)[0]
      self.assertTrue(context_type & bap.ContextType.CONVERSATIONAL)

  async def test_volume_initialization(self) -> None:
    """Makes sure DUT sets the volume correctly after connecting to REF."""
    if not self.dut_vcp_enabled:
      self.skipTest("VCP is not enabled on DUT")

    # When the flag is enabled, DUT's volume will be applied to REF.
    if self.dut.bluetooth_flags.get("vcp_device_volume_api_improvements", True):
      vcs_volume = pyee_extensions.EventTriggeredValueObserver[int](
          self.ref_vcs, "volume_state", lambda: self.ref_vcs.volume_setting
      )
      ref_expected_volume = decimal.Decimal(
          self.dut.bt.getVolume(_StreamType.MUSIC)
          / self.dut.bt.getMaxVolume(_StreamType.MUSIC)
          * vcs.MAX_VOLUME
      ).to_integral_exact(rounding=decimal.ROUND_HALF_UP)
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          "[REF] Wait for volume to be synced with DUT",
      ):
        await vcs_volume.wait_for_target_value(int(ref_expected_volume))
    else:
      dut_expected_volume = decimal.Decimal(
          self.ref_vcs.volume_setting
          / vcs.MAX_VOLUME
          * self.dut.bt.getMaxVolume(_StreamType.MUSIC)
      ).to_integral_exact(rounding=decimal.ROUND_HALF_UP)
      with (
          self.dut.bl4a.register_callback(
              bl4a_api.Module.AUDIO
          ) as dut_audio_cb,
      ):
        if self.dut.bt.getVolume(_StreamType.MUSIC) != dut_expected_volume:
          self.logger.info("[DUT] Wait for volume to be synced with REF")
          event = await dut_audio_cb.wait_for_event(
              callback_type=bl4a_api.VolumeChanged,
              predicate=lambda e: e.stream_type == _StreamType.MUSIC,
          )
          self.assertEqual(event.volume_value, dut_expected_volume)

  @navi_test_base.parameterized(_TestRole.DUT, _TestRole.REF)
  async def test_set_volume(self, issuer: _TestRole) -> None:
    """Tests setting volume over LEA VCP from DUT or REF.

    Test steps:
      1. Set volume from DUT or REF.
      2. Wait for the volume to be set correctly on the other device.

    Args:
      issuer: The issuer of the volume setting.
    """
    if not self.dut_vcp_enabled:
      self.skipTest("VCP is not enabled on DUT")

    dut_max_volume = self.dut.bt.getMaxVolume(_StreamType.MUSIC)
    dut_expected_volume = (self.dut.bt.getVolume(_StreamType.MUSIC) + 1) % (
        dut_max_volume + 1
    )
    ref_expected_volume = int(
        decimal.Decimal(
            dut_expected_volume / dut_max_volume * vcs.MAX_VOLUME
        ).to_integral_exact(rounding=decimal.ROUND_HALF_UP)
    )

    # DUT's VCS client might not be stable at the beginning. If we set volume
    # immediately, the volume might not be set correctly.
    await asyncio.sleep(_PREPARE_TIME_SECONDS)

    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.AUDIO) as dut_audio_cb,
    ):
      vcs_volume = pyee_extensions.EventTriggeredValueObserver[int](
          self.ref_vcs, "volume_state", lambda: self.ref_vcs.volume_setting
      )
      if issuer == _TestRole.DUT:
        self.logger.info("[DUT] Set volume to %d", dut_expected_volume)
        self.dut.bt.setVolume(_StreamType.MUSIC, dut_expected_volume)
        async with self.assert_not_timeout(
            _DEFAULT_STEP_TIMEOUT_SECONDS,
            msg="[REF] Wait for volume to be set",
        ):
          await vcs_volume.wait_for_target_value(ref_expected_volume)
      else:
        self.logger.info("[REF] Set volume to %d", ref_expected_volume)
        self.ref_vcs.volume_setting = ref_expected_volume
        await self.ref.device.notify_subscribers(
            self.ref_vcs.volume_state, self.ref_vcs.volume_state_bytes
        )
        await dut_audio_cb.wait_for_event(
            callback_type=bl4a_api.VolumeChanged,
            predicate=lambda e: (
                e.stream_type == _StreamType.MUSIC
                and e.volume_value == dut_expected_volume
            ),
        )

  async def test_mcp_play_pause(self) -> None:
    """Tests starting and stopping audio streaming over MCP.

    Test steps:
      1. Connect MCP.
      2. Subscribe MCP characteristics.
      3. Play audio streaming over MCP.
      4. Pause audio streaming over MCP.
    """
    if not self.dut_mcp_enabled:
      self.skipTest("MCP is not enabled on DUT")

    self.logger.info("[REF] Connect MCP")
    ref_dut_acl = list(self.ref.device.connections.values())[0]
    async with device.Peer(ref_dut_acl) as peer:
      ref_mcp_client = peer.create_service_proxy(
          mcp.GenericMediaControlServiceProxy
      )
      if not ref_mcp_client:
        self.fail("Failed to connect MCP")

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Subscribe MCP characteristics",
    ):
      await ref_mcp_client.subscribe_characteristics()
    assert ref_mcp_client.media_state
    media_state = await gatt_helper.MutableCharacteristicState.create(
        ref_mcp_client.media_state
    )

    # Make sure player is active but not streaming.
    await asyncio.to_thread(
        lambda: self.dut.bt.audioPlayFile(_SAMPLE_AUDIO_FILE_DEVICE_PATH)
    )
    await asyncio.to_thread(self.dut.bt.audioPause)
    await asyncio.sleep(_PREPARE_TIME_SECONDS)

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Play",
    ):
      self.assertEqual(
          await ref_mcp_client.write_control_point(_McpOpcode.PLAY),
          mcp.MediaControlPointResultCode.SUCCESS,
      )
      self.logger.info("[REF] Wait for media state to be PLAY")
      await media_state.wait_for_target_value(bytes([mcp.MediaState.PLAYING]))

    # Streaming for 1 second.
    await asyncio.sleep(_STREAMING_TIME_SECONDS)

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Pause",
    ):
      self.assertEqual(
          await ref_mcp_client.write_control_point(_McpOpcode.PAUSE),
          mcp.MediaControlPointResultCode.SUCCESS,
      )
      self.logger.info("[REF] Wait for media state to be PAUSED")
      await media_state.wait_for_target_value(bytes([mcp.MediaState.PAUSED]))

  async def test_mcp_previous_next_track(self) -> None:
    """Tests moving to previous and next track over MCP.

    Test steps:
      1. Connect MCP.
      2. Subscribe MCP characteristics.
      3. Move to next track over MCP.
      4. Move to previous track over MCP.
    """
    if not self.dut_mcp_enabled:
      self.skipTest("MCP is not enabled on DUT")

    self.logger.info("[REF] Connect MCP")
    ref_dut_acl = list(self.ref.device.connections.values())[0]
    async with device.Peer(ref_dut_acl) as peer:
      ref_mcp_client = peer.create_service_proxy(
          mcp.GenericMediaControlServiceProxy
      )
      if not ref_mcp_client:
        self.fail("Failed to connect MCP")

    # Make sure there are at least two tracks.
    await asyncio.to_thread(
        lambda: self.dut.bt.audioPlayFile(_SAMPLE_AUDIO_FILE_DEVICE_PATH)
    )
    self.dut.bt.addMediaItem(_SAMPLE_AUDIO_FILE_DEVICE_PATH)
    await asyncio.sleep(_PREPARE_TIME_SECONDS)

    watcher = pyee_extensions.EventWatcher()
    track_changed = watcher.async_monitor(ref_mcp_client, "track_changed")

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Subscribe MCP characteristics",
    ):
      await ref_mcp_client.subscribe_characteristics()

    await asyncio.sleep(_PREPARE_TIME_SECONDS)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Move to next track",
    ):
      self.assertEqual(
          await ref_mcp_client.write_control_point(_McpOpcode.NEXT_TRACK),
          mcp.MediaControlPointResultCode.SUCCESS,
      )
      self.logger.info("[REF] Wait for track changed")
      await track_changed.get()

    # Clear the track changed events.
    while not track_changed.empty():
      track_changed.get_nowait()

    await asyncio.sleep(_PREPARE_TIME_SECONDS)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Move to previous track",
    ):
      self.assertEqual(
          await ref_mcp_client.write_control_point(_McpOpcode.PREVIOUS_TRACK),
          mcp.MediaControlPointResultCode.SUCCESS,
      )
      self.logger.info("[REF] Wait for track changed")
      await track_changed.get()

  async def test_mcp_fast_rewind_fast_forward(self) -> None:
    """Tests moving to previous and next track over MCP.

    Test steps:
      1. Connect MCP.
      2. Subscribe MCP characteristics.
      3. Fast forward over MCP.
      4. Fast rewind over MCP.
    """
    if not self.dut_mcp_enabled:
      self.skipTest("MCP is not enabled on DUT")

    self.logger.info("[REF] Connect MCP")
    ref_dut_acl = list(self.ref.device.connections.values())[0]
    async with device.Peer(ref_dut_acl) as peer:
      ref_mcp_client = peer.create_service_proxy(
          mcp.GenericMediaControlServiceProxy
      )
      if not ref_mcp_client:
        self.fail("Failed to connect MCP")

    await asyncio.to_thread(
        lambda: self.dut.bt.audioPlayFile(_SAMPLE_AUDIO_FILE_DEVICE_PATH)
    )

    watcher = pyee_extensions.EventWatcher()
    track_position = watcher.async_monitor(ref_mcp_client, "track_position")

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Subscribe MCP characteristics",
    ):
      await ref_mcp_client.subscribe_characteristics()

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Fast forward",
    ):
      self.assertEqual(
          await ref_mcp_client.write_control_point(_McpOpcode.FAST_FORWARD),
          mcp.MediaControlPointResultCode.SUCCESS,
      )
      self.logger.info("[REF] Wait for track position changed")
      await track_position.get()

    # Clear the track changed events.
    while not track_position.empty():
      track_position.get_nowait()

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Fast rewind",
    ):
      self.assertEqual(
          await ref_mcp_client.write_control_point(_McpOpcode.FAST_REWIND),
          mcp.MediaControlPointResultCode.SUCCESS,
      )
      self.logger.info("[REF] Wait for track position changed")
      await track_position.get()

  @navi_test_base.parameterized(_Direction.INCOMING, _Direction.OUTGOING)
  async def test_ccp_call_notifications(self, direction: _Direction) -> None:
    """Tests receiving call notifications over CCP.

    Test steps:
      1. Connect CCP.
      2. Read and subscribe CCP characteristics.
      3. Put a call from DUT, check the call info on REF.
      4. Answer the call on REF, check the call info on DUT.
      5. Terminate the call on REF, check the call info on DUT.

    Args:
      direction: The direction of the call.
    """
    if not self.dut_ccp_enabled:
      self.skipTest("CCP is not enabled on DUT")

    self.logger.info("[REF] Connect TBS")
    ref_dut_acl = list(self.ref.device.connections.values())[0]
    async with device.Peer(ref_dut_acl) as peer:
      ref_tbs_client = peer.create_service_proxy(
          ccp.GenericTelephoneBearerServiceProxy
      )
      if not ref_tbs_client:
        self.fail("Failed to connect TBS")

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Read and subscribe TBS characteristics",
    ):
      await ref_tbs_client.read_and_subscribe_characteristics()

    expected_call_uri = f"tel:{_CALLER_NUMBER}"
    with self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        direction,
    ) as call:
      if ref_tbs_client.call_friendly_name:
        async with self.assert_not_timeout(
            _DEFAULT_STEP_TIMEOUT_SECONDS,
            msg="[REF] Wait for call friendly name",
        ):
          await ref_tbs_client.call_friendly_name.wait_for_target_value(
              bytes([1]) + _CALLER_NAME.encode()
          )
      if direction == _Direction.INCOMING:
        async with self.assert_not_timeout(
            _DEFAULT_STEP_TIMEOUT_SECONDS,
            msg="[REF] Wait for incoming call information",
        ):
          await ref_tbs_client.incoming_call.wait_for_target_value(
              bytes([1]) + expected_call_uri.encode()
          )
        expected_call_states = (ccp.CallState.INCOMING,)
        expected_call_flag = ccp.CallFlag(0)
      else:
        expected_call_states = (ccp.CallState.DIALING, ccp.CallState.ALERTING)
        expected_call_flag = ccp.CallFlag.IS_OUTGOING

      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Wait for call state change",
      ):
        await ref_tbs_client.call_state.wait_for_target_value(
            lambda value: (
                len(value) >= 3
                and value[0] == 1
                and value[1] in expected_call_states
                and value[2] == expected_call_flag
            )
        )
        self.logger.info("[REF] Wait for call info change")
        await ref_tbs_client.bearer_list_current_calls.wait_for_target_value(
            lambda value: (
                (info_list := ccp.CallInfo.parse_list(value))
                and (info_list[0].call_index == 1)
                and (info_list[0].call_state in expected_call_states)
                and (info_list[0].call_flags == expected_call_flag)
                and (info_list[0].call_uri == expected_call_uri)
            )
        )

      self.logger.info("[DUT] Answer / Activate call")
      call.answer()
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Wait for call state to be active",
      ):
        await ref_tbs_client.call_state.wait_for_target_value(
            bytes([1, ccp.CallState.ACTIVE, expected_call_flag])
        )
        self.logger.info("[REF] Wait for call info change")
        await ref_tbs_client.bearer_list_current_calls.wait_for_target_value(
            ccp.CallInfo(
                call_index=1,
                call_state=ccp.CallState.ACTIVE,
                call_flags=expected_call_flag,
                call_uri=expected_call_uri,
            ).to_bytes()
        )

    self.logger.info("[DUT] Terminate call")
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for call info removed",
    ):
      await ref_tbs_client.call_state.wait_for_target_value(b"")
      self.logger.info("[REF] Wait for call info change")
      await ref_tbs_client.bearer_list_current_calls.wait_for_target_value(b"")

  async def test_ccp_accept_and_terminate_call(self) -> None:
    """Tests answering and terminating a call over CCP.

    Test steps:
      1. Connect CCP.
      2. Read and subscribe CCP characteristics.
      3. Put an incoming call from DUT.
      4. Accept the call on REF.
      5. Terminate the call on REF.
    """
    if not self.dut_ccp_enabled:
      self.skipTest("CCP is not enabled on DUT")

    self.logger.info("[REF] Connect TBS")
    ref_dut_acl = list(self.ref.device.connections.values())[0]
    async with device.Peer(ref_dut_acl) as peer:
      ref_tbs_client = peer.create_service_proxy(
          ccp.GenericTelephoneBearerServiceProxy
      )
      if not ref_tbs_client:
        self.fail("Failed to connect TBS")

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Read and subscribe TBS characteristics",
    ):
      await ref_tbs_client.read_and_subscribe_characteristics()

    expected_call_index = 1
    with (
        self.dut.bl4a.make_phone_call(
            _CALLER_NAME,
            _CALLER_NUMBER,
            _Direction.INCOMING,
        ),
        self.dut.bl4a.register_callback(
            bl4a_api.Module.TELECOM
        ) as dut_telecom_cb,
    ):
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Wait for call state change",
      ):
        await ref_tbs_client.call_state.wait_for_target_value(
            bytes(
                [expected_call_index, ccp.CallState.INCOMING, ccp.CallFlag(0)]
            )
        )

      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Accept call",
      ):
        await ref_tbs_client.accept(expected_call_index)

      self.logger.info("[DUT] Wait for call to be active")
      await dut_telecom_cb.wait_for_event(
          callback_type=bl4a_api.CallStateChanged,
          predicate=lambda e: e.state == _CallState.ACTIVE,
      )

      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Terminate call",
      ):
        await ref_tbs_client.terminate(expected_call_index)

      self.logger.info("[DUT] Wait for call to be disconnected")
      await dut_telecom_cb.wait_for_event(
          callback_type=bl4a_api.CallStateChanged,
          predicate=lambda e: (e.state == _CallState.DISCONNECTED),
      )

  async def test_noisy_handling(self) -> None:
    """Tests enabling noisy handling, and verify the player is paused after REF disconnected.

    Test steps:
      1. Enable noisy handling.
      2. Start streaming.
      3. Disconnect from REF.
      4. Wait for player paused.
    """
    # Enable audio noisy handling.
    self.dut.bt.setHandleAudioBecomingNoisy(True)

    sink_ase = self.ref_ascs.ase_state_machines[_SINK_ASE_ID]

    # Make sure audio is not streaming.
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for ASE state to be idle",
    ):
      await _wait_for_ase_state(sink_ase, ascs.AseStateMachine.State.IDLE)

    self.logger.info("[DUT] Start audio streaming")
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ALL)
    await asyncio.to_thread(self.dut.bt.audioPlaySine)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for ASE state to be streaming",
    ):
      await _wait_for_ase_state(sink_ase, ascs.AseStateMachine.State.STREAMING)

    # Streaming for 1 second.
    await asyncio.sleep(_STREAMING_TIME_SECONDS)

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.PLAYER
    ) as dut_player_cb:
      ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
          hci.Address(self.dut.address), transport=core.BT_LE_TRANSPORT
      )
      if ref_dut_acl is None:
        self.fail("No ACL connection found?")
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Disconnect.",
      ):
        await ref_dut_acl.disconnect()

      self.logger.info("[DUT] Wait for player paused.")
      await dut_player_cb.wait_for_event(
          bl4a_api.PlayerIsPlayingChanged,
          lambda e: (not (cast(bl4a_api.PlayerIsPlayingChanged, e).is_playing)),
      )


if __name__ == "__main__":
  test_runner.main()
