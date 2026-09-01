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

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import contextlib
import decimal
import struct
import sys
import tempfile
from typing import TypeAlias
from unittest import mock
import wave

from bumble import core
from bumble import data_types
from bumble import device
from bumble import hci
from bumble.profiles import bap
from bumble.profiles import gmap
from bumble.profiles import le_audio
from bumble.profiles import mcp
from bumble.profiles import vcs
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import ascs
from navi.bumble_ext import ccp
from navi.bumble_ext import gatt_helper
from navi.bumble_ext import pacs
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import audio
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import lc3
from navi.utils import pyee_extensions


_DEFAUILT_ADVERTISING_PARAMETERS = device.AdvertisingParameters(
    own_address_type=hci.OwnAddressType.RANDOM,
    primary_advertising_interval_min=20,
    primary_advertising_interval_max=20,
)
_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_DEFAULT_RETRY_COUNT = 2
_STREAMING_TIME_SECONDS = 3.0
_PREPARE_TIME_SECONDS = 0.5
_CALLER_NAME = "Pixel Bluetooth"
_CALLER_NUMBER = "123456789"
_SINK_ASE_ID = 1
_SOURCE_ASE_ID = 2
_DEFAULT_FRAME_RATE = 48000
_MOCK_CONNECTION_SERVICE_CLASS = (
    f"{android_constants.PACKAGE_NAME_BLUETOOTH_SNIPPET}.MockConnectionService"
)
_MOCK_CONNECTION_SERVICE_ID = "mock_connection_service"
_RECORDING_PATH = "/storage/self/primary/Recordings/record.wav"
_GENERAL_DISCOVERABLE_AD_FLAGS = data_types.Flags(
    core.AdvertisingData.Flags.LE_GENERAL_DISCOVERABLE_MODE
)

_ConnectionState = android_constants.ConnectionState
_Direction = constants.Direction
_TestRole = constants.TestRole
_StreamType = android_constants.StreamType
_McpOpcode: TypeAlias = mcp.MediaControlPointOpcode
_CallState: TypeAlias = android_constants.CallState
_AndroidProperty = android_constants.Property


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


def decoder_for_ase(ase: ascs.AudioStreamEndpointCharacteristic) -> lc3.Decoder:
  """Returns the decoder for the ASE."""
  codec_config = ase.codec_specific_configuration
  assert isinstance(codec_config, bap.CodecSpecificConfiguration)
  assert codec_config.frame_duration is not None
  assert codec_config.sampling_frequency is not None
  assert codec_config.audio_channel_allocation is not None
  return lc3.Decoder(
      frame_duration_us=codec_config.frame_duration.us,
      sample_rate_hz=codec_config.sampling_frequency.hz,
      pcm_sample_rate_hz=_DEFAULT_FRAME_RATE,
      num_channels=codec_config.audio_channel_allocation.channel_count,
  )


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

  def _setup_unicast_server(self) -> None:
    self.ref.device.add_service(pacs.make_pacs())
    self.ref_ascs = ascs.AudioStreamControlService(
        self.ref.device,
        sink_ase_id=[_SINK_ASE_ID],
        source_ase_id=[_SOURCE_ASE_ID],
    )
    self.ref_vcs = vcs.VolumeControlService(volume_setting=vcs.MAX_VOLUME // 2)
    self.ref.device.add_service(self.ref_ascs)
    self.ref.device.add_service(self.ref_vcs)
    self.ref.device.add_service(
        gmap.GamingAudioService(
            gmap_role=gmap.GmapRole.UNICAST_GAME_TERMINAL,
            ugt_features=(
                gmap.UgtFeatures.UGT_SOURCE | gmap.UgtFeatures.UGT_SINK
            ),
        )
    )

  def _generate_and_push_wave_file(
      self, path_on_device: str, duration_seconds: int = 5
  ) -> None:
    with tempfile.NamedTemporaryFile(
        # On Windows, NamedTemporaryFile cannot be deleted if used multiple
        # times.
        delete=(sys.platform != "win32")
    ) as local_file:
      with wave.open(local_file.name, "wb") as wave_file:
        wave_file.setnchannels(1)
        wave_file.setsampwidth(2)
        wave_file.setframerate(48000)
        wave_file.writeframes(bytes(48000 * 2 * duration_seconds))
      self.dut.adb.push([local_file.name, path_on_device])

  def _setup_mock_connection_service(self) -> None:
    phone_account_handle = (
        android_constants.PACKAGE_NAME_BLUETOOTH_SNIPPET,
        _MOCK_CONNECTION_SERVICE_CLASS,
        _MOCK_CONNECTION_SERVICE_ID,
    )
    self.logger.info("[DUT] Register mock phone account.")
    self.dut.bt.registerPhoneAccount(
        *phone_account_handle, "Mock Connection Service"
    )

    self.logger.info("[DUT] Enable and set mock phone account as default.")
    self.dut.bt.setPhoneAccountEnabled(
        *phone_account_handle,
        True,
    )
    self.dut.bt.setUserSelectedOutgoingPhoneAccount(*phone_account_handle)

    def cleanup() -> None:
      self.logger.info("[DUT] Clean up mock phone account.")
      self.dut.bt.setUserSelectedOutgoingPhoneAccount()
      self.dut.bt.setPhoneAccountEnabled(
          *phone_account_handle,
          False,
      )
      self.dut.bt.unregisterPhoneAccount(*phone_account_handle)

    self.test_case_context.callback(cleanup)

  async def _prepare_paired_devices(self) -> None:
    with self.dut.bl4a.register_callback(
        bl4a_api.Module.LE_AUDIO
    ) as dut_lea_cb:
      self.logger.info("[DUT] Pair with REF")
      await self.le_connect_and_pair(
          ref_address_type=hci.OwnAddressType.RANDOM, connect_profiles=True
      )

      self.logger.info("[DUT] Wait for LE Audio connected")
      event = await dut_lea_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.random_address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )
      self.assertEqual(event.address, self.ref.random_address)
      self.logger.info("[DUT] Wait for audio route ready")
      await dut_lea_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(self.ref.random_address)
      )

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if not self.dut.is_le_audio_supported:
      raise signals.TestAbortClass("[DUT] Device does not support LE Audio.")

    self.ref.config.cis_enabled = True
    self.ref.device.cis_enabled = True
    self.dut_vcp_enabled = (
        self.dut.getprop(_AndroidProperty.VCP_CONTROLLER_ENABLED) == "true"
    )
    self.dut_mcp_enabled = (
        self.dut.getprop(_AndroidProperty.MCP_SERVER_ENABLED) == "true"
    )
    self.dut_ccp_enabled = (
        self.dut.getprop(_AndroidProperty.CCP_SERVER_ENABLED) == "true"
    )

    self.setprop_for_class_context(
        _AndroidProperty.LEAUDIO_BYPASS_ALLOW_LIST, "true"
    )

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()

    self._setup_unicast_server()
    # Reset audio attributes to media.
    self.dut.bl4a.set_audio_attributes(
        bl4a_api.AudioAttributes(usage=bl4a_api.AudioAttributes.Usage.MEDIA),
        handle_audio_focus=False,
    )
    await self._prepare_paired_devices()

  @override
  async def async_teardown_test(self) -> None:
    self.logger.info("[DUT] Disconnect all calls.")
    self.dut.bt.disconnectAllCalls()
    # Make sure audio is stopped before starting the test.
    await asyncio.to_thread(self.dut.bt.audioStop)
    # Reset to the default value.
    self.dut.bt.setHandleAudioBecomingNoisy(False)
    self.dut.bt.setAudioPlaybackOffload(False)
    await super().async_teardown_test()

  def _get_sampling_frequency(
      self, ase: ascs.AudioStreamEndpointCharacteristic
  ) -> bap.SamplingFrequency:
    """Returns the sampling frequency of the ASE."""
    if (
        isinstance(
            codec_config := ase.codec_specific_configuration,
            bap.CodecSpecificConfiguration,
        )
        and codec_config.sampling_frequency is not None
    ):
      return codec_config.sampling_frequency
    return bap.SamplingFrequency(0)

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
    if not is_active and self.dut.device.is_emulator:
      self.skipTest(
          "b/425668688 - TA filter reconnection is not supported on rootcanal"
          " yet."
      )

    with self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO) as dut_cb:
      await self.disconnect_with_check(
          self.ref.random_address, android_constants.Transport.LE
      )

      self.logger.info("[DUT] Wait for LE Audio disconnected")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=None),
      )

      self.logger.info("[REF] Start advertising")
      await self.ref.device.create_advertising_set(
          advertising_parameters=_DEFAUILT_ADVERTISING_PARAMETERS,
          advertising_data=bytes(
              core.AdvertisingData([
                  ascs.make_bap_announcement(
                      announcement_type=(
                          bap.AnnouncementType.GENERAL
                          if is_active
                          else bap.AnnouncementType.TARGETED
                      ),
                  ),
                  _GENERAL_DISCOVERABLE_AD_FLAGS,
              ])
          ),
      )
      if is_active:
        self.logger.info("[DUT] Reconnect REF")
        self.dut.bt.connect(self.ref.random_address)

      self.logger.info("[DUT] Wait for LE Audio connected")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.random_address),
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
      await _wait_for_ase_state(
          sink_ase, ascs.AudioStreamEndpointCharacteristic.State.IDLE
      )

    self.logger.info("[DUT] Start audio streaming")
    await asyncio.to_thread(self.dut.bt.audioPlaySine)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to start",
    ):
      await _wait_for_ase_state(
          sink_ase, ascs.AudioStreamEndpointCharacteristic.State.STREAMING
      )

    # Setup audio sink.
    sink_frames = list[bytes]()
    decoder = decoder_for_ase(sink_ase) if lc3.AVAILABLE else None

    def sink(pdu: hci.HCI_IsoDataPacket):
      if pdu.iso_sdu_fragment:
        sink_frames.append(pdu.iso_sdu_fragment)

    assert (cis_link := sink_ase.cis_link)
    cis_link.sink = sink

    # Streaming for 1 second.
    await asyncio.sleep(_STREAMING_TIME_SECONDS)

    self.logger.info("[DUT] Stop audio streaming")
    cis_link.sink = None
    await asyncio.to_thread(self.dut.bt.audioStop)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to stop",
    ):
      await _wait_for_ase_state(
          sink_ase, ascs.AudioStreamEndpointCharacteristic.State.IDLE
      )

    if self.user_params.get(navi_test_base.RECORD_FULL_DATA):
      self.write_test_output_data("sink.lc3", b"".join(sink_frames))
    if lc3.AVAILABLE and decoder and audio.SUPPORT_AUDIO_PROCESSING:
      pcm_format = lc3.PcmFormat.SIGNED_16
      decoded_frames = [
          decoder.decode(frame, pcm_format) for frame in sink_frames
      ]
      dominant_frequency = audio.get_dominant_frequency(
          buffer=b"".join(decoded_frames),
          format="pcm",
          sample_width=pcm_format.sample_width,
          frame_rate=_DEFAULT_FRAME_RATE,
          channels=decoder.num_channels,
      )
      self.logger.info("dominant_frequency: %.2f", dominant_frequency)
      self.assertAlmostEqual(dominant_frequency, 1000, delta=10)

  async def test_unidirectional_audio_stream_offloaded(self) -> None:
    """Tests unidirectional audio stream with offloaded playback between DUT and REF.

    Test steps:
      1. Set AudioPlaybackOffload to true.
      2. utilize test_unidirectional_audio_stream
    """
    self.dut.bt.setAudioPlaybackOffload(True)
    await self.test_unidirectional_audio_stream()

  async def test_gaming_context(self) -> None:
    """Tests streaming with gaming context.

    Test steps:
      1. Wait for BLE_HEADSET input audio device.
      2. [Optional] Wait for audio streaming to stop if it is already streaming.
      3. Start audio streaming from DUT with gaming context and put a call on
      DUT.
      4. Wait for audio streaming to start from REF.
      5. Stop audio streaming from DUT and end the call.
      6. Wait for audio streaming to stop from REF.
    """
    # Make sure input audio device is ready for recording.
    with self.dut.bl4a.register_callback(bl4a_api.Module.AUDIO) as dut_audio_cb:
      self.logger.info("[DUT] Wait for BLE_HEADSET input audio device")
      await dut_audio_cb.wait_for_event(
          bl4a_api.AudioDeviceAdded(
              device_type=android_constants.AudioDeviceType.BLE_HEADSET,
              address=self.ref.random_address,
              is_source=True,
              is_sink=False,
          )
      )

    sink_ase = self.ref_ascs.ase_state_machines[_SINK_ASE_ID]
    source_ase = self.ref_ascs.ase_state_machines[_SOURCE_ASE_ID]
    condition = asyncio.Condition()

    @sink_ase.on(sink_ase.EVENT_STATE_CHANGE)
    @source_ase.on(sink_ase.EVENT_STATE_CHANGE)
    async def on_state_change() -> None:
      async with condition:
        condition.notify_all()

    # It requires 2 AudioTracks of gaming and communication to trigger the
    # gaming context.
    self.dut.bl4a.set_audio_attributes(
        bl4a_api.AudioAttributes(usage=bl4a_api.AudioAttributes.Usage.GAME),
        handle_audio_focus=False,
    )
    communication_player = self.dut.bt.addPlayer()
    self.dut.bl4a.set_audio_attributes(
        bl4a_api.AudioAttributes(
            usage=bl4a_api.AudioAttributes.Usage.VOICE_COMMUNICATION,
            content_type=bl4a_api.AudioAttributes.ContentType.SPEECH,
        ),
        handle_audio_focus=False,
        player_id=communication_player,
    )
    self.dut.bt.audioSetRepeat(
        android_constants.RepeatMode.ONE, communication_player
    )
    self.test_case_context.callback(
        lambda: self.dut.bt.removePlayer(communication_player)
    )

    # Make sure audio is not streaming.
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to stop",
    ):
      for ase in self.ref_ascs.ase_state_machines.values():
        await _wait_for_ase_state(
            ase, ascs.AudioStreamEndpointCharacteristic.State.IDLE
        )

    call = self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.OUTGOING,
    )
    self.test_case_context.push(call)
    self.logger.info("[DUT] Start gaming audio streaming")
    await asyncio.to_thread(self.dut.bt.audioPlaySine)
    self.logger.info("[DUT] Start communication audio streaming")
    await asyncio.to_thread(self.dut.bt.audioPlaySine, communication_player)
    self.logger.info("[DUT] Start audio recording")
    recorder = await self.dut.bl4a.start_audio_recording(
        _RECORDING_PATH,
        source=bl4a_api.AudioRecorder.Source.VOICE_PERFORMANCE,
        preferred_device_address=self.ref.random_address,
    )
    self.test_case_context.push(recorder)

    if self.dut.getprop(_AndroidProperty.GMAP_ENABLED) == "true":
      # Asymmetric configuration is enabled with GMAP.
      expected_sink_freq = bap.SamplingFrequency.FREQ_48000
    else:
      expected_sink_freq = bap.SamplingFrequency.FREQ_32000

    def _condition_matched() -> bool:
      sink_freq = self._get_sampling_frequency(sink_ase)
      source_freq = self._get_sampling_frequency(source_ase)
      self.logger.info("sink_freq: %r", sink_freq)
      self.logger.info("source_freq: %r", source_freq)
      return (
          sink_freq >= expected_sink_freq
          and source_freq >= bap.SamplingFrequency.FREQ_32000
          and sink_ase.state
          == ascs.AudioStreamEndpointCharacteristic.State.STREAMING
          and source_ase.state
          == ascs.AudioStreamEndpointCharacteristic.State.STREAMING
      )

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to start",
    ):
      async with condition:
        await condition.wait_for(_condition_matched)
    self.logger.info("[REF] Audio streaming started")

    # Streaming for 1 second.
    await asyncio.sleep(_STREAMING_TIME_SECONDS)

    self.logger.info("[DUT] Stop audio streaming")
    await asyncio.to_thread(self.dut.bt.audioStop)
    await asyncio.to_thread(self.dut.bt.audioStop, communication_player)
    recorder.close()
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to stop",
    ):
      for ase in self.ref_ascs.ase_state_machines.values():
        await _wait_for_ase_state(
            ase, ascs.AudioStreamEndpointCharacteristic.State.IDLE
        )

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
    # Make sure audio is not streaming.
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to stop",
    ):
      for ase in self.ref_ascs.ase_state_machines.values():
        await _wait_for_ase_state(
            ase, ascs.AudioStreamEndpointCharacteristic.State.IDLE
        )

    dut_telecom_cb = self.dut.bl4a.register_callback(bl4a_api.Module.TELECOM)
    self.test_case_context.push(dut_telecom_cb)
    call = self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.OUTGOING,
    )
    sink_ase = self.ref_ascs.ase_state_machines[_SINK_ASE_ID]
    source_ase = self.ref_ascs.ase_state_machines[_SOURCE_ASE_ID]
    self.dut.bl4a.set_audio_attributes(
        bl4a_api.AudioAttributes(
            usage=bl4a_api.AudioAttributes.Usage.VOICE_COMMUNICATION,
            content_type=bl4a_api.AudioAttributes.ContentType.SPEECH,
        ),
        handle_audio_focus=False,
    )

    with call:
      await dut_telecom_cb.wait_for_event(
          bl4a_api.CallStateChanged,
          lambda e: (e.state in (_CallState.CONNECTING, _CallState.DIALING)),
      )

      self.logger.info("[DUT] Start audio streaming")
      await asyncio.to_thread(self.dut.bt.audioPlaySine)
      self.logger.info("[DUT] Start audio recording")
      recorder = await self.dut.bl4a.start_audio_recording(
          _RECORDING_PATH,
          source=bl4a_api.AudioRecorder.Source.VOICE_COMMUNICATION,
      )
      self.test_case_context.push(recorder)
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Wait for audio to start",
      ):
        for ase in (sink_ase, source_ase):
          await _wait_for_ase_state(
              ase, ascs.AudioStreamEndpointCharacteristic.State.STREAMING
          )

      # Setup audio sink.
      sink_frames = list[bytes]()
      decoder = decoder_for_ase(sink_ase) if lc3.AVAILABLE else None

      def sink(pdu: hci.HCI_IsoDataPacket):
        if pdu.iso_sdu_fragment:
          sink_frames.append(pdu.iso_sdu_fragment)

      assert (cis_link := sink_ase.cis_link)
      cis_link.sink = sink

      # Streaming for 1 second.
      await asyncio.sleep(_STREAMING_TIME_SECONDS)

      self.logger.info("[DUT] Stop audio streaming")
      cis_link.sink = None
      await asyncio.to_thread(self.dut.bt.audioStop)
      recorder.close()

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to stop",
    ):
      for ase in self.ref_ascs.ase_state_machines.values():
        await _wait_for_ase_state(
            ase, ascs.AudioStreamEndpointCharacteristic.State.IDLE
        )

    if self.user_params.get(navi_test_base.RECORD_FULL_DATA):
      self.write_test_output_data("sink.lc3", b"".join(sink_frames))
    if lc3.AVAILABLE and decoder and audio.SUPPORT_AUDIO_PROCESSING:
      pcm_format = lc3.PcmFormat.SIGNED_16
      decoded_frames = [
          decoder.decode(frame, pcm_format) for frame in sink_frames
      ]
      dominant_frequency = audio.get_dominant_frequency(
          buffer=b"".join(decoded_frames),
          format="pcm",
          sample_width=pcm_format.sample_width,
          frame_rate=_DEFAULT_FRAME_RATE,
          channels=decoder.num_channels,
      )
      self.logger.info("dominant_frequency: %.2f", dominant_frequency)
      self.assertAlmostEqual(dominant_frequency, 1000, delta=10)

  async def test_reconnect_during_call(self) -> None:
    """Tests reconnecting during a call. Call audio should be routed to Unicast.

    Test steps:
      1. Disconnect REF.
      2. Put a call on DUT.
      3. Reconnect REF.
      4. Wait for audio streaming to start from REF.
    """

    if self.dut.device.is_emulator:
      self.skipTest(
          "b/425668688 - TA filter reconnection is not supported on rootcanal"
          " yet."
      )

    with self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO) as dut_cb:
      await self.disconnect_with_check(
          self.ref.random_address, android_constants.Transport.LE
      )

      self.logger.info("[DUT] Wait for LE Audio disconnected")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=None),
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
      recorder = await self.dut.bl4a.start_audio_recording(
          _RECORDING_PATH,
          source=bl4a_api.AudioRecorder.Source.VOICE_COMMUNICATION,
      )
      stack.enter_context(recorder)

      dut_leaudio_cb = self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO)
      stack.enter_context(dut_leaudio_cb)

      self.logger.info("[REF] Start advertising")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await self.ref.device.create_advertising_set(
            advertising_parameters=_DEFAUILT_ADVERTISING_PARAMETERS,
            advertising_data=bytes(
                core.AdvertisingData([
                    _GENERAL_DISCOVERABLE_AD_FLAGS,
                    ascs.make_bap_announcement(
                        announcement_type=bap.AnnouncementType.TARGETED
                    ),
                ])
            ),
        )
      self.logger.info("[DUT] Wait for LE Audio connected")
      await dut_leaudio_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.random_address),
      )

      self.logger.info("[REF] Wait for streaming to start")
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Wait for audio to start",
      ):
        for ase in self.ref_ascs.ase_state_machines.values():
          await _wait_for_ase_state(
              ase, ascs.AudioStreamEndpointCharacteristic.State.STREAMING
          )

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
      await _wait_for_ase_state(
          sink_ase, ascs.AudioStreamEndpointCharacteristic.State.IDLE
      )

    self.logger.info("[DUT] Start audio streaming")
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)
    await asyncio.to_thread(self.dut.bt.audioPlaySine)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for audio to start",
    ):
      await _wait_for_ase_state(
          sink_ase, ascs.AudioStreamEndpointCharacteristic.State.STREAMING
      )
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
      self.dut.bl4a.set_audio_attributes(
          bl4a_api.AudioAttributes(
              usage=bl4a_api.AudioAttributes.Usage.VOICE_COMMUNICATION,
              content_type=bl4a_api.AudioAttributes.ContentType.SPEECH,
          ),
          handle_audio_focus=False,
      )
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[DUT] Wait for ASE to be released",
      ):
        await _wait_for_ase_state(
            sink_ase, ascs.AudioStreamEndpointCharacteristic.State.IDLE
        )
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[DUT] Wait for ASE to be reconfigured",
      ):
        await _wait_for_ase_state(
            sink_ase, ascs.AudioStreamEndpointCharacteristic.State.STREAMING
        )
      context_type = struct.unpack_from("<H", get_audio_context().data)[0]
      self.assertTrue(context_type & bap.ContextType.CONVERSATIONAL)

  async def test_volume_initialization(self) -> None:
    """Makes sure DUT sets the volume correctly after connecting to REF."""
    if not self.dut_vcp_enabled:
      self.skipTest("VCP is not enabled on DUT")
    vcs_volume = pyee_extensions.EventTriggeredValueObserver[int](
        self.ref_vcs,
        self.ref_vcs.EVENT_VOLUME_STATE_CHANGE,
        lambda: self.ref_vcs.volume_setting,
    )
    ref_expected_volume = decimal.Decimal(
        await asyncio.to_thread(
            lambda: self.dut.bt.getVolume(_StreamType.MUSIC)
        )
        / await asyncio.to_thread(
            lambda: self.dut.bt.getMaxVolume(_StreamType.MUSIC)
        )
        * vcs.MAX_VOLUME
    ).to_integral_exact(rounding=decimal.ROUND_HALF_UP)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        "[REF] Wait for volume to be synced with DUT",
    ):
      await vcs_volume.wait_for_target_value(int(ref_expected_volume))

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

    dut_max_volume = await asyncio.to_thread(
        lambda: self.dut.bt.getMaxVolume(_StreamType.MUSIC)
    )
    dut_expected_volume = (
        await asyncio.to_thread(
            lambda: self.dut.bt.getVolume(_StreamType.MUSIC)
        )
        + 1
    ) % (dut_max_volume + 1)
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
          self.ref_vcs,
          self.ref_vcs.EVENT_VOLUME_STATE_CHANGE,
          lambda: self.ref_vcs.volume_setting,
      )
      if issuer == _TestRole.DUT:
        self.logger.info("[DUT] Set volume to %d", dut_expected_volume)
        await asyncio.to_thread(
            lambda: self.dut.bt.setVolume(
                _StreamType.MUSIC, dut_expected_volume
            )
        )
        async with self.assert_not_timeout(
            _DEFAULT_STEP_TIMEOUT_SECONDS,
            msg="[REF] Wait for volume to be set",
        ):
          await vcs_volume.wait_for_target_value(ref_expected_volume)
      else:
        self.logger.info("[REF] Set volume to %d", ref_expected_volume)
        self.ref_vcs.volume_setting = ref_expected_volume
        await self.ref.device.notify_subscribers(self.ref_vcs.volume_state)
        await dut_audio_cb.wait_for_event(
            event=bl4a_api.VolumeChanged(
                stream_type=_StreamType.MUSIC,
                volume_value=int(dut_expected_volume),
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
    await asyncio.to_thread(self.dut.bt.audioPlaySine)
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

    # Allow repeating to avoid the end of the track.
    await asyncio.to_thread(
        self.dut.bt.audioSetRepeat, android_constants.RepeatMode.ONE
    )

    dut_player_cb = self.dut.bl4a.register_callback(bl4a_api.Module.PLAYER)
    self.test_case_context.push(dut_player_cb)

    self.logger.info("[DUT] Generate wave file.")
    self._generate_and_push_wave_file(
        f"/data/media/{self.dut.adb.current_user_id}/Music/sample.wav"
    )
    app_uri = "/storage/self/primary/Music/sample.wav"
    media_item_1 = bl4a_api.MediaItem(id="1", uri=app_uri)
    media_item_2 = bl4a_api.MediaItem(id="2", uri=app_uri)

    self.logger.info("[DUT] Set media item to 1.")
    await asyncio.to_thread(self.dut.bl4a.play_media_item, media_item_1)

    self.logger.info("[DUT] Add media item of 2.")
    await asyncio.to_thread(self.dut.bl4a.add_media_item, media_item_2)

    self.logger.info("[DUT] Wait for playback started.")
    await dut_player_cb.wait_for_event(
        bl4a_api.PlayerIsPlayingChanged(is_playing=True)
    )

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
      result = await ref_mcp_client.write_control_point(_McpOpcode.NEXT_TRACK)
      self.assertEqual(result, mcp.MediaControlPointResultCode.SUCCESS)

    self.logger.info("[DUT] Wait for playback changed.")
    await dut_player_cb.wait_for_event(
        bl4a_api.PlayerMediaItemTransition(media_item=media_item_2),
    )

    await asyncio.sleep(_PREPARE_TIME_SECONDS)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Move to previous track",
    ):
      result = await ref_mcp_client.write_control_point(
          _McpOpcode.PREVIOUS_TRACK
      )
      self.assertEqual(result, mcp.MediaControlPointResultCode.SUCCESS)

    self.logger.info("[DUT] Wait for playback changed.")
    await dut_player_cb.wait_for_event(
        bl4a_api.PlayerMediaItemTransition(media_item=media_item_1),
    )

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

    # Push a 60 seconds audio file to DUT and play it.
    self.logger.info("[DUT] Generate wave file.")
    self._generate_and_push_wave_file(
        f"/data/media/{self.dut.adb.current_user_id}/Music/sample.wav",
        duration_seconds=60,
    )

    self.logger.info("[DUT] Play audio file.")
    self.dut.bl4a.play_media_item(
        bl4a_api.MediaItem(
            uri="/storage/self/primary/Music/sample.wav",
        )
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
      expected_call_states: Sequence[ccp.CallState]
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
                bool(info_list := ccp.CallInfo.parse_list(value))
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
          event=bl4a_api.CallStateChanged,
          predicate=lambda e: e.state == _CallState.ACTIVE,
      )

      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Terminate call",
      ):
        await ref_tbs_client.terminate(expected_call_index)

      self.logger.info("[DUT] Wait for call to be disconnected")
      await dut_telecom_cb.wait_for_event(
          event=bl4a_api.CallStateChanged,
          predicate=lambda e: (e.state == _CallState.DISCONNECTED),
      )

  async def test_ccp_dial_outgoing_call_from_ref(self) -> None:
    """Tests outgoing dial from REF over CCP (originate).

    Test steps:
      1. Set default dialer to snippet on DUT.
      2. Enable and configure mock connection service on DUT.
      3. Connect TBS on REF.
      4. Register Telecom callback on DUT.
      5. Dial phone number from REF using CCP originate.
      6. Verify call dialing event on DUT.
    """
    if not self.dut_ccp_enabled:
      self.skipTest("CCP is not enabled on DUT")

    self._setup_mock_connection_service()

    self.logger.info("[REF] Connect TBS")
    ref_dut_acl = next(iter(self.ref.device.connections.values()))
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

    dut_telecom_cb = self.dut.bl4a.register_callback(bl4a_api.Module.TELECOM)
    self.test_case_context.push(dut_telecom_cb)

    dial_uri = f"tel:{_CALLER_NUMBER}"
    self.logger.info("[REF] Dial %s", dial_uri)
    dial_task = asyncio.create_task(ref_tbs_client.originate(dial_uri))

    self.logger.info("[DUT] Wait for call dialing event.")
    await dut_telecom_cb.wait_for_event(
        bl4a_api.CallStateChanged(
            handle=dial_uri,
            state=android_constants.CallState.DIALING,
            name=mock.ANY,
        ),
    )

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for dial task to complete.")
      await dial_task

  async def test_noisy_handling(self) -> None:
    """Tests enabling noisy handling, and verify the player is paused after REF disconnected.

    Test steps:
      1. Enable noisy handling.
      2. Start streaming.
      3. Disconnect from REF.
      4. Wait for player paused.
    """
    if self.dut.device.is_emulator:
      self.skipTest(
          "b/434613780 - Disconnection on streaming may cause Rootcanal crash."
      )

    # Enable audio noisy handling.
    self.dut.bt.setHandleAudioBecomingNoisy(True)

    sink_ase = self.ref_ascs.ase_state_machines[_SINK_ASE_ID]

    # Make sure audio is not streaming.
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for ASE state to be idle",
    ):
      await _wait_for_ase_state(
          sink_ase, ascs.AudioStreamEndpointCharacteristic.State.IDLE
      )

    self.logger.info("[DUT] Start audio streaming")
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ALL)
    await asyncio.to_thread(self.dut.bt.audioPlaySine)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF] Wait for ASE state to be streaming",
    ):
      await _wait_for_ase_state(
          sink_ase, ascs.AudioStreamEndpointCharacteristic.State.STREAMING
      )

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
          bl4a_api.PlayerIsPlayingChanged(is_playing=False),
      )


if __name__ == "__main__":
  test_runner.main()
