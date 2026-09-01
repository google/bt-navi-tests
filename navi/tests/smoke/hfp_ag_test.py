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

"""Tests related to Bluetooth HFP(Hands-Free Profile) AG role on Pixel."""

import asyncio
import collections
from collections.abc import Iterator, Sequence
import enum
import io
import itertools
import re
from unittest import mock
import wave

from bumble import core
from bumble import device
from bumble import hci
from bumble import hfp
from bumble import rfcomm
from mobly import test_runner
from mobly import signals
from mobly.controllers import android_device
from typing_extensions import override

from navi.utils import resources
from navi.bumble_ext import hfp as hfp_ext
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import audio
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import errors
from navi.utils import lc3

_DEFAULT_STEP_TIMEOUT_SECONDS = 15.0
_HFP_SDP_HANDLE = 1
_CALLER_NAME = "Pixel Bluetooth"
_CALLER_NUMBER = "123456789"
_HFP_MAX_VOLUME = 15
_STREAM_TYPE_CALL = android_constants.StreamType.CALL
_PROPERTY_SWB_SUPPORTED = "bluetooth.hfp.swb.supported"
_RECORDING_PATH = "/storage/self/primary/Recordings/record.wav"
_HFP_FRAME_DURATION = 0.0075  # 7.5ms
_MAX_FRAME_SIZE = 240
_ACTION_VOICE_COMMAND = "android.intent.action.VOICE_COMMAND"
_MOCK_CONNECTION_SERVICE_CLASS = (
    f"{android_constants.PACKAGE_NAME_BLUETOOTH_SNIPPET}.MockConnectionService"
)
_MOCK_CONNECTION_SERVICE_ID = "mock_connection_service"
_MSBC_AUDIO_FILE = "navi/tests/data/sine1000hz_16khz_1s.sbc"
_LC3_AUDIO_FILE = "navi/tests/data/sine1000hz_32khz_1s.lc3"
_AG_SDP_FEATURE_MASK = (
    hfp.AgSdpFeature.THREE_WAY_CALLING
    | hfp.AgSdpFeature.EC_NR
    | hfp.AgSdpFeature.VOICE_RECOGNITION_FUNCTION
    | hfp.AgSdpFeature.IN_BAND_RING_TONE_CAPABILITY
    | hfp.AgSdpFeature.VOICE_TAG
)

_AudioCodec = hfp.AudioCodec
_AgIndicator = hfp.AgIndicator
_CallState = android_constants.CallState
_CallbackHandler = bl4a_api.CallbackHandler
_HfpAgAudioStateChange = bl4a_api.HfpAgAudioStateChanged
_Module = bl4a_api.Module
_ScoState = android_constants.ScoState


@enum.unique
class _CallAnswer(enum.Enum):
  ACCEPT = enum.auto()
  REJECT = enum.auto()


@enum.unique
class _CallAgIndicator(enum.IntEnum):
  INACTIVE = 0
  ACTIVE = 1


def _pcm_to_wave(pcm_data: bytes, frame_rate: int) -> bytes:
  """Converts PCM data to wave data."""
  buffer = io.BytesIO()
  with wave.open(buffer, "wb") as wave_file:
    wave_file.setnchannels(1)
    wave_file.setsampwidth(2)
    wave_file.setframerate(frame_rate)
    wave_file.writeframes(pcm_data)
  return buffer.getvalue()


class HfpAgTest(navi_test_base.TwoDevicesTestBase):

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if self.dut.getprop(android_constants.Property.HFP_AG_ENABLED) != "true":
      raise signals.TestAbortClass("HFP(AG) is not enabled on DUT.")

    self.logger.info("[DUT] Disable all notifications.")
    self.dut.shell("cmd notification set_dnd on")
    self.test_class_context.callback(
        lambda: self.dut.shell("cmd notification set_dnd off")
    )

    self.logger.info("[DUT] Disable all other voice command apps.")
    voice_command_packages: set[str] = set(
        re.findall(
            r"packageName=([\w\.]+)",
            self.dut.shell([
                "pm",
                "query-activities",
                "-a",
                _ACTION_VOICE_COMMAND,
            ]),
        )
    )

    def callback(package: str) -> None:
      self.logger.info("[DUT] Re-Enable voice command app: %s.", package)
      self.dut.shell(["pm", "enable", package])

    for package in voice_command_packages:
      if package == android_constants.PACKAGE_NAME_BLUETOOTH_SNIPPET:
        continue

      self.logger.info("[DUT] Disable voice command app: %s.", package)
      self.dut.shell(["pm", "disable", package])

      self.test_class_context.callback(callback, package)

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    self.logger.info("[DUT] Disconnect all active calls.")
    self.dut.bt.disconnectAllCalls()

  @override
  async def async_teardown_test(self) -> None:
    self.logger.info("[DUT] Disconnect all active calls.")
    self.dut.bt.disconnectAllCalls()
    self.logger.info("[DUT] Stop audio.")
    self.dut.bt.audioStop()

    self.logger.info("[DUT] Reset audio attributes.")
    self.dut.bl4a.set_audio_attributes(
        bl4a_api.AudioAttributes(
            content_type=bl4a_api.AudioAttributes.ContentType.MUSIC,
            usage=bl4a_api.AudioAttributes.Usage.MEDIA,
        ),
        handle_audio_focus=False,
    )

    await super().async_teardown_test()

  def _is_ranchu_emulator(self, dev: android_device.AndroidDevice) -> bool:
    return (build_info := dev.build_info) and build_info["hardware"] == "ranchu"

  async def _wait_for_call_state(
      self,
      dut_telecom_callback: _CallbackHandler,
      *states: _CallState,
  ) -> None:
    self.logger.info(
        "[DUT] Wait for call state in %s.",
        ", ".join(state.name for state in states),
    )
    await dut_telecom_callback.wait_for_event(
        event=bl4a_api.CallStateChanged,
        predicate=lambda e: (e.state in states),
    )

  async def test_pair_and_connect(self) -> None:
    """Tests HFP connection after pairing.

    Test steps:
      1. Setup HFP on REF.
      2. Create bond from DUT.
      3. Wait for HFP connected on DUT.
    """
    self.logger.info("[REF] Setup HFP server.")
    hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_ext.make_hf_configuration(),
    )

    with self.dut.bl4a.register_callback(_Module.HFP_AG) as ag_cb:
      await self.classic_connect_and_pair(connect_profiles=True)

      self.logger.info("[DUT] Wait for HFP connected.")
      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
      )

  async def test_paired_connect_outgoing(self) -> None:
    """Tests HFP reconnection from DUT.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Terminate ACL connection.
      3. Trigger connection from DUT.
      4. Wait for HFP connected on DUT.
      5. Disconnect from DUT.
      6. Wait for HFP disconnected on DUT.
    """
    with self.dut.bl4a.register_callback(_Module.HFP_AG) as ag_cb:
      await self.test_pair_and_connect()

      self.logger.info("[DUT] Wait for HFP connected.")
      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
      )

      await self.disconnect_with_check(
          self.ref.address, android_constants.Transport.CLASSIC
      )

      self.logger.info("[DUT] Wait for HFP disconnected.")
      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=None),
      )

      self.logger.info("[DUT] Reconnect.")
      self.dut.bt.connect(self.ref.address)

      self.logger.info("[DUT] Wait for HFP connected.")
      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
      )

      self.logger.info("[DUT] Disconnect.")
      self.dut.bt.disconnect(self.ref.address)

      self.logger.info("[DUT] Wait for HFP disconnected.")
      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=None),
      )

  async def test_paired_connect_incoming(self) -> None:
    """Tests HFP reconnection from REF.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Terminate ACL connection.
      3. Trigger connection from REF.
      4. Wait HFP connected on DUT.
      5. Disconnect from REF.
      6. Wait HFP disconnected on DUT.
    """
    dut_cb = self.dut.bl4a.register_callback(_Module.HFP_AG)
    self.test_case_context.push(dut_cb)

    await self.test_pair_and_connect()

    self.logger.info("[DUT] Wait for HFP connected.")
    await dut_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
    )

    await self.disconnect_with_check(
        self.ref.address, android_constants.Transport.CLASSIC
    )

    self.logger.info("[DUT] Wait for HFP disconnected.")
    await dut_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=None),
    )

    self.logger.info("[REF] Reconnect.")
    dut_ref_acl = await self.ref.device.connect(
        self.dut.address,
        core.BT_BR_EDR_TRANSPORT,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    self.logger.info("[REF] Authenticate connection.")
    await dut_ref_acl.authenticate()

    self.logger.info("[REF] Encrypt connection.")
    await dut_ref_acl.encrypt()

    self.logger.info("[REF] Discover SDP records.")
    sdp_records = await hfp_ext.AudioGatewaySdpRecord.find(dut_ref_acl)
    self.logger.info("[REF] Found SDP records: %s.", sdp_records)
    self.assertLen(sdp_records, 1)
    rfcomm_channel = sdp_records[0].rfcomm_channel

    self.logger.info("[REF] Open RFCOMM Multiplexer.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      multiplexer = await rfcomm.Client(dut_ref_acl).start()

    self.logger.info("[REF] Open RFCOMM DLC.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      dlc = await multiplexer.open_dlc(rfcomm_channel)

    self.logger.info("[REF] Establish SLC.")
    ref_hfp_protocol = hfp_ext.HfProtocol(dlc, hfp_ext.make_hf_configuration())
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await ref_hfp_protocol.initiate_slc()

    self.logger.info("[DUT] Wait for HFP connected.")
    await dut_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
    )

    self.logger.info("[REF] Disconnect.")
    await dut_ref_acl.disconnect()

    self.logger.info("[DUT] Wait for HFP disconnected.")
    await dut_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=None),
    )

  async def test_sdp_discovery(self) -> None:
    """Tests SDP discovery from REF.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Discover SDP records from REF.
      3. Verify SDP records.
    """
    await self.test_pair_and_connect()
    ref_acl_connection = self.ref.device.find_connection_by_bd_addr(
        hci.Address(self.dut.address),
        transport=core.PhysicalTransport.BR_EDR,
    )
    if not ref_acl_connection:
      self.fail("No ACL connection found.")

    self.logger.info("[REF] Discover SDP records.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      records = await hfp_ext.AudioGatewaySdpRecord.find(ref_acl_connection)
      if not records:
        self.fail("No SDP record found.")
      self.logger.info("[REF] Found SDP records: %s.", records)

    self.assertLen(records, 1)
    record = records[0]
    self.assertGreaterEqual(record.version, hfp.ProfileVersion.V1_6)
    expected_features = self.dut.get_int_prop(
        android_constants.Property.HFP_AG_FEATURES_MASK
    )
    if expected_features is None:
      expected_features = (
          hfp.AgSdpFeature.THREE_WAY_CALLING
          | hfp.AgSdpFeature.EC_NR
          | hfp.AgSdpFeature.VOICE_RECOGNITION_FUNCTION
          | hfp.AgSdpFeature.IN_BAND_RING_TONE_CAPABILITY
          | hfp.AgSdpFeature.WIDE_BAND_SPEECH
      )
    else:
      expected_features &= _AG_SDP_FEATURE_MASK
    self.assertContainsSubset(
        hfp.AgSdpFeature(expected_features),
        record.supported_features,
    )
    if (
        self.dut.getprop(_PROPERTY_SWB_SUPPORTED) == "true"
        or self.dut.getprop(android_constants.Property.HFP_SW_PATH_ENABLED)
        == "true"
    ):
      self.assertIn(
          hfp.AgSdpFeature.SUPER_WIDE_BAND_SPEED_SPEECH,
          record.supported_features,
      )
    else:
      self.assertNotIn(
          hfp.AgSdpFeature.SUPER_WIDE_BAND_SPEED_SPEECH,
          record.supported_features,
      )

  async def test_reconnect_bt_on_off(self) -> None:
    """Tests HFP connection after BT on/off.

    Test steps:
      1. Setup HF on REF.
      2. Create bond from DUT.
      3. Wait for HFP connected on DUT.
      4. Turn off BT on DUT.
      5. Wait for HFP disconnected on DUT.
      6. Turn on BT on DUT.
      7. Wait for HFP connected on DUT.
    """

    await self.test_pair_and_connect()

    with self.dut.bl4a.register_callback(bl4a_api.Module.HFP_AG) as ag_cb:
      self.logger.info("[DUT] Turn off BT.")
      self.assertTrue(self.dut.bt.disable())

      self.logger.info("[DUT] Wait for BT disabled.")
      self.dut.bt.waitForAdapterState(android_constants.AdapterState.OFF)

      self.logger.info("[DUT] Wait for HFP disconnected.")
      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=None)
      )

      self.logger.info("[DUT] Turn on BT.")
      self.assertTrue(self.dut.bt.enable())

      self.logger.info("[DUT] Wait for BT enabled.")
      self.dut.bt.waitForAdapterState(android_constants.AdapterState.ON)

      self.logger.info("[DUT] Wait for HFP connected.")
      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address)
      )

  async def _test_streaming_with_codec_negotiation(
      self,
      preferred_codec: hfp.AudioCodec,
      supported_audio_codecs: list[hfp.AudioCodec],
      ref_tx_data_iterator: Iterator[Sequence[int]],
  ) -> tuple[bytes, list[hci.HCI_SynchronousDataPacket]]:
    """Tests SCO streaming with codec negotiation.

    Test steps:
      1. Connect and pair REF.
      2. Wait for HFP connected.
      3. [DUT] Add call.
      4. [DUT] Start streaming.
      5. Wait for SCO connected.
      6. [DUT] Start recording.
      7. Streaming for 5 seconds.
      8. [DUT] Terminate call.
      9. Wait for SCO disconnected.
      10. [DUT] Stop recording.

    Args:
      preferred_codec: The preferred codec.
      supported_audio_codecs: The supported audio codecs.
      ref_tx_data_iterator: The iterator for REF to send SCO data.

    Returns:
      A tuple of (DUT RX received buffer, REF RX received packets).
    """
    # [REF] Setup HFP.
    hfp_configuration = hfp_ext.make_hf_configuration(
        supported_hf_features=[hfp.HfFeature.CODEC_NEGOTIATION],
        supported_hf_indicators=[],
        supported_audio_codecs=supported_audio_codecs,
    )
    ref_hfp_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_configuration,
    )

    ag_cb = self.dut.bl4a.register_callback(_Module.HFP_AG)
    telecom_cb = self.dut.bl4a.register_callback(_Module.TELECOM)
    self.test_case_context.push(ag_cb)
    self.test_case_context.push(telecom_cb)

    await self.classic_connect_and_pair(connect_profiles=True)

    self.logger.info("[DUT] Wait for HFP connected.")
    await ag_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
    )

    self.logger.info("[REF] Wait for HFP connected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_hfp_protocol = await ref_hfp_protocol_queue.get()

    sco_links = asyncio.Queue[device.ScoLink]()
    self.ref.device.on(
        self.ref.device.EVENT_SCO_CONNECTION, sco_links.put_nowait
    )

    self.logger.info("[DUT] Add call.")
    with self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.OUTGOING,
    ) as call:
      await self._wait_for_call_state(
          telecom_cb, _CallState.CONNECTING, _CallState.DIALING
      )

      self.logger.info("[DUT] Answer call.")
      call.answer()

      await self._wait_for_call_state(telecom_cb, _CallState.ACTIVE)

      self.logger.info("[DUT] Set audio repeat mode to one.")
      self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)

      self.logger.info("[DUT] Set audio attributes for voice communication.")
      self.dut.bl4a.set_audio_attributes(
          bl4a_api.AudioAttributes(
              content_type=bl4a_api.AudioAttributes.ContentType.SPEECH,
              usage=bl4a_api.AudioAttributes.Usage.VOICE_COMMUNICATION,
          ),
          handle_audio_focus=False,
      )

      self.logger.info("[DUT] Start streaming.")
      self.dut.bt.audioPlaySine()

      self.logger.info("[DUT] Wait for SCO connected.")
      await ag_cb.wait_for_event(
          _HfpAgAudioStateChange(
              address=self.ref.address, state=_ScoState.CONNECTED
          )
      )

      self.logger.info("[REF] Wait for SCO connected.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        sco_link = await sco_links.get()

      self.assertEqual(ref_hfp_protocol.active_codec, preferred_codec)

      self.logger.info("[DUT] Start recording.")
      recorder = await self.dut.bl4a.start_audio_recording(_RECORDING_PATH)
      self.test_case_context.push(recorder)

      ref_received_packets = list[hci.HCI_SynchronousDataPacket]()

      def ref_send_sco_data() -> None:
        if sco_link not in self.ref.device.sco_links.values():
          return
        ref_tx_data = next(ref_tx_data_iterator)
        self.ref.device.host.send_sco_sdu(sco_link.handle, bytes(ref_tx_data))
        # Sleep for 90% of the frame duration, or packets might be dropped.
        asyncio.get_running_loop().call_later(
            _HFP_FRAME_DURATION * 0.9, ref_send_sco_data
        )

      sco_link.sink = ref_received_packets.append
      ref_send_sco_data()

      self.logger.info("[DUT] Stream for 5 seconds.")
      await asyncio.sleep(5.0)

      self.logger.info("[DUT] Terminate call.")
      call.close()

      await self._wait_for_call_state(telecom_cb, _CallState.DISCONNECTED)

    self.logger.info("[DUT] Wait for SCO disconnected.")
    await ag_cb.wait_for_event(
        _HfpAgAudioStateChange(
            address=self.ref.address,
            state=_ScoState.DISCONNECTED,
        )
    )

    self.logger.info("[DUT] Stop recording.")
    await asyncio.to_thread(recorder.close)

    self.logger.info("[DUT] Stop streaming.")
    await asyncio.to_thread(self.dut.bt.audioStop)

    # Get recording from DUT.
    rx_received_buffer = self.dut.adb.shell([
        "cat",
        f"/data/media/{self.dut.adb.current_user_id}/Recordings/record.wav",
    ])

    self.assertTrue(sco_links.empty(), "More than one SCO link is created.")

    return rx_received_buffer, ref_received_packets

  async def test_streaming_with_codec_negotiation_cvsd(self) -> None:
    """Test SCO streaming with codec negotiation using CVSD.

    Test steps:
      1. Setup HFP connection.
      2. [DUT] Add call.
      3. Verify SCO connection and codec.
      4. Streaming for 5 second.
      5. Terminate call.
      6. Verify SCO disconnection.
      7. Verify dominant frequency of streaming data.
    """
    dut_rx_received_buffer, ref_received_packets = (
        await self._test_streaming_with_codec_negotiation(
            preferred_codec=hfp.AudioCodec.CVSD,
            supported_audio_codecs=[hfp.AudioCodec.CVSD],
            ref_tx_data_iterator=itertools.cycle(
                audio.batched(
                    audio.generate_sine_tone(
                        frequency=1000,
                        duration=1.5,
                        sample_rate=8000,
                        data_type="int16",
                    ),
                    n=120,
                )
            ),
        )
    )

    ref_rx_received_buffer = _pcm_to_wave(
        b"".join([packet.data for packet in ref_received_packets]),
        frame_rate=8000,
    )
    self.write_test_output_data("tx.wav", ref_rx_received_buffer)
    self.write_test_output_data("rx.wav", dut_rx_received_buffer)

    if self.dut.device.is_emulator:
      self.logger.info("Skip codec check for emulator.")
      return

    tx_dominant_frequency = audio.get_dominant_frequency(
        ref_rx_received_buffer, format="wav"
    )
    self.logger.info("[Tx] Dominant frequency: %.2f", tx_dominant_frequency)
    rx_dominant_frequency = audio.get_dominant_frequency(
        dut_rx_received_buffer, format="wav"
    )
    self.logger.info("[Rx] Dominant frequency: %.2f", rx_dominant_frequency)
    self.assertAlmostEqual(tx_dominant_frequency, 1000, delta=10)
    self.assertAlmostEqual(rx_dominant_frequency, 1000, delta=10)

  async def test_streaming_with_codec_negotiation_msbc(self) -> None:
    """Test SCO streaming with codec negotiation using mSBC.

    Test steps:
      1. Setup HFP connection.
      2. [DUT] Add call.
      3. Verify SCO connection and codec.
      4. Streaming for 5 second.
      5. Terminate call.
      6. Verify SCO disconnection.
      7. Verify dominant frequency of streaming data.
    """
    frame_size = 57
    ref_tx_data_iterator = (
        header + bytes(payload) + b"\0"  # Padding.
        for header, payload in zip(
            itertools.cycle(hfp_ext.SCO_H2_HEADER),
            itertools.cycle(
                audio.batched(
                    resources.GetResource(_MSBC_AUDIO_FILE), frame_size
                )
            ),
        )
    )
    dut_rx_received_buffer, ref_rx_received_packets = (
        await self._test_streaming_with_codec_negotiation(
            preferred_codec=hfp.AudioCodec.MSBC,
            supported_audio_codecs=[hfp.AudioCodec.CVSD, hfp.AudioCodec.MSBC],
            ref_tx_data_iterator=ref_tx_data_iterator,
        )
    )

    ref_rx_received_buffer = b"".join([
        # Drop header and padding.
        packet.data[hfp_ext.SCO_H2_HEADER_SIZE : -1]
        for packet in ref_rx_received_packets
        # Filter out invalid packets, or libsbc cannot decode them.
        if (
            packet.packet_status == 0
            and (
                len(packet.data)
                == hfp_ext.ESCO_PARAMETERS_T2_TRANSPARENT.transmit_codec_frame_size
            )
            and (
                packet.data[: hfp_ext.SCO_H2_HEADER_SIZE]
                in hfp_ext.SCO_H2_HEADER
            )
        )
    ])
    self.write_test_output_data("tx.sbc", ref_rx_received_buffer)
    self.write_test_output_data("rx.wav", dut_rx_received_buffer)

    if self.dut.device.is_emulator:
      self.logger.info("Skip codec check for emulator.")
      return

    tx_dominant_frequency = audio.get_dominant_frequency(
        ref_rx_received_buffer,
        format="sbc",
        frame_rate=16000,
    )
    self.logger.info("[Tx] Dominant frequency: %.2f", tx_dominant_frequency)
    rx_dominant_frequency = audio.get_dominant_frequency(
        dut_rx_received_buffer, format="wav"
    )
    self.logger.info("[Rx] Dominant frequency: %.2f", rx_dominant_frequency)
    self.assertAlmostEqual(tx_dominant_frequency, 1000, delta=10)
    self.assertAlmostEqual(rx_dominant_frequency, 1000, delta=10)

  async def test_streaming_with_codec_negotiation_lc3(self) -> None:
    """Test SCO streaming with codec negotiation using LC3 SWB.

    Test steps:
      1. Setup HFP connection.
      2. [DUT] Add call.
      3. Verify SCO connection and codec.
      4. Streaming for 5 second.
      5. Terminate call.
      6. Verify SCO disconnection.
      7. Verify dominant frequency of streaming data.
    """

    if (
        self.dut.getprop(_PROPERTY_SWB_SUPPORTED) != "true"
        and self.dut.getprop(android_constants.Property.HFP_SW_PATH_ENABLED)
        != "true"
    ):
      self.skipTest("LC3 SWB is not supported on DUT.")

    frame_size = 58
    ref_tx_data_iterator = (
        header + bytes(payload)
        for header, payload in zip(
            itertools.cycle(hfp_ext.SCO_H2_HEADER),
            itertools.cycle(
                audio.batched(
                    resources.GetResource(_LC3_AUDIO_FILE), frame_size
                )
            ),
        )
    )
    dut_rx_received_buffer, ref_rx_received_packets = (
        await self._test_streaming_with_codec_negotiation(
            preferred_codec=hfp.AudioCodec.LC3_SWB,
            supported_audio_codecs=[
                hfp.AudioCodec.CVSD,
                hfp.AudioCodec.MSBC,
                hfp.AudioCodec.LC3_SWB,
            ],
            ref_tx_data_iterator=ref_tx_data_iterator,
        )
    )

    if self.dut.device.is_emulator:
      self.logger.info("Skip codec check for emulator.")
      return

    if lc3.AVAILABLE:
      decoder = lc3.Decoder(
          frame_duration_us=7500,
          sample_rate_hz=32000,
          pcm_sample_rate_hz=32000,
          num_channels=1,
      )
      ref_rx_received_buffer_decoded = _pcm_to_wave(
          b"".join([
              decoder.decode(
                  # Drop header.
                  packet.data[hfp_ext.SCO_H2_HEADER_SIZE :],
                  lc3.PcmFormat.SIGNED_16,
              )
              for packet in ref_rx_received_packets
          ]),
          frame_rate=32000,
      )
      self.write_test_output_data("tx.wav", ref_rx_received_buffer_decoded)
      tx_dominant_frequency = audio.get_dominant_frequency(
          ref_rx_received_buffer_decoded, format="wav"
      )
      self.logger.info("[Tx] Dominant frequency: %.2f", tx_dominant_frequency)
      self.assertAlmostEqual(tx_dominant_frequency, 1000, delta=10)

    self.write_test_output_data("rx.wav", dut_rx_received_buffer)
    rx_dominant_frequency = audio.get_dominant_frequency(
        dut_rx_received_buffer, format="wav"
    )
    self.logger.info("[Rx] Dominant frequency: %.2f", rx_dominant_frequency)
    self.assertAlmostEqual(rx_dominant_frequency, 1000, delta=10)

  @navi_test_base.parameterized(_CallAnswer.ACCEPT, _CallAnswer.REJECT)
  async def test_answer_call_from_ref(self, call_answer: _CallAnswer) -> None:
    """Tests answering an incoming phone call from REF.

    Test steps:
      1. Setup HFP connection.
      2. Place an incoming call.
      3. Answer call on REF.
      4. Verify call status.

    Args:
      call_answer: Answer type of call.
    """
    if self._is_ranchu_emulator(self.dut.device):
      self.skipTest("[DUT] Call control is not supported on Ranchu emulator.")

    self.logger.info("[REF] Setup HFP server.")
    ref_hfp_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_ext.make_hf_configuration(),
    )

    ag_cb = self.dut.bl4a.register_callback(_Module.HFP_AG)
    telecom_cb = self.dut.bl4a.register_callback(_Module.TELECOM)
    self.test_case_context.push(ag_cb)
    self.test_case_context.push(telecom_cb)

    await self.classic_connect_and_pair(connect_profiles=True)

    self.logger.info("[REF] Wait for HFP connected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_hfp_protocol = await ref_hfp_protocol_queue.get()

    self.logger.info("[DUT] Wait for HFP connected.")
    await ag_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
    )

    condition = asyncio.Condition()

    @ref_hfp_protocol.on(ref_hfp_protocol.EVENT_AG_INDICATOR)
    async def _(*_) -> None:
      async with condition:
        condition.notify_all()

    self.logger.info("[DUT] Make incoming call.")
    with self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.INCOMING,
    ):
      await self._wait_for_call_state(telecom_cb, _CallState.RINGING)

      self.logger.info("[REF] Wait for callsetup.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        async with condition:
          call_setup = next(
              indicator
              for indicator in ref_hfp_protocol.ag_indicators
              if indicator.indicator == hfp.AgIndicator.CALL_SETUP
          )
          await condition.wait_for(lambda: (call_setup.current_status == 1))

      if call_answer == _CallAnswer.ACCEPT:
        self.logger.info("[REF] Answer call.")
        await ref_hfp_protocol.answer_incoming_call()

        self.logger.info("[DUT] Wait for call state active.")
        await self._wait_for_call_state(telecom_cb, _CallState.ACTIVE)
      else:
        self.logger.info("[REF] Reject call.")
        await ref_hfp_protocol.reject_incoming_call()

        self.logger.info("[DUT] Wait for call state disconnected.")
        await self._wait_for_call_state(telecom_cb, _CallState.DISCONNECTED)

  @navi_test_base.parameterized(
      constants.Direction.INCOMING,
      constants.Direction.OUTGOING,
  )
  async def test_callsetup_ag_indicator(
      self,
      direction: constants.Direction,
  ) -> None:
    """Tests making phone call, observing AG indicator.

    Test steps:
      1. Setup HFP connection.
      2. Place a phone call.
      3. Verify callsetup ag indicator.
      4. Answer the call
      5. Verify callsetup and call ag indicator.
      6. Terminate the call.
      7. Verify call ag indicator.

    Args:
      direction: The direction of phone call.
    """

    self.logger.info("[REF] Setup HFP server.")
    ref_hfp_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_ext.make_hf_configuration(),
    )

    with self.dut.bl4a.register_callback(_Module.HFP_AG) as ag_cb:
      await self.classic_connect_and_pair(connect_profiles=True)

      self.logger.info("[DUT] Wait for HFP connected.")
      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
      )

    self.logger.info("[REF] Wait for HFP connected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_hfp_protocol = await ref_hfp_protocol_queue.get()

    ag_indicators = collections.defaultdict[
        hfp.AgIndicator, asyncio.Queue[int]
    ](asyncio.Queue)

    def on_ag_indicator(ag_indicator: hfp.AgIndicatorState) -> None:
      ag_indicators[ag_indicator.indicator].put_nowait(
          ag_indicator.current_status
      )

    ref_hfp_protocol.on(ref_hfp_protocol.EVENT_AG_INDICATOR, on_ag_indicator)

    self.logger.info("[DUT] Make phone call.")
    with self.dut.bl4a.make_phone_call(
        _CALLER_NAME, _CALLER_NUMBER, direction
    ) as call:
      if direction == constants.Direction.INCOMING:
        self.logger.info("[REF] Wait for (callsetup, 1 - incoming).")
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          self.assertEqual(
              await ag_indicators[_AgIndicator.CALL_SETUP].get(),
              hfp.CallSetupAgIndicator.INCOMING_CALL_PROCESS,
          )
      else:
        self.logger.info("[REF] Wait for (callsetup, 2 - outgoing).")
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          self.assertEqual(
              await ag_indicators[_AgIndicator.CALL_SETUP].get(),
              hfp.CallSetupAgIndicator.OUTGOING_CALL_SETUP,
          )

        self.logger.info("[REF] Wait for (callsetup, 3 - remote alerted).")
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          self.assertEqual(
              await ag_indicators[_AgIndicator.CALL_SETUP].get(),
              hfp.CallSetupAgIndicator.REMOTE_ALERTED,
          )

      self.logger.info("[DUT] Answer Call.")
      call.answer()

      self.logger.info("[REF] Wait for (callsetup, 0 - not in setup).")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.assertEqual(
            await ag_indicators[_AgIndicator.CALL_SETUP].get(),
            hfp.CallSetupAgIndicator.NOT_IN_CALL_SETUP,
        )

      self.logger.info("[REF] Wait for (call, 1 - active).")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.assertEqual(
            await ag_indicators[_AgIndicator.CALL].get(),
            _CallAgIndicator.ACTIVE,
        )

      self.logger.info("[DUT] Terminate Call.")
      call.close()

    self.logger.info("[REF] Wait for (call, 0 - inactive).")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.assertEqual(
          await ag_indicators[_AgIndicator.CALL].get(),
          _CallAgIndicator.INACTIVE,
      )

  @navi_test_base.timeout(120.0)
  async def test_update_battery_level(self) -> None:
    """Tests updating battery level indicator from HF.

    Test steps:
      1. Setup HFP connection.
      2. Send battery level indicator from HF.
      3. Verify call ag indicator.
    """

    self.logger.info("[REF] Setup HFP server.")
    hfp_configuration = hfp.HfConfiguration(
        supported_hf_features=[hfp.HfFeature.HF_INDICATORS],
        supported_hf_indicators=[hfp.HfIndicator.BATTERY_LEVEL],
        supported_audio_codecs=[hfp.AudioCodec.CVSD],
    )
    ref_hfp_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_configuration,
    )

    with (
        self.dut.bl4a.register_callback(_Module.HFP_AG) as ag_cb,
        self.dut.bl4a.register_callback(_Module.ADAPTER) as adapter_cb,
    ):
      await self.classic_connect_and_pair(connect_profiles=True)

      self.logger.info("[DUT] Wait for HFP connected.")
      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
      )

      self.logger.info("[REF] Wait for HFP connected.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_hfp_protocol = await ref_hfp_protocol_queue.get()

      if not ref_hfp_protocol.supports_ag_feature(hfp.AgFeature.HF_INDICATORS):
        raise signals.TestSkip("[REF] Does not support HF Indicator")

      for i in range(101):
        await ref_hfp_protocol.execute_command(
            f"AT+BIEV={hfp.HfIndicator.BATTERY_LEVEL.value},{i}"
        )
        await adapter_cb.wait_for_event(
            bl4a_api.BatteryLevelChanged(address=self.ref.address, level=i),
        )

  async def test_connect_hf_during_call_should_route_to_hf(self) -> None:
    """Tests connecting HFP during phone call should route to HFP.

    Test steps:
      1. Place a call.
      2. Setup HFP connection.
    """

    self.logger.info("[REF] Setup HFP server.")
    hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_ext.make_hf_configuration(),
    )

    self.logger.info("[DUT] Make outgoing call.")
    with (
        self.dut.bl4a.register_callback(_Module.TELECOM) as telecom_cb,
        self.dut.bl4a.make_phone_call(
            _CALLER_NAME,
            _CALLER_NUMBER,
            constants.Direction.OUTGOING,
        ),
    ):
      self.logger.info("[DUT] Set repeat mode to one.")
      self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)

      self.logger.info("[DUT] Set audio attributes for voice communication.")
      self.dut.bl4a.set_audio_attributes(
          bl4a_api.AudioAttributes(
              content_type=bl4a_api.AudioAttributes.ContentType.SPEECH,
              usage=bl4a_api.AudioAttributes.Usage.VOICE_COMMUNICATION,
          ),
          handle_audio_focus=False,
      )

      self.logger.info("[DUT] Play sine wave.")
      self.dut.bt.audioPlaySine()

      await self._wait_for_call_state(
          telecom_cb, _CallState.CONNECTING, _CallState.DIALING
      )

      with self.dut.bl4a.register_callback(_Module.HFP_AG) as ag_cb:
        await self.classic_connect_and_pair(connect_profiles=True)

        self.logger.info("[DUT] Wait for SCO connected.")
        await ag_cb.wait_for_event(
            _HfpAgAudioStateChange(
                address=self.ref.address, state=_ScoState.CONNECTED
            )
        )

  @navi_test_base.parameterized(constants.TestRole.DUT, constants.TestRole.REF)
  @navi_test_base.retry(max_count=2)
  async def test_adjust_speaker_volume(
      self, issuer: constants.TestRole
  ) -> None:
    """Tests adjusting speaker volume with HFP.

    Test steps:
      1. Place a call.
      2. Setup HFP connection.
      3. Adjust volume.

    Args:
      issuer: The issuer of volume adjustment.
    """
    if (
        self.dut.device.is_emulator
        and self.dut.getprop(android_constants.Property.SCO_MANAGED_BY_AUDIO)
        != "true"
    ):
      self.skipTest("Volume control is only available with AMSCO on emulator")

    # [REF] Setup HFP.
    hfp_configuration = hfp.HfConfiguration(
        supported_hf_features=[hfp.HfFeature.REMOTE_VOLUME_CONTROL],
        supported_hf_indicators=[],
        supported_audio_codecs=[hfp.AudioCodec.CVSD],
    )
    ref_hfp_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_configuration,
    )

    with (
        self.dut.bl4a.register_callback(_Module.HFP_AG) as ag_cb,
        self.dut.bl4a.register_callback(_Module.AUDIO) as audio_cb,
        self.dut.bl4a.make_phone_call(
            _CALLER_NAME,
            _CALLER_NUMBER,
            constants.Direction.OUTGOING,
        ),
    ):
      await self.classic_connect_and_pair(connect_profiles=True)

      self.logger.info("[DUT] Set audio attributes for voice communication.")
      self.dut.bl4a.set_audio_attributes(
          bl4a_api.AudioAttributes(
              content_type=bl4a_api.AudioAttributes.ContentType.SPEECH,
              usage=bl4a_api.AudioAttributes.Usage.VOICE_COMMUNICATION,
          ),
          handle_audio_focus=False,
      )

      self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)
      self.dut.bt.audioPlaySine()

      self.logger.info("[DUT] Wait for SCO connected.")
      await ag_cb.wait_for_event(
          _HfpAgAudioStateChange(
              address=self.ref.address, state=_ScoState.CONNECTED
          )
      )

      self.logger.info("[REF] Wait for HFP connected.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_hfp_protocol = await ref_hfp_protocol_queue.get()

      if not self.dut.device.is_emulator:
        self.logger.info("[DUT] Wait for SCO active.")
        await audio_cb.wait_for_event(
            bl4a_api.CommunicationDeviceChanged(
                self.ref.address,
                device_type=android_constants.AudioDeviceType.BLUETOOTH_SCO,
            )
        )

      # Volume change cannot be broadcasted to Bluetooth at the moment
      # when SCO becomes active.
      self.logger.info("[DUT] Wait for volume change to be ready.")
      await asyncio.sleep(0.5)

      for expected_volume in range(1, _HFP_MAX_VOLUME + 1):
        if expected_volume == self.dut.bt.getVolume(_STREAM_TYPE_CALL):
          continue

        if issuer == constants.TestRole.DUT:
          self.logger.info("[DUT] Set volume to %d.", expected_volume)
          self.dut.bt.setVolume(_STREAM_TYPE_CALL, expected_volume)

          self.logger.info("[REF] Wait for volume changed event.")
          async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
            async with ref_hfp_protocol.speaker_volume_condition:
              await ref_hfp_protocol.speaker_volume_condition.wait_for(
                  lambda: ref_hfp_protocol.speaker_volume == expected_volume,  # pylint: disable=cell-var-from-loop
              )
        else:
          self.logger.info("[REF] Set volume to %d.", expected_volume)
          await ref_hfp_protocol.execute_command(f"AT+VGS={expected_volume}")

          self.logger.info("[DUT] Wait for volume changed event.")
          await audio_cb.wait_for_event(
              bl4a_api.VolumeChanged(
                  stream_type=_STREAM_TYPE_CALL, volume_value=expected_volume
              ),
          )

  async def test_query_call_status(self) -> None:
    """Tests querying call status from HF.

    Test steps:
      1. Setup HFP connection.
      2. Place a call.
      3. Query call status from HF.
      4. Terminate the call.
      5. Query call status from HF.
    """

    self.logger.info("[REF] Setup HFP server.")
    ref_hfp_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_ext.make_hf_configuration(),
    )

    with self.dut.bl4a.register_callback(_Module.HFP_AG) as ag_cb:
      await self.classic_connect_and_pair(connect_profiles=True)

      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
      )

      self.logger.info("[REF] Wait for HFP connected.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_hfp_protocol = await ref_hfp_protocol_queue.get()

    ag_indicators = collections.defaultdict[
        hfp.AgIndicator, asyncio.Queue[int]
    ](asyncio.Queue)

    def on_ag_indicator(ag_indicator: hfp.AgIndicatorState) -> None:
      ag_indicators[ag_indicator.indicator].put_nowait(
          ag_indicator.current_status
      )

    ref_hfp_protocol.on(ref_hfp_protocol.EVENT_AG_INDICATOR, on_ag_indicator)

    self.logger.info("[DUT] Make incoming call.")
    with self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.INCOMING,
    ):
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        call_setup_state = await ag_indicators[_AgIndicator.CALL_SETUP].get()
        self.assertEqual(call_setup_state, 1)

      calls = await ref_hfp_protocol.query_current_calls()
      self.assertLen(calls, 1)
      self.assertEqual(
          calls[0].direction,
          hfp.CallInfoDirection.MOBILE_TERMINATED_CALL,
      )
      self.assertEqual(calls[0].status, hfp.CallInfoStatus.INCOMING)
      self.assertEqual(calls[0].number, _CALLER_NUMBER)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      call_setup_state = await ag_indicators[_AgIndicator.CALL_SETUP].get()
      self.assertEqual(call_setup_state, 0)

    calls = await ref_hfp_protocol.query_current_calls()
    self.assertEmpty(calls)

  async def test_hold_unhold_call(self) -> None:
    """Tests holding and unholding call with HFP.

    Test steps:
      1. Setup HFP connection.
      2. Place an outgoing call.
      3. Hold the call.
      4. Unhold the call.
    """
    if self._is_ranchu_emulator(self.dut.device):
      self.skipTest("[DUT] Call hold is not supported on Ranchu emulator.")

    self.logger.info("[REF] Setup HFP server.")
    ref_hfp_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_ext.make_hf_configuration(),
    )

    with self.dut.bl4a.register_callback(_Module.HFP_AG) as ag_cb:
      await self.classic_connect_and_pair(connect_profiles=True)

      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
      )

      self.logger.info("[REF] Wait for HFP connected.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_hfp_protocol = await ref_hfp_protocol_queue.get()

    ag_indicators = collections.defaultdict[
        hfp.AgIndicator, asyncio.Queue[int]
    ](asyncio.Queue)

    def on_ag_indicator(ag_indicator: hfp.AgIndicatorState) -> None:
      ag_indicators[ag_indicator.indicator].put_nowait(
          ag_indicator.current_status
      )

    ref_hfp_protocol.on(ref_hfp_protocol.EVENT_AG_INDICATOR, on_ag_indicator)

    self.logger.info("[DUT] Make incoming call.")
    with (
        self.dut.bl4a.register_callback(_Module.TELECOM) as telecom_cb,
        self.dut.bl4a.make_phone_call(
            _CALLER_NAME,
            _CALLER_NUMBER,
            constants.Direction.OUTGOING,
        ) as call,
    ):
      # 25Q1 => CONNECTING, 25Q2 -> DIALING
      self.logger.info("[DUT] Wait for call state to be CONNECTING or DIALING.")
      await self._wait_for_call_state(
          telecom_cb, _CallState.CONNECTING, _CallState.DIALING
      )

      self.logger.info("[REF] Answer call.")
      call.answer()

      self.logger.info("[DUT] Wait for call state to be ACTIVE.")
      await self._wait_for_call_state(telecom_cb, _CallState.ACTIVE)

      self.logger.info("[REF] Wait for HFP AG call indicator to be ACTIVE.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.assertEqual(
            await ag_indicators[_AgIndicator.CALL].get(),
            _CallAgIndicator.ACTIVE,
            "[REF] HFP AG call indicator is not ACTIVE.",
        )

      self.logger.info("[REF] Hold call.")
      await ref_hfp_protocol.execute_command("AT+CHLD=2")

      self.logger.info("[DUT] Wait for call state to be HOLDING.")
      await self._wait_for_call_state(telecom_cb, _CallState.HOLDING)

      self.logger.info("[REF] Wait for call state to be HOLDING.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.assertEqual(
            await ag_indicators[_AgIndicator.CALL_HELD].get(),
            hfp.CallHeldAgIndicator.CALL_ON_HOLD_NO_ACTIVE_CALL,
        )

      self.logger.info("[REF] Unhold call.")
      await ref_hfp_protocol.execute_command("AT+CHLD=2")

      self.logger.info("[DUT] Wait for call state to be ACTIVE.")
      await self._wait_for_call_state(telecom_cb, _CallState.ACTIVE)

      self.logger.info("[REF] Wait for call state to be NO_CALLS_HELD.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.assertEqual(
            await ag_indicators[_AgIndicator.CALL_HELD].get(),
            hfp.CallHeldAgIndicator.NO_CALLS_HELD,
        )

  @navi_test_base.named_parameterized(
      from_ag=constants.TestRole.DUT,
      from_hf=constants.TestRole.REF,
  )
  async def test_voice_recognition(self, initiator: constants.TestRole) -> None:
    """Tests voice recognition.

    Test steps:
      1. Setup HFP connection.
      2. Start voice recognition.
      3. Stop voice recognition.

    Args:
      initiator: The initiator of voice recognition process.
    """
    self.logger.info("[REF] Setup HF server.")
    hfp_configuration = hfp.HfConfiguration(
        supported_hf_features=[
            hfp.HfFeature.THREE_WAY_CALLING,
            hfp.HfFeature.VOICE_RECOGNITION_ACTIVATION,
        ],
        supported_hf_indicators=[],
        supported_audio_codecs=[hfp.AudioCodec.CVSD],
    )
    ref_hfp_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_configuration,
    )

    ag_cb = self.dut.bl4a.register_callback(_Module.HFP_AG)
    self.test_case_context.push(ag_cb)

    await self.classic_connect_and_pair(connect_profiles=True)

    self.logger.info("[DUT] Wait for HFP connected.")
    await ag_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
    )

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for HFP connected.")
      ref_hfp_protocol = await ref_hfp_protocol_queue.get()

      self.logger.info("[REF] Wait for SLC initialized.")
      await ref_hfp_protocol.slc_initialized.wait()

    if not ref_hfp_protocol.supports_ag_feature(
        hfp.AgFeature.VOICE_RECOGNITION_FUNCTION
    ):
      self.skipTest("[REF] Doesn't support voice recognition activation.")

    vr_state = [hfp.VoiceRecognitionState.DISABLE]
    condition = asyncio.Condition()

    @ref_hfp_protocol.on(ref_hfp_protocol.EVENT_VOICE_RECOGNITION)
    async def _(state: hfp.VoiceRecognitionState):
      async with condition:
        vr_state[0] = state
        condition.notify_all()

    voice_command_callback = self.dut.bl4a.register_voice_command_callback()
    self.test_case_context.push(voice_command_callback)
    activation_task: asyncio.Task[None] | None = None
    if initiator == constants.TestRole.REF:
      self.logger.info("[REF] Start voice recognition.")
      # Android stack doesn't reply BVRA until
      # BluetoothHeadset.startVoiceRecognition() is called, so it must be
      # executed asynchronously.
      self.logger.info("[REF] Send AT+BVRA=1 to HF.")
      activation_task = asyncio.create_task(
          ref_hfp_protocol.execute_command(
              "AT+BVRA=1", timeout=_DEFAULT_STEP_TIMEOUT_SECONDS
          )
      )

      self.logger.info("[DUT] Wait for voice recognition to be enabled.")
      await voice_command_callback.wait_for_event(
          bl4a_api.VoiceCommand(state=True),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

    # DUT always needs startVoiceRecognition() to:
    #   1. (HF-initiated) Send OK response for AT+BVRA.
    #   2. (AG-initiated) Send +BVRA=1 as AG-initiated voice recognition.
    self.logger.info("[DUT] Start voice recognition.")
    self.dut.bt.hfpAgStartVoiceRecognition(self.ref.address)

    if activation_task:
      self.logger.info("[REF] Wait for AT+BVRA result.")
      await activation_task

    # Only AG-initiated voice recognition should send +BVRA to the HF.
    if initiator == constants.TestRole.DUT:
      self.logger.info("[REF] Wait for voice recognition to be enabled.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        async with condition:
          await condition.wait_for(
              lambda: vr_state[0] == hfp.VoiceRecognitionState.ENABLE
          )

    recorder = await self.dut.bl4a.start_audio_recording(_RECORDING_PATH)
    self.test_case_context.push(recorder)

    self.logger.info("[DUT] Wait for SCO connected.")
    await ag_cb.wait_for_event(
        _HfpAgAudioStateChange(
            address=self.ref.address, state=_ScoState.CONNECTED
        )
    )

    if initiator == constants.TestRole.DUT:
      self.logger.info("[DUT] Stop voice recognition.")
      self.dut.bt.hfpAgStopVoiceRecognition(self.ref.address)

      self.logger.info("[REF] Wait for voice recognition to be disabled.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        async with condition:
          await condition.wait_for(
              lambda: vr_state[0] == hfp.VoiceRecognitionState.DISABLE
          )
    else:
      self.logger.info("[REF] Stop voice recognition.")
      await ref_hfp_protocol.execute_command(
          "AT+BVRA=0", timeout=_DEFAULT_STEP_TIMEOUT_SECONDS
      )
      if self.dut.bluetooth_mainline_version < 361000000:
        self.logger.info(
            "[DUT] Mainline version is too old, skip waiting for voice"
            " recognition to be disabled."
        )
      else:
        self.logger.info("[DUT] Wait for voice recognition to be disabled.")
        await voice_command_callback.wait_for_event(
            bl4a_api.VoiceCommand(state=False),
            timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
        )

    self.logger.info("[DUT] Stop audio recording.")
    recorder.close()

    self.logger.info("[DUT] Wait for SCO disconnected.")
    await ag_cb.wait_for_event(
        _HfpAgAudioStateChange(
            address=self.ref.address, state=_ScoState.DISCONNECTED
        )
    )

  async def test_switch_call_endpoint(self) -> None:
    """Tests switching call endpoint.

    Test steps:
      1. Setup HFP connection.
      2. Place a call.
      3. Switch call endpoint from SCO to Speaker.
      4. Switch call endpoint from Speaker to SCO.
    """
    if (
        self.dut.device.is_emulator
        and self.dut.getprop(android_constants.Property.SCO_MANAGED_BY_AUDIO)
        != "true"
    ):
      self.skipTest(
          "SCO endpoint switch is not supported with non-AMSCO emulator"
      )

    self.logger.info("[REF] Setup HFP server.")
    hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_ext.make_hf_configuration(),
    )

    ag_cb = self.dut.bl4a.register_callback(_Module.HFP_AG)
    self.test_case_context.push(ag_cb)

    await self.classic_connect_and_pair(connect_profiles=True)

    self.logger.info("[DUT] Wait for HFP connected.")
    await ag_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address)
    )

    telecom_cb = self.dut.bl4a.register_callback(_Module.TELECOM)
    self.test_case_context.push(telecom_cb)

    self.logger.info("[DUT] Make outgoing call.")
    call = self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.OUTGOING,
    )
    self.test_case_context.push(call)

    self.logger.info("[DUT] Set repeat mode to one.")
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)

    self.logger.info("[DUT] Set audio attributes for voice communication.")
    self.dut.bl4a.set_audio_attributes(
        bl4a_api.AudioAttributes(
            content_type=bl4a_api.AudioAttributes.ContentType.SPEECH,
            usage=bl4a_api.AudioAttributes.Usage.VOICE_COMMUNICATION,
        ),
        handle_audio_focus=False,
    )

    self.logger.info("[DUT] Play sine wave.")
    self.dut.bt.audioPlaySine()

    self.logger.info("[DUT] Wait for SCO connected.")
    await ag_cb.wait_for_event(
        _HfpAgAudioStateChange(
            address=self.ref.address, state=_ScoState.CONNECTED
        )
    )

    self.logger.info("[DUT] Wait for 1 second.")
    await asyncio.sleep(1.0)

    self.logger.info("[DUT] Switch call endpoint to SPEAKER.")
    await call.request_call_endpoint_switch(
        endpoint_type=android_constants.CallEndpointType.SPEAKER
    )

    self.logger.info("[DUT] Wait for SCO disconnected.")
    await ag_cb.wait_for_event(
        _HfpAgAudioStateChange(
            address=self.ref.address, state=_ScoState.DISCONNECTED
        )
    )

    self.logger.info("[DUT] Wait for 1 second.")
    await asyncio.sleep(1.0)

    self.logger.info("[DUT] Switch call endpoint to SCO.")
    await call.request_call_endpoint_switch(
        endpoint_type=android_constants.CallEndpointType.BLUETOOTH
    )

    self.logger.info("[DUT] Wait for SCO connected.")
    await ag_cb.wait_for_event(
        _HfpAgAudioStateChange(
            address=self.ref.address, state=_ScoState.CONNECTED
        )
    )

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

  async def test_dial_outgoing_call_from_ref(self) -> None:
    """Tests outgoing dial from REF (ATD).

    Test steps:
      1. Set default dialer to snippet on DUT.
      2. Enable and configure mock connection service on DUT.
      3. Connect and pair REF (HFP connected).
      4. Register Telecom callback on DUT.
      5. Dial phone number from REF using ATD.
      6. Verify call dialing event on DUT.
    """
    self._setup_mock_connection_service()

    self.logger.info("[REF] Setup HFP server.")
    ref_hfp_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_ext.make_hf_configuration(),
    )

    ag_cb = self.dut.bl4a.register_callback(_Module.HFP_AG)
    telecom_cb = self.dut.bl4a.register_callback(_Module.TELECOM)
    self.test_case_context.push(ag_cb)
    self.test_case_context.push(telecom_cb)

    await self.classic_connect_and_pair(connect_profiles=True)

    self.logger.info("[DUT] Wait for HFP connected.")
    await ag_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
    )

    self.logger.info("[REF] Wait for HFP connected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_hfp_protocol = await ref_hfp_protocol_queue.get()

      self.logger.info("[REF] Wait for SLC initialized.")
      await ref_hfp_protocol.slc_initialized.wait()

    dial_number = "123456789"
    self.logger.info("[REF] Send ATD%s;", dial_number)
    dial_task = asyncio.create_task(
        ref_hfp_protocol.execute_command(f"ATD{dial_number};")
    )

    self.logger.info("[DUT] Wait for call dialing event.")
    await telecom_cb.wait_for_event(
        bl4a_api.CallStateChanged(
            handle=f"tel:{dial_number}",
            state=android_constants.CallState.DIALING,
            name=mock.ANY,
        ),
    )

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for dial task to complete.")
      await dial_task

  async def test_last_number_redial_from_ref(self) -> None:
    """Tests last number redial from REF (AT+BLDN).

    Test steps:
      1. Set default dialer to snippet on DUT.
      2. Enable and configure mock connection service on DUT.
      3. Connect and pair REF (HFP connected).
      4. Register Telecom callback on DUT.
      5. Inject an outgoing call log to call log database on DUT.
      6. Redial from REF using AT+BLDN.
      7. Verify call dialing event on DUT.
    """
    self._setup_mock_connection_service()

    self.logger.info("[REF] Setup HFP server.")
    ref_hfp_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_ext.make_hf_configuration(),
    )

    ag_cb = self.dut.bl4a.register_callback(_Module.HFP_AG)
    telecom_cb = self.dut.bl4a.register_callback(_Module.TELECOM)
    self.test_case_context.push(ag_cb)
    self.test_case_context.push(telecom_cb)

    # Clean and inject call logs.
    self.logger.info("[DUT] Clear call logs.")
    self.dut.bt.clearCallLogs()
    self.test_case_context.callback(self.dut.bt.clearCallLogs)

    redial_number = "987654321"
    self.logger.info("[DUT] Add outgoing call log for %s.", redial_number)
    self.dut.bt.addCallLogs([{
        "number": redial_number,
        "name": "Last Dialer Test",
        "date": 1727273204595,
        "duration": 10000,
        "call_type": 2,  # OUTGOING_TYPE
    }])

    await self.classic_connect_and_pair(connect_profiles=True)

    self.logger.info("[DUT] Wait for HFP connected.")
    await ag_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
    )

    self.logger.info("[REF] Wait for HFP connected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_hfp_protocol = await ref_hfp_protocol_queue.get()

      self.logger.info("[REF] Wait for SLC initialized.")
      await ref_hfp_protocol.slc_initialized.wait()

    self.logger.info("[REF] Send AT+BLDN.")
    redial_task = asyncio.create_task(
        ref_hfp_protocol.execute_command("AT+BLDN")
    )

    self.logger.info("[DUT] Wait for call dialing event.")
    await telecom_cb.wait_for_event(
        bl4a_api.CallStateChanged(
            handle=f"tel:{redial_number}",
            state=android_constants.CallState.DIALING,
            name=mock.ANY,
        ),
    )

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for redial task to complete.")
      await redial_task

  async def test_sink_audio_policy_connecting_policy_not_allowed(self) -> None:
    """Tests sink audio policy not allowed for connecting time.

    Test steps:
      1. Setup HFP connection.
      2. Verify REF is set as active device on DUT.
      3. REF sends sink audio policy to NOT_ALLOWED for connecting time.
      4. Verify DUT removes REF from active device.
      5. Disconnect HFP.
      6. Reconnect HFP.
      7. Verify REF is not set active on reconnect.
    """
    self.logger.info("[REF] Setup HF server.")
    hfp_configuration = hfp.HfConfiguration(
        supported_hf_features=[],
        supported_hf_indicators=[],
        supported_audio_codecs=[],
    )
    ref_hfp_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.ref.device,
        sdp_handle=_HFP_SDP_HANDLE,
        configuration=hfp_configuration,
    )

    ag_cb = self.dut.bl4a.register_callback(_Module.HFP_AG)
    self.test_case_context.push(ag_cb)

    await self.classic_connect_and_pair(connect_profiles=True)

    self.logger.info("[DUT] Wait for HFP connected and active.")
    await ag_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
    )

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for HFP connected.")
      ref_hfp_protocol = await ref_hfp_protocol_queue.get()

      self.logger.info("[REF] Wait for SLC initialized.")
      await ref_hfp_protocol.slc_initialized.wait()

    self.logger.info(
        "[REF] Send SINKAUDIOPOLICY: UNCONFIGURED, NOT_ALLOWED, UNCONFIGURED"
    )
    await ref_hfp_protocol.set_sink_audio_policy(
        call_establish=hfp_ext.SinkAudioPolicy.UNCONFIGURED,
        connecting_time=hfp_ext.SinkAudioPolicy.NOT_ALLOWED,
        in_band_ringtone=hfp_ext.SinkAudioPolicy.UNCONFIGURED,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    self.logger.info("[DUT] Wait for active device to become null.")
    await ag_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=None),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    self.logger.info("[DUT] Disconnect HFP.")
    await self.disconnect_with_check(
        self.ref.address, android_constants.Transport.CLASSIC
    )

    self.logger.info("[DUT] Reconnect HFP.")
    self.dut.bt.connect(self.ref.address)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for HFP reconnected and SLC initialized.")
      ref_hfp_protocol2 = await ref_hfp_protocol_queue.get()
      await ref_hfp_protocol2.slc_initialized.wait()

    self.logger.info("[DUT] Verify REF is not set active on reconnect")
    with self.assertRaises(errors.AsyncTimeoutError):
      await ag_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )


if __name__ == "__main__":
  test_runner.main()
