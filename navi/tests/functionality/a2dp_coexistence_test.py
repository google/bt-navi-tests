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

"""Tests A2DP Source and Sink coexistence (Dual-Role)."""

import asyncio
import dataclasses

from bumble import avc
from bumble import avdtp
from bumble import avrcp
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import a2dp as a2dp_ext
from navi.bumble_ext import avdtp as avdtp_ext
from navi.bumble_ext import crown
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api

_A2DP_SERVICE_RECORD_HANDLE = 1
_AVRCP_TARGET_RECORD_HANDLE = 2
_AVRCP_CONTROLLER_RECORD_HANDLE = 3

_DEFAULT_STEP_TIMEOUT_SECONDS = 15.0
_MEDIA_BROWSER_SERVICE_NAME = "BluetoothMediaBrowserService"


class A2dpCoexistenceTest(navi_test_base.MultiDevicesTestBase):
  """Tests A2DP Source and A2DP Sink coexistence (dual-role) scenarios.

  This test class uses three devices:
    - DUT: The Device Under Test, supporting both A2DP Source and A2DP Sink
    roles.
    - A2DP Source Ref: A reference device acting as A2DP Source.
    - A2DP Sink Ref: A reference device acting as A2DP Sink.
  """

  bluetooth_package: str
  bluetooth_browser_service: str

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.device.is_emulator:
      self.setprop_for_class_context(
          android_constants.Property.A2DP_SOURCE_ENABLED, "true"
      )
      self.setprop_for_class_context(
          android_constants.Property.A2DP_SINK_ENABLED, "true"
      )
      self.setprop_for_class_context(
          android_constants.Property.AVRCP_CONTROLLER_ENABLED, "true"
      )
      self.setprop_for_class_context(
          android_constants.Property.A2DP_CODEC_EXTENSIBILITY,
          "false",
      )

    if (
        self.dut.getprop(android_constants.Property.A2DP_SOURCE_ENABLED)
        != "true"
    ):
      raise signals.TestAbortClass("A2DP Source is not enabled on DUT.")
    if self.dut.getprop(android_constants.Property.A2DP_SINK_ENABLED) != "true":
      raise signals.TestAbortClass("A2DP Sink is not enabled on DUT.")
    if (
        self.dut.getprop(android_constants.Property.AVRCP_CONTROLLER_ENABLED)
        != "true"
    ):
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

    # Disable Cross-Transport Key Derivation over Classic to avoid blocking SDP.
    for i, ref in enumerate(self.refs):
      self.logger.info(
          "[REF-%d] Disable CTKD over Classic to avoid blocking SDP.", i
      )
      ref.config.classic_smp_enabled = False

  @dataclasses.dataclass
  class SourceRef:
    device: crown.CrownDevice
    avdtp_protocol_queue: asyncio.Queue[avdtp.Protocol]
    avrcp_delegate: avrcp.Delegate
    avrcp_protocol: avrcp.Protocol
    avrcp_protocol_starts: asyncio.Queue[None]
    avrcp_key_events: asyncio.Queue[
        tuple[avc.PassThroughFrame.OperationId, bool]
    ]

  @dataclasses.dataclass
  class SinkRef:
    device: crown.CrownDevice
    avdtp_listener: avdtp_ext.Listener
    avdtp_connections: asyncio.Queue[avdtp.Protocol]
    avrcp_protocol: avrcp.Protocol
    avrcp_protocol_starts: asyncio.Queue[None]

  def _setup_source_ref(self, ref: crown.CrownDevice) -> SourceRef:
    """Sets up A2DP Source Ref acting as A2DP Source / AVRCP Target."""
    ref.device.sdp_service_records = {
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
    avdtp_protocol_queue = asyncio.Queue[avdtp.Protocol]()
    avdtp_listener = avdtp.Listener.for_device(device=ref.device)

    def on_avdtp_connection(protocol: avdtp.Protocol) -> None:
      protocol.add_source(
          a2dp_ext.A2dpCodec.SBC.get_default_capabilities(),
          a2dp_ext.A2dpCodec.SBC.get_media_packet_pump(
              protocol.l2cap_channel.peer_mtu
          ),
      )
      avdtp_protocol_queue.put_nowait(protocol)

    avdtp_listener.on(avdtp_listener.EVENT_CONNECTION, on_avdtp_connection)

    avrcp_key_events = asyncio.Queue[
        tuple[avc.PassThroughFrame.OperationId, bool]
    ]()

    class SourceRefDelegate(avrcp.Delegate):

      @override
      async def on_key_event(
          self,
          key: avc.PassThroughFrame.OperationId,
          pressed: bool,
          data: bytes,
      ) -> None:
        avrcp_key_events.put_nowait((key, pressed))

    avrcp_delegate = SourceRefDelegate(
        supported_events=[avrcp.EventId.PLAYBACK_STATUS_CHANGED]
    )
    avrcp_protocol_starts = asyncio.Queue[None]()
    avrcp_protocol = avrcp.Protocol(delegate=avrcp_delegate)
    avrcp_protocol.listen(ref.device)
    avrcp_protocol.on(
        avrcp_protocol.EVENT_START,
        lambda: avrcp_protocol_starts.put_nowait(None),
    )

    return self.SourceRef(
        device=ref,
        avdtp_protocol_queue=avdtp_protocol_queue,
        avrcp_delegate=avrcp_delegate,
        avrcp_protocol=avrcp_protocol,
        avrcp_protocol_starts=avrcp_protocol_starts,
        avrcp_key_events=avrcp_key_events,
    )

  def _setup_sink_ref(self, ref: crown.CrownDevice) -> SinkRef:
    """Sets up A2DP Sink Ref acting as A2DP Sink / AVRCP Controller."""
    avdtp_listener = a2dp_ext.setup_sink_server(
        ref.device,
        [a2dp_ext.A2dpCodec.SBC.get_default_capabilities()],
        _A2DP_SERVICE_RECORD_HANDLE,
    )
    avdtp_connections = asyncio.Queue[avdtp.Protocol]()
    avdtp_listener.on(
        avdtp_listener.EVENT_CONNECTION, avdtp_connections.put_nowait
    )

    ref.device.sdp_service_records.update({
        _AVRCP_CONTROLLER_RECORD_HANDLE: (
            avrcp.ControllerServiceSdpRecord(
                _AVRCP_CONTROLLER_RECORD_HANDLE,
                supported_features=avrcp.ControllerFeatures.CATEGORY_1,
            ).to_service_attributes()
        ),
    })

    avrcp_delegate = avrcp.Delegate()
    avrcp_protocol_starts = asyncio.Queue[None]()
    avrcp_protocol = avrcp.Protocol(delegate=avrcp_delegate)
    avrcp_protocol.listen(ref.device)
    avrcp_protocol.on(
        avrcp_protocol.EVENT_START,
        lambda: avrcp_protocol_starts.put_nowait(None),
    )

    return self.SinkRef(
        device=ref,
        avdtp_listener=avdtp_listener,
        avdtp_connections=avdtp_connections,
        avrcp_protocol=avrcp_protocol,
        avrcp_protocol_starts=avrcp_protocol_starts,
    )

  async def _avrcp_key_click(
      self,
      ref_avrcp_protocol: avrcp.Protocol,
      key: avc.PassThroughFrame.OperationId,
  ) -> None:
    self.logger.info("[REF] Press %s.", key.name)
    await ref_avrcp_protocol.send_key_event(key, pressed=True)

    self.logger.info("[REF] Release %s.", key.name)
    await ref_avrcp_protocol.send_key_event(key, pressed=False)

  async def _ensure_stream_open(
      self,
      stream: avdtp.Stream,
      timeout: float = _DEFAULT_STEP_TIMEOUT_SECONDS,
  ) -> None:
    self.logger.info(
        "[REF] Stream state is %s (%r)",
        stream.state.name if hasattr(stream.state, "name") else "unknown",
        stream.state,
    )
    if stream.state in (avdtp.State.OPEN, avdtp.State.STREAMING):
      return

    if stream.state == avdtp.State.IDLE:
      self.fail("Stream is IDLE, cannot open")

    condition = asyncio.Condition()

    async def notify(*args, **kwargs):
      del args, kwargs
      async with condition:
        condition.notify_all()

    events = [
        stream.local_endpoint.EVENT_OPEN,
        stream.local_endpoint.EVENT_START,
        stream.local_endpoint.EVENT_SUSPEND,
        stream.local_endpoint.EVENT_CLOSE,
        stream.local_endpoint.EVENT_ABORT,
    ]
    for event_name in events:
      stream.local_endpoint.on(event_name, notify)

    try:
      async with self.assert_not_timeout(timeout):
        async with condition:
          await condition.wait_for(
              lambda: stream.state
              in (
                  avdtp.State.CONFIGURED,
                  avdtp.State.OPEN,
                  avdtp.State.STREAMING,
              )
          )

        if stream.state == avdtp.State.CONFIGURED:
          self.logger.info("[REF] Stream is CONFIGURED, opening it...")
          await stream.open()

        async with condition:
          await condition.wait_for(
              lambda: stream.state in (avdtp.State.OPEN, avdtp.State.STREAMING)
          )
    finally:
      for event_name in events:
        stream.local_endpoint.remove_listener(event_name, notify)

  def _connect_media_browser(self) -> bl4a_api.MediaBrowser:
    self.logger.info("[DUT] Connecting media browser")
    browser = self.dut.bl4a.connect_media_browser(
        self.bluetooth_package, self.bluetooth_browser_service
    )
    self.test_case_context.push(browser)
    browser_cb = browser.register_callback()
    self.test_case_context.push(browser_cb)
    return browser

  async def _connect_sink_ref(
      self,
      sink_ref: SinkRef,
      dut_a2dp_cb: bl4a_api.CallbackHandler,
  ) -> avdtp.Protocol:
    self.logger.info("[DUT] Connect to A2DP Sink Ref.")
    await self.classic_connect_and_pair(sink_ref.device, connect_profiles=True)
    self.logger.info("[DUT] Wait for A2DP Source profile connected.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=sink_ref.device.address,
            state=android_constants.ConnectionState.CONNECTED,
        )
    )
    self.logger.info(
        "[DUT] Wait for A2DP Source profile active device changed."
    )
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=sink_ref.device.address)
    )
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_avdtp = await sink_ref.avdtp_connections.get()
      await sink_ref.avrcp_protocol_starts.get()
    return ref_avdtp

  async def _connect_source_ref(
      self,
      source_ref: SourceRef,
      dut_a2dp_sink_cb: bl4a_api.CallbackHandler,
  ) -> avdtp.Protocol:
    self.logger.info("[DUT] Connect to A2DP Source Ref.")
    await self.classic_connect_and_pair(
        source_ref.device, connect_profiles=True
    )
    self.logger.info("[DUT] Wait for A2DP Sink profile connected.")
    await dut_a2dp_sink_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=source_ref.device.address,
            state=android_constants.ConnectionState.CONNECTED,
        )
    )
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_avdtp = await source_ref.avdtp_protocol_queue.get()
      await source_ref.avrcp_protocol_starts.get()
    return ref_avdtp

  def _flush_key_events(
      self, queue: asyncio.Queue[tuple[avc.PassThroughFrame.OperationId, bool]]
  ) -> None:
    self.logger.info("[REF] Flushing key events queue")
    while not queue.empty():
      k, p = queue.get_nowait()
      self.logger.info(
          "[REF] Flushed key event: %s %s",
          k.name,
          "pressed" if p else "released",
      )

  async def _expect_key_click(
      self,
      queue: asyncio.Queue[tuple[avc.PassThroughFrame.OperationId, bool]],
      expected_key: avc.PassThroughFrame.OperationId,
      timeout: float = _DEFAULT_STEP_TIMEOUT_SECONDS,
  ) -> None:
    self.logger.info("[REF] Wait for %s key event", expected_key.name)
    async with self.assert_not_timeout(timeout):
      key, pressed = await queue.get()
      self.assertEqual(key, expected_key)
      self.assertTrue(pressed)
      key, pressed = await queue.get()
      self.assertEqual(key, expected_key)
      self.assertFalse(pressed)

  @navi_test_base.named_parameterized(
      sink_ref_then_source_ref=dict(connect_sink_ref_first=True),
      source_ref_then_sink_ref=dict(connect_sink_ref_first=False),
  )
  async def test_dual_connection(self, connect_sink_ref_first: bool) -> None:
    """Verifies dual connection establishment in both orders.

    Test steps:
      1. Setup A2DP Source Ref and A2DP Sink Ref.
      2. Connect A2DP Sink Ref and A2DP Source Ref to DUT in the specified
        order.
      3. Verify both profiles connect successfully and maintain connection.

    Args:
      connect_sink_ref_first: Whether to connect sink_ref first.
    """
    source_ref = self._setup_source_ref(self.refs[0])
    sink_ref = self._setup_sink_ref(self.refs[1])

    dut_a2dp_cb = self.dut.bl4a.register_callback(bl4a_api.Module.A2DP)
    dut_a2dp_sink_cb = self.dut.bl4a.register_callback(
        bl4a_api.Module.A2DP_SINK
    )
    self.test_case_context.push(dut_a2dp_cb)
    self.test_case_context.push(dut_a2dp_sink_cb)

    if connect_sink_ref_first:
      await self._connect_sink_ref(sink_ref, dut_a2dp_cb)
      await self._connect_source_ref(source_ref, dut_a2dp_sink_cb)
    else:
      await self._connect_source_ref(source_ref, dut_a2dp_sink_cb)
      await self._connect_sink_ref(sink_ref, dut_a2dp_cb)

  async def test_audio_routing(self) -> None:
    """Verifies audio routing from A2DP Source Ref to A2DP Sink Ref via DUT.

    Test steps:
      1. Connect A2DP Sink Ref and A2DP Source Ref to the DUT.
      2. Start SBC audio streaming from the A2DP Source Ref.
      3. Verify that the DUT routes the incoming A2DP audio stream to the
         A2DP Sink Ref (AVDTP state on A2DP Sink Ref transitions to STREAMING).
    """
    source_ref = self._setup_source_ref(self.refs[0])
    sink_ref = self._setup_sink_ref(self.refs[1])

    dut_a2dp_cb = self.dut.bl4a.register_callback(bl4a_api.Module.A2DP)
    dut_a2dp_sink_cb = self.dut.bl4a.register_callback(
        bl4a_api.Module.A2DP_SINK
    )
    self.test_case_context.push(dut_a2dp_cb)
    self.test_case_context.push(dut_a2dp_sink_cb)

    sink_ref_avdtp_protocol = await self._connect_sink_ref(
        sink_ref, dut_a2dp_cb
    )

    source_ref_avdtp_protocol = await self._connect_source_ref(
        source_ref, dut_a2dp_sink_cb
    )

    # Connect media browser to force session activation and request focus
    browser = self._connect_media_browser()

    self.logger.info("[DUT] Call browser.play() to request focus")
    browser.play()

    # Flush the PLAY key event on A2DP Source Ref to avoid interference
    await self._expect_key_click(
        source_ref.avrcp_key_events, avc.PassThroughFrame.OperationId.PLAY
    )

    # Setup A2DP Sink Ref sink wrapper
    ref1_sinks = a2dp_ext.find_local_endpoints_by_codec(
        sink_ref_avdtp_protocol,
        a2dp_ext.A2dpCodec.SBC.codec_type,
        avdtp.LocalSink,
    )
    self.assertTrue(ref1_sinks, "No sink found on A2DP Sink Ref")
    sink_ref_sink = a2dp_ext.LocalSinkWrapper(ref1_sinks[0])
    buffer = a2dp_ext.register_sink_buffer(
        sink_ref_sink.impl, a2dp_ext.A2dpCodec.SBC
    )

    # Find stream on A2DP Source Ref
    ref0_sources = a2dp_ext.find_local_endpoints_by_codec(
        source_ref_avdtp_protocol,
        a2dp_ext.A2dpCodec.SBC.codec_type,
        avdtp.LocalSource,
    )
    self.assertTrue(ref0_sources, "No source found on A2DP Source Ref")
    source_ref_stream = ref0_sources[0].stream
    assert source_ref_stream is not None
    self.assertIsNotNone(
        source_ref_stream, "A2DP Source Ref stream not configured"
    )

    # Ensure stream is OPEN
    await self._ensure_stream_open(source_ref_stream)

    # Start streaming from A2DP Source Ref (restart if already streaming to
    # force data pump)
    if source_ref_stream.state == avdtp.State.STREAMING:
      self.logger.info(
          "[A2DP Source Ref] Already streaming, stopping first to restart"
      )
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await source_ref_stream.stop()

    self.logger.info("[A2DP Source Ref] Start streaming")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await source_ref_stream.start()

    # Verify DUT routes it to A2DP Sink Ref
    self.logger.info("[A2DP Sink Ref] Wait for stream to start")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      async with sink_ref_sink.condition:
        await sink_ref_sink.condition.wait_for(
            lambda: sink_ref_sink.stream_state == avdtp.State.STREAMING
        )

    # Verify DUT reports A2DP is playing to A2DP Sink Ref
    self.logger.info("[DUT] Wait for A2DP playing to A2DP Sink Ref")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.A2dpPlayingStateChanged(
            address=sink_ref.device.address,
            state=android_constants.A2dpState.PLAYING,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    await asyncio.sleep(2.0)  # Stream for 2 seconds

    self.logger.info("[A2DP Source Ref] Stop streaming")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await source_ref_stream.stop()

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      async with sink_ref_sink.condition:
        await sink_ref_sink.condition.wait_for(
            lambda: sink_ref_sink.stream_state != avdtp.State.STREAMING
        )

    self.assertNotEmpty(buffer, "No audio data received on A2DP Sink Ref")

  async def test_avrcp_propagation(self) -> None:
    """Verifies AVRCP command propagation from A2DP Sink Ref to A2DP Source Ref.

    Test steps:
      1. Connect A2DP Sink Ref and A2DP Source Ref to the DUT.
      2. Connect a local MediaBrowser client on the DUT to
         BluetoothMediaBrowserService to prepare for session activation.
      3. Trigger playback by calling browser.play() from the DUT.
      4. Verify the A2DP Source Ref receives the AVRCP PLAY command.
      5. Start A2DP streaming from the A2DP Source Ref and notify PLAYING status
      to
         activate the DUT's media session.
      6. Send AVRCP PAUSE command from the A2DP Sink Ref.
      7. Verify the A2DP Source Ref receives the PAUSE command.
      8. Send AVRCP PLAY command from the A2DP Sink Ref.
      9. Verify the A2DP Source Ref receives the PLAY command.
    """
    source_ref = self._setup_source_ref(self.refs[0])
    sink_ref = self._setup_sink_ref(self.refs[1])

    dut_a2dp_cb = self.dut.bl4a.register_callback(bl4a_api.Module.A2DP)
    dut_a2dp_sink_cb = self.dut.bl4a.register_callback(
        bl4a_api.Module.A2DP_SINK
    )
    self.test_case_context.push(dut_a2dp_cb)
    self.test_case_context.push(dut_a2dp_sink_cb)

    # Connect both
    await self._connect_sink_ref(sink_ref, dut_a2dp_cb)

    source_ref_avdtp_protocol = await self._connect_source_ref(
        source_ref, dut_a2dp_sink_cb
    )

    # Find stream on A2DP Source Ref
    ref0_sources = a2dp_ext.find_local_endpoints_by_codec(
        source_ref_avdtp_protocol,
        a2dp_ext.A2dpCodec.SBC.codec_type,
        avdtp.LocalSource,
    )
    source_ref_stream = ref0_sources[0].stream
    assert source_ref_stream is not None

    # Use pre-configured A2DP Source Ref key events queue from _setup_source_ref
    source_ref_key_events = source_ref.avrcp_key_events

    # Ensure stream is OPEN
    await self._ensure_stream_open(source_ref_stream)

    # Connect media browser to force session activation
    browser = self._connect_media_browser()

    # Flush automatic key events if any
    self._flush_key_events(source_ref_key_events)

    # Trigger playback from DUT
    self.logger.info("[DUT] Call browser.play()")
    browser.play()

    # Verify A2DP Source Ref receives PLAY key event from DUT
    self.logger.info("[A2DP Source Ref] Wait for PLAY key event")
    await self._expect_key_click(
        source_ref_key_events, avc.PassThroughFrame.OperationId.PLAY
    )

    # Start streaming from A2DP Source Ref in response to PLAY if not already
    # streaming
    if source_ref_stream.state == avdtp.State.OPEN:
      self.logger.info("[A2DP Source Ref] Start streaming")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await source_ref_stream.start()
    else:
      self.logger.info("[A2DP Source Ref] Stream is already streaming")
    source_ref.avrcp_delegate.playback_status = avrcp.PlayStatus.PLAYING
    source_ref.avrcp_protocol.notify_playback_status_changed(avrcp.PlayStatus.PLAYING)

    await asyncio.sleep(2.0)

    # Flush automatic key events
    self._flush_key_events(source_ref_key_events)

    # Send PAUSE from A2DP Sink Ref
    self.logger.info("[A2DP Sink Ref] Send PAUSE")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await self._avrcp_key_click(
          sink_ref.avrcp_protocol, avc.PassThroughFrame.OperationId.PAUSE
      )

    # Verify A2DP Source Ref receives PAUSE
    self.logger.info("[A2DP Source Ref] Wait for PAUSE key event")
    await self._expect_key_click(
        source_ref_key_events, avc.PassThroughFrame.OperationId.PAUSE
    )

    # Send PLAY from A2DP Sink Ref to resume
    self.logger.info("[A2DP Sink Ref] Send PLAY")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await self._avrcp_key_click(
          sink_ref.avrcp_protocol, avc.PassThroughFrame.OperationId.PLAY
      )

    # Verify A2DP Source Ref receives PLAY
    self.logger.info("[A2DP Source Ref] Wait for PLAY key event")
    await self._expect_key_click(
        source_ref_key_events, avc.PassThroughFrame.OperationId.PLAY
    )

    # Stop streaming
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await source_ref_stream.stop()


if __name__ == "__main__":
  test_runner.main()
