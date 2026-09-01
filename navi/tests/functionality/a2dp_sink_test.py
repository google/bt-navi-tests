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
from collections.abc import Iterable, Sequence
import dataclasses
import decimal

from bumble import a2dp
from bumble import avc
from bumble import avdtp
from bumble import avrcp
from bumble import core
from bumble import device
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import a2dp as a2dp_ext
from navi.bumble_ext import avrcp as avrcp_ext
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api

_A2DP_SERVICE_RECORD_HANDLE = 1
_AVRCP_TARGET_RECORD_HANDLE = 2

_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_DEFAULT_STREAM_DURATION_SECONDS = 1.0
_MEDIA_BROWSER_SERVICE_NAME = "BluetoothMediaBrowserService"
_FEATURE_AUTOMOTIVE = "android.hardware.type.automotive"

_Property = android_constants.Property
_SAMPLE_TRACK = avrcp_ext.MediaElement(
    media_element_uid=1,
    displayable_name="Test Media Element",
    attributes={
        avrcp.MediaAttributeId.TITLE: "Test Media Element",
        avrcp.MediaAttributeId.ARTIST_NAME: "Test Artist",
        avrcp.MediaAttributeId.ALBUM_NAME: "Test Album",
    },
)
_SAMPLE_FOLDER = avrcp_ext.Folder(
    folder_uid=1,
    is_playable=True,
    displayable_name="Test Folder",
    children=[_SAMPLE_TRACK],
)
_SAMPLE_ROOT_FOLDER = avrcp_ext.Folder(
    folder_uid=0,
    is_playable=False,
    displayable_name="root",
    children=[_SAMPLE_FOLDER],
)
_SAMPLE_PLAYER = avrcp_ext.Player(
    player_id=1,
    feature_bitmask=avrcp.MediaPlayerItem.Features(
        avrcp.MediaPlayerItem.Features.BROWSING
    ),
    displayable_name="Test Player",
    root_folder=_SAMPLE_ROOT_FOLDER,
)
_STREAM_TYPE_MUSIC = android_constants.StreamType.MUSIC


class A2dpSinkTest(navi_test_base.TwoDevicesTestBase):
  """Tests A2DP Sink and AVRCP Controller profiles."""

  bluetooth_package: str
  bluetooth_browser_service: str

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if self.dut.device.is_emulator:
      self.setprop_for_class_context(_Property.A2DP_SINK_ENABLED, "true")

      self.setprop_for_class_context(_Property.AVRCP_CONTROLLER_ENABLED, "true")

    if self.dut.getprop(_Property.A2DP_SINK_ENABLED) != "true":
      raise signals.TestAbortClass("A2DP Sink is not enabled on DUT.")
    if self.dut.getprop(_Property.AVRCP_CONTROLLER_ENABLED) != "true":
      raise signals.TestAbortClass("AVRCP Controller is not enabled on DUT.")

    # The bluetooth package name might be different on different DUTs.
    component = self.dut.shell(
        "pm query-services -a android.media.browse.MediaBrowserService --brief"
        f" | grep {_MEDIA_BROWSER_SERVICE_NAME}"
    )
    if not component:
      self.fail("No media browser service found")
    component_line = component.strip().splitlines()[0]
    self.bluetooth_package, self.bluetooth_browser_service = (
        component_line.strip().split("/")
    )
    if self.bluetooth_browser_service.startswith("."):
      self.bluetooth_browser_service = (
          self.bluetooth_package + self.bluetooth_browser_service
      )

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    # Setup SDP service records.
    self.ref.device.sdp_service_records = {
        _A2DP_SERVICE_RECORD_HANDLE: (
            a2dp_ext.SourceSdpRecord(
                _A2DP_SERVICE_RECORD_HANDLE
            ).to_service_attributes()
        ),
        _AVRCP_TARGET_RECORD_HANDLE: (
            avrcp.TargetServiceSdpRecord(
                _AVRCP_TARGET_RECORD_HANDLE,
                supported_features=(
                    avrcp.TargetFeatures.CATEGORY_1
                    | avrcp.TargetFeatures.SUPPORTS_BROWSING
                ),
            ).to_service_attributes()
        ),
    }

  @dataclasses.dataclass
  class SourceDevice:
    avdtp_protocol_queue: asyncio.Queue[avdtp.Protocol]
    avrcp_protocol: avrcp.Protocol
    avrcp_protocol_starts: asyncio.Queue[None]
    browsing_target_queue: asyncio.Queue[avrcp_ext.BrowsingTarget]

  def _capabilities_support_codec(
      self,
      capabilities: Iterable[avdtp.ServiceCapabilities],
      codec: a2dp_ext.A2dpCodec,
  ) -> bool:
    """Checks if the given capabilities support the codec."""
    for cap in capabilities:
      if not isinstance(cap, avdtp.MediaCodecCapabilities):
        continue
      if cap.media_codec_type != codec.codec_type:
        continue
      # If standard codec, type match is enough
      if codec.codec_type != a2dp.CodecType.NON_A2DP:
        return True
      # If vendor codec, check vendor/codec ID
      info = cap.media_codec_information
      if (
          isinstance(info, a2dp.VendorSpecificMediaCodecInformation)
          and info.vendor_id == codec.vendor_id
          and info.codec_id == codec.codec_id
      ):
        return True
    return False

  async def _reconnect_from_ref_to_dut(
      self,
      ref_a2dp_source_device: SourceDevice,
      codec: a2dp_ext.A2dpCodec = a2dp_ext.A2dpCodec.SBC,
  ) -> None:
    """Actively initiates AVDTP and AVRCP connection from REF."""
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Connecting back to DUT.")
      ref_acl = await self.ref.device.connect(
          self.dut.address,
          core.BT_BR_EDR_TRANSPORT,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )
      await ref_acl.authenticate()
      await ref_acl.encrypt()

      self.logger.info("[REF] Initiating AVDTP connection.")
      avdtp_protocol = await avdtp.Protocol.connect(ref_acl)

      local_source = avdtp_protocol.add_source(
          codec.get_default_capabilities(),
          codec.get_media_packet_pump(avdtp_protocol.l2cap_channel.peer_mtu),
      )

      self.logger.info("[REF] Discover remote endpoints (fast)")
      discover_response = await avdtp_protocol.send_command(
          avdtp.Discover_Command()
      )

      # We don't retrieve capabilities for all SEPs. Instead, we proceed with
      # SetConfiguration immediately after finding the SEP for the target codec.
      # This is because the phone, as an A2DP sink, supports up to 10 codec
      # items, and AOSP imposes a 2-second timeout for AVDTP signaling channel
      # setup. Retrieving capabilities for all items would exceed this timeout
      # and cause connection failure.

      # TODO: Get capabilities for all SEPs if b/495367526 fixed.
      for endpoint_entry in discover_response.endpoints:
        if endpoint_entry.tsep != avdtp.AVDTP_TSEP_SNK:
          continue

        self.logger.info(
            "[REF] Found SNK endpoint %d, fetching capabilities",
            endpoint_entry.seid,
        )
        get_cap_response = await avdtp_protocol.get_capabilities(
            endpoint_entry.seid
        )

        if self._capabilities_support_codec(
            get_cap_response.capabilities, codec
        ):
          target_endpoint_seid = endpoint_entry.seid
          target_capabilities = get_cap_response.capabilities
          break
      else:
        self.fail(f"No remote SNK endpoint with {codec.name} found")

      target_endpoint = avdtp.StreamEndPointProxy(
          avdtp_protocol, target_endpoint_seid
      )

      self.logger.info("[REF] Create and configure stream")
      if not (
          remote_caps := next(
              (
                  cap
                  for cap in target_capabilities
                  if isinstance(cap, avdtp.MediaCodecCapabilities)
                  and cap.media_codec_type == codec.codec_type
              ),
              None,
          )
      ):
        self.fail(f"No remote media codec capabilities with {codec.name} found")

      configuration = a2dp_ext.select_configuration(codec, remote_caps)

      self.logger.info("[REF] Set configuration")
      await target_endpoint.set_configuration(local_source.seid, configuration)

      local_source.configuration = configuration
      stream = avdtp.Stream(avdtp_protocol, local_source, target_endpoint)
      avdtp_protocol.streams[target_endpoint_seid] = stream
      stream.change_state(avdtp.State.CONFIGURED)

      self.logger.info("[REF] Open stream")
      await stream.open()

      self.logger.info("[REF] Connect AVRCP.")
      await ref_a2dp_source_device.avrcp_protocol.connect(ref_acl)

  def _setup_a2dp_source_device(
      self,
      bumble_device: device.Device,
      codecs: Sequence[a2dp_ext.A2dpCodec] = (
          a2dp_ext.A2dpCodec.SBC,
          a2dp_ext.A2dpCodec.AAC,
      ),
  ) -> SourceDevice:
    # Setup AVDTP server.
    avdtp_protocol_queue = asyncio.Queue[avdtp.Protocol]()
    avdtp_listener = avdtp.Listener.for_device(device=bumble_device)

    def on_avdtp_connection(protocol: avdtp.Protocol) -> None:
      for codec in codecs:
        protocol.add_source(
            codec.get_default_capabilities(),
            codec.get_media_packet_pump(protocol.l2cap_channel.peer_mtu),
        )
      avdtp_protocol_queue.put_nowait(protocol)

    avdtp_listener.on(avdtp_listener.EVENT_CONNECTION, on_avdtp_connection)
    # Setup AVRCP server.
    avrcp_delegate = avrcp.Delegate()
    avrcp_protocol_starts = asyncio.Queue[None]()
    avrcp_protocol = avrcp.Protocol(delegate=avrcp_delegate)
    avrcp_protocol.listen(bumble_device)
    avrcp_protocol.on(
        avrcp_protocol.EVENT_START,
        lambda: avrcp_protocol_starts.put_nowait(None),
    )
    browsing_target_queue = avrcp_ext.BrowsingTarget.listen(
        bumble_device,
        players=[_SAMPLE_PLAYER],
    )
    return self.SourceDevice(
        avdtp_protocol_queue=avdtp_protocol_queue,
        avrcp_protocol=avrcp_protocol,
        avrcp_protocol_starts=avrcp_protocol_starts,
        browsing_target_queue=browsing_target_queue,
    )

  async def test_paired_connect_outgoing(self) -> None:
    """Tests A2DP connection establishment right after a pairing session.

    Test steps:
      1. Connect and pair REF.
      2. Make A2DP and AVRCP connection from DUT.
    """
    ref_a2dp_source_device = self._setup_a2dp_source_device(self.ref.device)

    dut_a2dp_sink_callback = self.dut.bl4a.register_callback(
        bl4a_api.Module.A2DP_SINK
    )
    self.test_case_context.push(dut_a2dp_sink_callback)

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVDTP connection")
      avdtp_protocol = await ref_a2dp_source_device.avdtp_protocol_queue.get()
      self.logger.info("[REF] Discover remote endpoints")
      await avdtp_protocol.discover_remote_endpoints()
      self.logger.info("[REF] Wait for AVRCP connection")
      await ref_a2dp_source_device.avrcp_protocol_starts.get()

    self.logger.info("[DUT] Waiting for A2DP connection state changed.")
    await dut_a2dp_sink_callback.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.CONNECTED,
        )
    )

  async def test_paired_connect_incoming(self) -> None:
    """Tests A2DP and AVRCP connection actively initiated by REF.

    Test steps:
      1. Connect and pair DUT to REF.
      2. Verify DUT is connected to REF A2DP Source and AVRCP Target.
      3. Disconnect DUT and REF from DUT.
      4. REF actively initiates AVDTP and AVRCP connection.
    """
    ref_a2dp_source_device = self._setup_a2dp_source_device(self.ref.device)

    dut_a2dp_sink_callback = self.dut.bl4a.register_callback(
        bl4a_api.Module.A2DP_SINK
    )
    self.test_case_context.push(dut_a2dp_sink_callback)

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVDTP connection")
      avdtp_protocol = await ref_a2dp_source_device.avdtp_protocol_queue.get()
      self.logger.info("[REF] Discover remote endpoints")
      await avdtp_protocol.discover_remote_endpoints()
      self.logger.info("[REF] Wait for AVRCP connection")
      await ref_a2dp_source_device.avrcp_protocol_starts.get()

    self.logger.info("[DUT] Wait for A2DP connected.")
    await dut_a2dp_sink_callback.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.CONNECTED,
        )
    )

    self.logger.info("[DUT] Disconnect with check.")
    await self.disconnect_with_check(
        self.ref.address, android_constants.Transport.CLASSIC
    )

    await self._reconnect_from_ref_to_dut(
        ref_a2dp_source_device, codec=a2dp_ext.A2dpCodec.SBC
    )

    self.logger.info("[DUT] Wait for A2DP connected.")
    await dut_a2dp_sink_callback.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.CONNECTED,
        )
    )

  @navi_test_base.named_parameterized(
      sbc=dict(
          codec=a2dp_ext.A2dpCodec.SBC, a2dp_codec_type=a2dp.CodecType.SBC
      ),
      aac=dict(
          codec=a2dp_ext.A2dpCodec.AAC,
          a2dp_codec_type=a2dp.CodecType.MPEG_2_4_AAC,
      ),
      opus=dict(
          codec=a2dp_ext.A2dpCodec.OPUS,
          a2dp_codec_type=a2dp.CodecType.NON_A2DP,
          vendor_id=a2dp.OpusMediaCodecInformation.VENDOR_ID,
          codec_id=a2dp.OpusMediaCodecInformation.CODEC_ID,
      ),
  )
  async def test_streaming(
      self,
      codec: a2dp_ext.A2dpCodec,
      a2dp_codec_type: a2dp.CodecType,
      vendor_id: int = 0,
      codec_id: int = 0,
  ) -> None:
    """Tests streaming.

    Test steps:
      1. Connect and pair REF.
      2. Make A2DP and AVRCP connection from DUT.
      3. Start streaming.
      4. Stop streaming.

    Args:
      codec: The codec to use for streaming.
      a2dp_codec_type: The codec type to use for streaming.
      vendor_id: The vendor ID of the codec.
      codec_id: The codec ID of the codec.
    """

    ref_a2dp_source_device = self._setup_a2dp_source_device(
        self.ref.device, codecs=list(set([a2dp_ext.A2dpCodec.SBC, codec]))
    )

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVDTP connection")
      avdtp_protocol = await ref_a2dp_source_device.avdtp_protocol_queue.get()
      self.logger.info("[REF] Discover remote endpoints")
      await avdtp_protocol.discover_remote_endpoints()

    sources = a2dp_ext.find_local_endpoints_by_codec(
        avdtp_protocol, a2dp_codec_type, avdtp.LocalSource, vendor_id, codec_id
    )
    if not sources:
      self.fail(f"No A2DP local {codec.name} source found")

    if not (stream := sources[0].stream):
      # If there is only one source, DUT will automatically create a stream.
      self.fail("REF doesn't create a stream")

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Start stream")
      await stream.start()

    await asyncio.sleep(_DEFAULT_STREAM_DURATION_SECONDS)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Stop stream")
      await stream.stop()

  async def test_browsing(self) -> None:
    """Tests AVRCP Browsing.

    Test steps:
      1. Connect and pair REF.
      2. Make A2DP, AVRCP and Browsing connection from DUT.
      3. Browse remote devices, players, folders and media elements.
    """

    ref_a2dp_source_device = self._setup_a2dp_source_device(self.ref.device)

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVRCP connection")
      await ref_a2dp_source_device.avrcp_protocol_starts.get()
      self.logger.info("[REF] Wait for Browsing connection")
      await ref_a2dp_source_device.browsing_target_queue.get()

    self.logger.info("[DUT] Connect to media browser")
    browser = self.dut.bl4a.connect_media_browser(
        self.bluetooth_package,
        self.bluetooth_browser_service,
    )
    self.test_case_context.push(browser)
    self.logger.info("[DUT] Get media browser root id")
    root_id = await browser.get_root_media_item()

    self.logger.info("[DUT] Browse remote devices")
    children = await browser.get_children(root_id)
    self.assertLen(children, 1)
    self.assertEqual(children[0].title, self.ref.device.name)
    device_node = children[0]
    assert device_node.id is not None

    self.logger.info("[DUT] Browse players")
    children = await browser.get_children(device_node.id)
    self.assertLen(children, 1)
    self.assertEqual(children[0].title, _SAMPLE_PLAYER.displayable_name)
    player_node = children[0]
    assert player_node.id is not None

    self.logger.info("[DUT] Browse folders")
    children = await browser.get_children(player_node.id)
    self.assertLen(children, 1)
    self.assertEqual(children[0].title, _SAMPLE_FOLDER.displayable_name)
    folder_node = children[0]
    assert folder_node.id is not None

    self.logger.info("[DUT] Browse media elements")
    children = await browser.get_children(folder_node.id)
    self.assertLen(children, 1)
    self.assertEqual(children[0].title, _SAMPLE_TRACK.displayable_name)

  @navi_test_base.TwoDevicesTestBase.require_flag(
      "com.android.bluetooth.flags.avrcp_controller_abs_vol_changed_notification"
  )
  async def test_set_volume_from_dut(self) -> None:
    """Tests AVRCP set absolute volume.

    Test steps:
      1. Connect and pair REF.
      2. Make A2DP and AVRCP connection from DUT.
      3. Set volume from DUT.
      4. Verify the volume is changed on DUT and REF.
    """

    if self.dut.bt.isVolumeFixed():
      self.skipTest("Volume is fixed by manager")
    if _FEATURE_AUTOMOTIVE in self.dut.shell("pm list features"):
      self.skipTest("Volume is fixed on automotive")

    ref_a2dp_source_device = self._setup_a2dp_source_device(self.ref.device)

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVRCP connection")
      await ref_a2dp_source_device.avrcp_protocol_starts.get()

    ref_avrcp_protocol = ref_a2dp_source_device.avrcp_protocol
    volume_iter = ref_avrcp_protocol.monitor_volume()
    audio_cb = self.dut.bl4a.register_callback(bl4a_api.Module.AUDIO)
    self.test_case_context.push(audio_cb)

    dut_min_volume = self.dut.bt.getMinVolume(_STREAM_TYPE_MUSIC)
    dut_max_volume = self.dut.bt.getMaxVolume(_STREAM_TYPE_MUSIC)

    for volume in range(dut_min_volume, dut_max_volume + 1):
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        if self.dut.bt.getVolume(_STREAM_TYPE_MUSIC) == volume:
          continue

        if self.dut.bluetooth_mainline_version < 361611000:
          ref_expected_volume = (
              volume * avrcp.SetAbsoluteVolumeCommand.MAXIMUM_VOLUME
          ) // dut_max_volume
        else:
          ref_expected_volume = int(
              decimal.Decimal(
                  (volume * avrcp.SetAbsoluteVolumeCommand.MAXIMUM_VOLUME)
                  / dut_max_volume
              ).to_integral_exact(rounding=decimal.ROUND_HALF_UP)
          )

        self.logger.info("[REF] Wait for volume interim")
        await anext(volume_iter)

        self.logger.info("[DUT] Set volume to %d", volume)
        self.dut.bt.setVolume(_STREAM_TYPE_MUSIC, volume)

        self.logger.info("[DUT] Wait for volume changed")
        await audio_cb.wait_for_event(
            bl4a_api.VolumeChanged(
                stream_type=_STREAM_TYPE_MUSIC,
                volume_value=volume,
            )
        )
        self.logger.info("[REF] Wait for volume changed")
        self.assertEqual(await anext(volume_iter), ref_expected_volume)

  async def test_set_volume_from_ref(self) -> None:
    """Tests AVRCP set absolute volume.

    Test steps:
      1. Connect and pair REF.
      2. Make A2DP and AVRCP connection from DUT.
      3. Set volume from REF.
      4. Verify the volume is changed on DUT.
    """

    if self.dut.bt.isVolumeFixed():
      self.skipTest("Volume is fixed by manager")
    if _FEATURE_AUTOMOTIVE in self.dut.shell("pm list features"):
      self.skipTest("Volume is fixed on automotive")

    ref_a2dp_source_device = self._setup_a2dp_source_device(self.ref.device)

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVRCP connection")
      await ref_a2dp_source_device.avrcp_protocol_starts.get()

    ref_avrcp_protocol = ref_a2dp_source_device.avrcp_protocol
    audio_cb = self.dut.bl4a.register_callback(bl4a_api.Module.AUDIO)
    self.test_case_context.push(audio_cb)

    dut_max_volume = self.dut.bt.getMaxVolume(_STREAM_TYPE_MUSIC)

    for volume in range(
        0, avrcp.SetAbsoluteVolumeCommand.MAXIMUM_VOLUME + 1, 8
    ):
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        if self.dut.bluetooth_mainline_version < 361611000:
          dut_expected_volume = (
              volume * dut_max_volume
          ) // avrcp.SetAbsoluteVolumeCommand.MAXIMUM_VOLUME
        else:
          dut_expected_volume = int(
              decimal.Decimal(
                  (volume * dut_max_volume)
                  / avrcp.SetAbsoluteVolumeCommand.MAXIMUM_VOLUME
              ).to_integral_exact(rounding=decimal.ROUND_HALF_UP)
          )

        if self.dut.bt.getVolume(_STREAM_TYPE_MUSIC) == dut_expected_volume:
          continue

        self.logger.info("[REF] Set volume to %d", volume)
        await ref_avrcp_protocol.send_avrcp_command(
            avc.CommandFrame.CommandType.CONTROL,
            avrcp.SetAbsoluteVolumeCommand(volume),
        )

        self.logger.info("[DUT] Wait for volume changed")
        await audio_cb.wait_for_event(
            bl4a_api.VolumeChanged(
                stream_type=_STREAM_TYPE_MUSIC,
                volume_value=dut_expected_volume,
            )
        )

  async def test_playback_control(self) -> None:
    """Tests AVRCP playback control.

    Test steps:
      1. Connect and pair REF.
      2. Make A2DP and AVRCP connection from DUT.
      3. Play media on DUT and check if REF receives the PLAY command.
      4. Pause media on DUT and check if REF receives the PAUSE command.
      5. Stop media on DUT and check if REF receives the STOP command.
      6. Skip to next media on DUT and check if REF receives the FORWARD
      command.
      7. Skip to previous media on DUT and check if REF receives the BACKWARD
         command.
      8. Fast forward media on DUT and check if REF receives the FAST_FORWARD
         command.
      9. Rewind media on DUT and check if REF receives the REWIND command.
    """

    ref_a2dp_source_device = self._setup_a2dp_source_device(
        self.ref.device, codecs=[a2dp_ext.A2dpCodec.SBC]
    )

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVDTP connection")
      avdtp_protocol = await ref_a2dp_source_device.avdtp_protocol_queue.get()
      self.logger.info("[REF] Discover remote endpoints")
      await avdtp_protocol.discover_remote_endpoints()
      self.logger.info("[REF] Wait for AVRCP connection")
      await ref_a2dp_source_device.avrcp_protocol_starts.get()

    ref_avrcp_protocol = ref_a2dp_source_device.avrcp_protocol
    key_events = asyncio.Queue[tuple[avc.PassThroughFrame.OperationId, bool]]()

    class Delegate(avrcp.Delegate):

      @override
      async def on_key_event(
          self,
          key: avc.PassThroughFrame.OperationId,
          pressed: bool,
          data: bytes,
      ) -> None:
        key_events.put_nowait((key, pressed))

    ref_avrcp_protocol.delegate = Delegate()

    self.logger.info("[DUT] Connect to media browser")
    browser = self.dut.bl4a.connect_media_browser(
        self.bluetooth_package,
        self.bluetooth_browser_service,
    )
    self.test_case_context.push(browser)

    for playback_control_method, expected_key in [
        (browser.play, avc.PassThroughFrame.OperationId.PLAY),
        (browser.pause, avc.PassThroughFrame.OperationId.PAUSE),
        (browser.stop, avc.PassThroughFrame.OperationId.STOP),
        (browser.skip_to_next, avc.PassThroughFrame.OperationId.FORWARD),
        (browser.skip_to_previous, avc.PassThroughFrame.OperationId.BACKWARD),
    ]:
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.logger.info("[DUT] %s", playback_control_method.__name__)
        playback_control_method()
        self.logger.info("[REF] Wait for %r pressed", expected_key)
        actual_key, actual_pressed = await key_events.get()
        self.assertTrue(actual_pressed)
        self.assertEqual(actual_key, expected_key)
        self.logger.info("[REF] Wait for %r released", expected_key)
        actual_key, actual_pressed = await key_events.get()
        self.assertFalse(actual_pressed)
        self.assertEqual(actual_key, expected_key)

    # Holdable keys, need to call twice to release the key.
    for playback_control_method, expected_key in [
        (browser.fast_forward, avc.PassThroughFrame.OperationId.FAST_FORWARD),
        (browser.rewind, avc.PassThroughFrame.OperationId.REWIND),
    ]:
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.logger.info("[DUT] %s", playback_control_method.__name__)
        playback_control_method()
        self.logger.info("[REF] Wait for %r pressed", expected_key)
        actual_key, actual_pressed = await key_events.get()
        self.assertTrue(actual_pressed)
        self.assertEqual(actual_key, expected_key)

        self.logger.info("[DUT] %s", playback_control_method.__name__)
        playback_control_method()
        self.logger.info("[REF] Wait for %r released", expected_key)
        actual_key, actual_pressed = await key_events.get()
        self.assertFalse(actual_pressed)
        self.assertEqual(actual_key, expected_key)

  async def test_playback_status(self) -> None:
    """Tests AVRCP playback status.

    Test steps:
      1. Connect and pair REF.
      2. Make A2DP and AVRCP connection from DUT.
      3. Play media on DUT and check if REF receives the PLAYBACK_STATUS_CHANGED
         event with PLAYING state.
      4. Pause media on DUT and check if REF receives the
         PLAYBACK_STATUS_CHANGED event with PAUSED state.
      5. Stop media on DUT and check if REF receives the PLAYBACK_STATUS_CHANGED
         event with STOPPED state.
      6. Fast forward media on DUT and check if REF receives the
         PLAYBACK_STATUS_CHANGED event with FAST_FORWARD state.
      7. Rewind media on DUT and check if REF receives the
          PLAYBACK_STATUS_CHANGED event with REWIND state.
    """

    ref_a2dp_source_device = self._setup_a2dp_source_device(
        self.ref.device, codecs=[a2dp_ext.A2dpCodec.SBC]
    )
    ref_avrcp_protocol = ref_a2dp_source_device.avrcp_protocol
    ref_avrcp_protocol.delegate = avrcp.Delegate(
        supported_events=[avrcp.EventId(avrcp.EventId.PLAYBACK_STATUS_CHANGED)]
    )

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVRCP connection")
      await ref_a2dp_source_device.avrcp_protocol_starts.get()

    self.logger.info("[DUT] Connect to media browser")
    browser = self.dut.bl4a.connect_media_browser(
        self.bluetooth_package,
        self.bluetooth_browser_service,
    )
    self.test_case_context.push(browser)
    self.logger.info("[DUT] Register media controller callback")
    callback = browser.register_callback()
    self.test_case_context.push(callback)

    for ref_playback_status, dut_playback_state in [
        (
            avrcp.PlayStatus.PLAYING,
            android_constants.MediaPlaybackState.PLAYING,
        ),
        (avrcp.PlayStatus.PAUSED, android_constants.MediaPlaybackState.PAUSED),
        (
            avrcp.PlayStatus.STOPPED,
            android_constants.MediaPlaybackState.STOPPED,
        ),
        (
            avrcp.PlayStatus.FWD_SEEK,
            android_constants.MediaPlaybackState.FAST_FORWARDING,
        ),
        (
            avrcp.PlayStatus.REV_SEEK,
            android_constants.MediaPlaybackState.REWINDING,
        ),
    ]:
      ref_playback_status = avrcp.PlayStatus(ref_playback_status)

      self.logger.info(
          "[REF] Notify playback status changed to %r", ref_playback_status
      )
      ref_avrcp_protocol.delegate.playback_status = ref_playback_status
      ref_avrcp_protocol.notify_playback_status_changed(ref_playback_status)

      self.logger.info("[DUT] Wait for playback state changed event")
      await callback.wait_for_event(
          bl4a_api.MediaBrowser.PlaybackStateChanged(state=dut_playback_state)
      )

  async def test_media_metadata(self) -> None:
    """Tests AVRCP media metadata changed and get item attributes.

    Android passively receives the media metadata changed event and actively
    requests the media metadata using get item attributes when the media is
    changed.

    Test steps:
      1. Connect and pair REF.
      2. Make A2DP and AVRCP connection from DUT.
      3. Set now playing item on REF.
      4. Verify the media metadata is changed on DUT.
      5. Get item attributes on DUT and verify the media metadata.
    """

    ref_a2dp_source_device = self._setup_a2dp_source_device(self.ref.device)
    ref_avrcp_protocol = ref_a2dp_source_device.avrcp_protocol
    ref_avrcp_protocol.delegate = avrcp.Delegate(
        supported_events=[
            avrcp.EventId.TRACK_CHANGED,
            avrcp.EventId.AVAILABLE_PLAYERS_CHANGED,
        ]
    )

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVRCP connection")
      await ref_a2dp_source_device.avrcp_protocol_starts.get()
      self.logger.info("[REF] Wait for Browsing connection")
      ref_browsing_target = (
          await ref_a2dp_source_device.browsing_target_queue.get()
      )

    self.logger.info("[DUT] Connect to media browser")
    browser = self.dut.bl4a.connect_media_browser(
        self.bluetooth_package,
        self.bluetooth_browser_service,
    )
    self.test_case_context.push(browser)
    self.logger.info("[DUT] Register media controller callback")
    callback = browser.register_callback()
    self.test_case_context.push(callback)

    self.logger.info("[REF] Set browsed player")
    ref_browsed_player = ref_browsing_target.browsed_player = (
        ref_browsing_target.players[0]
    )
    self.logger.info("[REF] Set now playing item")
    ref_browsed_player.now_playing_items = [_SAMPLE_TRACK]
    ref_avrcp_protocol.delegate.current_track_uid = (
        _SAMPLE_TRACK.media_element_uid
    )
    ref_avrcp_protocol.notify_track_changed(_SAMPLE_TRACK.media_element_uid)

    self.logger.info("[DUT] Wait for metadata changed event")
    await callback.wait_for_event(
        bl4a_api.MediaBrowser.MetadataChanged(
            title=_SAMPLE_TRACK.attributes[avrcp.MediaAttributeId.TITLE],
            artist=_SAMPLE_TRACK.attributes[avrcp.MediaAttributeId.ARTIST_NAME],
            album=_SAMPLE_TRACK.attributes[avrcp.MediaAttributeId.ALBUM_NAME],
        )
    )

  async def test_player_app_setting_changed_from_ref(self) -> None:
    """Tests AVRCP player app setting changed from REF.

    Test steps:
      1. Connect and pair REF.
      2. Make A2DP and AVRCP connection from DUT.
      3. Set player app setting on REF and check if DUT receives the
         PLAYER_APPLICATION_SETTING_CHANGED event.
    """

    ref_a2dp_source_device = self._setup_a2dp_source_device(self.ref.device)
    ref_avrcp_protocol = ref_a2dp_source_device.avrcp_protocol
    condition = asyncio.Condition()

    class Delegate(avrcp.Delegate):

      @override
      async def get_current_player_app_settings(
          self,
      ) -> dict[avrcp.ApplicationSetting.AttributeId, int]:
        # Whenever event notification of PLAYER_APPLICATION_SETTING_CHANGED is
        # registered, the delegate.get_current_player_app_settings() is called
        # to get the current player app settings.
        async with condition:
          condition.notify_all()
          return await super().get_current_player_app_settings()

    ref_avrcp_protocol.delegate = Delegate(
        supported_events=[
            avrcp.EventId.PLAYER_APPLICATION_SETTING_CHANGED,
        ],
        supported_player_app_settings={
            avrcp.ApplicationSetting.AttributeId.REPEAT_MODE: [
                avrcp.ApplicationSetting.RepeatModeStatus.OFF,
                avrcp.ApplicationSetting.RepeatModeStatus.SINGLE_TRACK_REPEAT,
                avrcp.ApplicationSetting.RepeatModeStatus.ALL_TRACK_REPEAT,
                avrcp.ApplicationSetting.RepeatModeStatus.GROUP_REPEAT,
            ],
            avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF: [
                avrcp.ApplicationSetting.ShuffleOnOffStatus.OFF,
                avrcp.ApplicationSetting.ShuffleOnOffStatus.ALL_TRACKS_SHUFFLE,
                avrcp.ApplicationSetting.ShuffleOnOffStatus.GROUP_SHUFFLE,
            ],
        },
    )
    ref_avrcp_protocol.delegate.player_app_settings = {
        avrcp.ApplicationSetting.AttributeId.REPEAT_MODE: (
            avrcp.ApplicationSetting.RepeatModeStatus.OFF
        ),
        avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF: (
            avrcp.ApplicationSetting.ShuffleOnOffStatus.OFF
        ),
    }

    self.logger.info("[DUT] Connect to media browser")
    browser = self.dut.bl4a.connect_media_browser(
        self.bluetooth_package,
        self.bluetooth_browser_service,
    )
    self.test_case_context.push(browser)
    self.logger.info("[DUT] Register media controller callback")
    callback = browser.register_callback()
    self.test_case_context.push(callback)

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVRCP connection")
      await ref_a2dp_source_device.avrcp_protocol_starts.get()

    async def wait_for_registration() -> None:
      async with (
          self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS),
          condition,
      ):
        await condition.wait_for(
            lambda: avrcp.EventId.PLAYER_APPLICATION_SETTING_CHANGED
            in ref_avrcp_protocol.notification_listeners
        )

    for ref_repeat_mode, dut_repeat_mode in [
        (
            avrcp.ApplicationSetting.RepeatModeStatus.ALL_TRACK_REPEAT,
            android_constants.RepeatMode.ALL,
        ),
        (
            avrcp.ApplicationSetting.RepeatModeStatus.SINGLE_TRACK_REPEAT,
            android_constants.RepeatMode.ONE,
        ),
        (
            avrcp.ApplicationSetting.RepeatModeStatus.GROUP_REPEAT,
            android_constants.RepeatMode.GROUP,
        ),
        (
            avrcp.ApplicationSetting.RepeatModeStatus.OFF,
            android_constants.RepeatMode.OFF,
        ),
    ]:
      await wait_for_registration()
      self.logger.info("[REF] Set repeat mode to %r", ref_repeat_mode)
      ref_avrcp_protocol.delegate.player_app_settings[
          avrcp.ApplicationSetting.AttributeId.REPEAT_MODE
      ] = ref_repeat_mode
      ref_avrcp_protocol.notify_player_application_settings_changed([
          avrcp.PlayerApplicationSettingChangedEvent.Setting(
              avrcp.ApplicationSetting.AttributeId.REPEAT_MODE,
              ref_repeat_mode,
          )
      ])
      self.logger.info(
          "[DUT] Wait for repeat mode changed to %r", dut_repeat_mode
      )
      await callback.wait_for_event(
          bl4a_api.MediaBrowser.RepeatModeChanged(mode=dut_repeat_mode)
      )

    for ref_shuffle_mode, dut_shuffle_mode in [
        (
            avrcp.ApplicationSetting.ShuffleOnOffStatus.ALL_TRACKS_SHUFFLE,
            android_constants.ShuffleMode.ALL,
        ),
        (
            avrcp.ApplicationSetting.ShuffleOnOffStatus.GROUP_SHUFFLE,
            android_constants.ShuffleMode.GROUP,
        ),
        (
            avrcp.ApplicationSetting.ShuffleOnOffStatus.OFF,
            android_constants.ShuffleMode.OFF,
        ),
    ]:
      await wait_for_registration()
      self.logger.info("[REF] Set shuffle mode to %r", ref_shuffle_mode)
      ref_avrcp_protocol.delegate.player_app_settings[
          avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF
      ] = ref_shuffle_mode
      ref_avrcp_protocol.notify_player_application_settings_changed([
          avrcp.PlayerApplicationSettingChangedEvent.Setting(
              avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF,
              ref_shuffle_mode,
          )
      ])
      self.logger.info(
          "[DUT] Wait for shuffle mode changed to %r", dut_shuffle_mode
      )
      await callback.wait_for_event(
          bl4a_api.MediaBrowser.ShuffleModeChanged(mode=dut_shuffle_mode)
      )

  async def test_player_app_setting_changed_from_dut(self) -> None:
    """Tests AVRCP player app setting changed from DUT.

    Test steps:
      1. Connect and pair REF.
      2. Make A2DP and AVRCP connection from DUT.
      3. Set player app setting on DUT and check if REF receives the
         PLAYER_APPLICATION_SETTING_CHANGED event.
    """

    ref_a2dp_source_device = self._setup_a2dp_source_device(self.ref.device)
    ref_avrcp_protocol = ref_a2dp_source_device.avrcp_protocol
    condition = asyncio.Condition()

    class Delegate(avrcp.Delegate):

      @override
      async def set_player_app_settings(
          self, attribute: avrcp.ApplicationSetting.AttributeId, value: int
      ) -> None:
        await super().set_player_app_settings(attribute, value)
        async with condition:
          condition.notify_all()

    ref_avrcp_protocol.delegate = Delegate(
        supported_events=[
            avrcp.EventId.PLAYER_APPLICATION_SETTING_CHANGED,
        ],
        supported_player_app_settings={
            avrcp.ApplicationSetting.AttributeId.REPEAT_MODE: [
                avrcp.ApplicationSetting.RepeatModeStatus.OFF,
                avrcp.ApplicationSetting.RepeatModeStatus.SINGLE_TRACK_REPEAT,
                avrcp.ApplicationSetting.RepeatModeStatus.ALL_TRACK_REPEAT,
                avrcp.ApplicationSetting.RepeatModeStatus.GROUP_REPEAT,
            ],
            avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF: [
                avrcp.ApplicationSetting.ShuffleOnOffStatus.OFF,
                avrcp.ApplicationSetting.ShuffleOnOffStatus.ALL_TRACKS_SHUFFLE,
                avrcp.ApplicationSetting.ShuffleOnOffStatus.GROUP_SHUFFLE,
            ],
        },
    )
    ref_avrcp_protocol.delegate.player_app_settings = {
        avrcp.ApplicationSetting.AttributeId.REPEAT_MODE: (
            avrcp.ApplicationSetting.RepeatModeStatus.OFF
        ),
        avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF: (
            avrcp.ApplicationSetting.ShuffleOnOffStatus.OFF
        ),
    }

    self.logger.info("[DUT] Connect to media browser")
    browser = self.dut.bl4a.connect_media_browser(
        self.bluetooth_package,
        self.bluetooth_browser_service,
    )
    self.test_case_context.push(browser)

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVRCP connection")
      await ref_a2dp_source_device.avrcp_protocol_starts.get()

    for ref_repeat_mode, dut_repeat_mode in [
        (
            avrcp.ApplicationSetting.RepeatModeStatus.ALL_TRACK_REPEAT,
            android_constants.RepeatMode.ALL,
        ),
        (
            avrcp.ApplicationSetting.RepeatModeStatus.SINGLE_TRACK_REPEAT,
            android_constants.RepeatMode.ONE,
        ),
        (
            avrcp.ApplicationSetting.RepeatModeStatus.GROUP_REPEAT,
            android_constants.RepeatMode.GROUP,
        ),
        (
            avrcp.ApplicationSetting.RepeatModeStatus.OFF,
            android_constants.RepeatMode.OFF,
        ),
    ]:
      self.logger.info("[DUT] Set repeat mode to %r", dut_repeat_mode)
      browser.set_repeat_mode(dut_repeat_mode)
      self.logger.info(
          "[REF] Wait for repeat mode changed to %r", ref_repeat_mode
      )
      async with (
          self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS),
          condition,
      ):
        await condition.wait_for(
            lambda: (
                ref_avrcp_protocol.delegate.player_app_settings.get(
                    avrcp.ApplicationSetting.AttributeId.REPEAT_MODE
                )
                == ref_repeat_mode,  # pylint: disable=cell-var-from-loop
            )
        )

    for ref_shuffle_mode, due_shuffle_mode in [
        (
            avrcp.ApplicationSetting.ShuffleOnOffStatus.ALL_TRACKS_SHUFFLE,
            android_constants.ShuffleMode.ALL,
        ),
        (
            avrcp.ApplicationSetting.ShuffleOnOffStatus.GROUP_SHUFFLE,
            android_constants.ShuffleMode.GROUP,
        ),
        (
            avrcp.ApplicationSetting.ShuffleOnOffStatus.OFF,
            android_constants.ShuffleMode.OFF,
        ),
    ]:
      self.logger.info("[DUT] Set shuffle mode to %r", due_shuffle_mode)
      browser.set_shuffle_mode(due_shuffle_mode)
      self.logger.info(
          "[REF] Wait for shuffle mode changed to %r", ref_shuffle_mode
      )
      async with (
          self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS),
          condition,
      ):
        await condition.wait_for(
            lambda: (
                ref_avrcp_protocol.delegate.player_app_settings.get(
                    avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF
                )
                == ref_shuffle_mode  # pylint: disable=cell-var-from-loop
            ),
        )

  async def test_stream_suspend_and_resume(self) -> None:
    """Tests handling of AVDTP Suspend and Start (Resume) commands during an active stream without dropping the connection.

    Test steps:
      1. Establish connection and REF starts A2DP stream.
      2. REF sends AVDTP Suspend command.
      3. Verify audio stream is suspended smoothly on DUT (no disconnection).
      4. REF sends AVDTP Start command.
      5. Verify DUT resumes audio playback successfully.
    """

    ref_a2dp_source_device = self._setup_a2dp_source_device(
        self.ref.device, codecs=[a2dp_ext.A2dpCodec.SBC]
    )

    self.logger.info("[DUT] Connect and pair REF.")
    await self.classic_connect_and_pair(connect_profiles=True)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for AVDTP connection")
      avdtp_protocol = await ref_a2dp_source_device.avdtp_protocol_queue.get()
      self.logger.info("[REF] Discover remote endpoints")
      await avdtp_protocol.discover_remote_endpoints()

    sources = a2dp_ext.find_local_endpoints_by_codec(
        avdtp_protocol,
        a2dp.CodecType.SBC,
        avdtp.LocalSource,
        vendor_id=0,
        codec_id=0,
    )
    if not sources:
      self.fail("No A2DP local SBC source found")

    if not (stream := sources[0].stream):
      self.fail("REF doesn't create a stream")

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Start stream")
      await stream.start()

    await asyncio.sleep(_DEFAULT_STREAM_DURATION_SECONDS)

    self.assertEqual(stream.state, avdtp.State.STREAMING)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Suspend stream")
      await stream.stop()

    await asyncio.sleep(_DEFAULT_STREAM_DURATION_SECONDS)

    self.assertEqual(stream.state, avdtp.State.OPEN)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Resume stream")
      await stream.start()

    await asyncio.sleep(_DEFAULT_STREAM_DURATION_SECONDS)

    self.assertEqual(stream.state, avdtp.State.STREAMING)


if __name__ == "__main__":
  test_runner.main()
