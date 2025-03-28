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

from __future__ import annotations

import asyncio
import decimal
import enum
import functools
import tempfile
from typing import Iterable, TypeAlias, cast

from bumble import a2dp
from bumble import avc
from bumble import avdtp
from bumble import avrcp
from bumble import hci
import bumble.core
import bumble.utils
from mobly import test_runner
from mobly import signals
import pydub
import pydub.generators
from typing_extensions import override

from navi.bumble_ext import a2dp as a2dp_ext
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import audio
from navi.utils import bl4a_api
from navi.utils import constants

_A2DP_SERVICE_RECORD_HANDLE = 1
_AVRCP_CONTROLLER_RECORD_HANDLE = 2
_AVRCP_TARGET_RECORD_HANDLE = 3
_DEFAULT_STEP_TIMEOUT_SECONDS = 5.0
_AVRCP_MAX_VOLUME = 127
_PROPERTY_CODEC_PRIORITY = "bluetooth.a2dp.source.%s_priority.config"
_VALUE_CODEC_DISABLED = -1
_PREPARE_TIME_SECONDS = 0.5

_Issuer = constants.TestRole
_A2dpState = android_constants.A2dpState
_StreamType: TypeAlias = android_constants.StreamType
_A2dpCodec = a2dp_ext.A2dpCodec


class LocalSinkWrapper:
  """Wrapper for LocalSink to provide start/suspend events."""

  class Command(enum.StrEnum):
    OPEN = "open"
    START = "start"
    SUSPEND = "suspend"
    CLOSE = "close"
    ABORT = "abort"

  def __init__(self, impl: avdtp.LocalSink):
    self.impl = impl
    self.last_command: LocalSinkWrapper.Command | None = None
    self.condition = asyncio.Condition()
    for command in LocalSinkWrapper.Command:
      self.impl.on(command.value, functools.partial(self._on_command, command))

  @bumble.utils.AsyncRunner.run_in_task()
  async def _on_command(self, command: LocalSinkWrapper.Command) -> None:
    self.last_command = command
    async with self.condition:
      self.condition.notify_all()


class AvrcpDelegate(avrcp.Delegate):

  def __init__(self, supported_events: Iterable[avrcp.EventId] = ()):
    super().__init__(supported_events)
    self.condition = asyncio.Condition()

  @override
  async def set_absolute_volume(self, volume: int) -> None:
    await super().set_absolute_volume(volume)
    async with self.condition:
      self.condition.notify_all()


class A2dpTest(navi_test_base.TwoDevicesTestBase):
  dut_supported_codecs: list[_A2dpCodec]

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if (
        self.dut.getprop(android_constants.Property.A2DP_SOURCE_ENABLED)
        != "true"
    ):
      raise signals.TestAbortClass("A2DP is not enabled on DUT.")
    self.dut_supported_codecs = [
        codec
        for codec in _A2dpCodec
        if int(
            self.dut.getprop(_PROPERTY_CODEC_PRIORITY % codec.name.lower())
            or "0"
        )
        > _VALUE_CODEC_DISABLED
    ]

  @override
  async def async_teardown_test(self) -> None:
    await super().async_teardown_test()
    self.dut.bt.audioStop()

  def _setup_a2dp_device(self, codecs: list[_A2dpCodec]) -> tuple[
      AvrcpDelegate,
      avrcp.Protocol,
      asyncio.Queue[tuple[avdtp.Protocol, dict[_A2dpCodec, LocalSinkWrapper]]],
  ]:
    """Sets up A2DP profile on REF.

    Args:
      codecs: A2DP codecs supported by REF.

    Returns:
      A tuple of (AvrcpDelegate, avrcp.Protocol, asyncio.Queue)
      - AvrcpDelegate: Delegate for AVRCP protocol.
      - avrcp.Protocol: AVRCP protocol.
      - asyncio.Queue: Queue of AVDTP connections.
    """
    avdtp_connections = asyncio.Queue[avdtp.Protocol]()
    self.ref.device.sdp_service_records = {
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

    def on_avdtp_connection(server: avdtp.Protocol) -> None:
      ref_sinks = dict[_A2dpCodec, LocalSinkWrapper]()
      for codec in codecs:
        ref_sinks[codec] = LocalSinkWrapper(
            server.add_sink(codec.get_default_capabilities())
        )
      avdtp_connections.put_nowait((server, ref_sinks))

    avdtp_listener = avdtp.Listener.for_device(self.ref.device)
    avdtp_listener.on("connection", on_avdtp_connection)

    ref_avrcp_delegator = AvrcpDelegate(
        supported_events=(avrcp.EventId.VOLUME_CHANGED,)
    )
    ref_avrcp_protocol = avrcp.Protocol(ref_avrcp_delegator)
    ref_avrcp_protocol.listen(self.ref.device)
    return ref_avrcp_delegator, ref_avrcp_protocol, avdtp_connections

  async def _setup_a2dp_connection(self, ref_codecs: list[_A2dpCodec]) -> tuple[
      AvrcpDelegate,
      avrcp.Protocol,
      avdtp.Protocol,
      dict[_A2dpCodec, LocalSinkWrapper],
  ]:
    """Sets up A2DP connection between DUT and REF.

    Args:
      ref_codecs: A2DP codecs supported by REF.

    Returns:
      A tuple of (AvrcpDelegate, avrcp.Protocol, avdtp.Protocol,
      dict[_A2dpCodec, LocalSinkWrapper])
      - AvrcpDelegate: Delegate for AVRCP protocol.
      - avrcp.Protocol: AVRCP protocol.
      - avdtp.Protocol: AVDTP protocol.
      - dict[_A2dpCodec, LocalSinkWrapper]: Sinks for each codec.
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.A2DP) as dut_cb:
      ref_avrcp_delegator, ref_avrcp_protocol, avdtp_connections = (
          self._setup_a2dp_device(ref_codecs)
      )

      self.logger.info("[DUT] Connect and pair REF.")
      ref_acl = await self.classic_connect_and_pair()

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: (e.state == android_constants.ConnectionState.CONNECTED),
      )
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS, msg="[REF] Wait for A2DP connected."
      ):
        avdtp_connection, ref_sinks = await avdtp_connections.get()
      self.logger.info("[DUT] Wait for A2DP becomes active.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged,
          lambda e: (e.address == self.ref.address),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      if ref_avrcp_protocol.avctp_protocol is not None:
        self.logger.info("[REF] AVRCP already connected.")
      else:
        self.logger.info("[REF] Connect AVRCP.")
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          await ref_avrcp_protocol.connect(ref_acl)
        self.logger.info("[REF] AVRCP connected.")
    return ref_avrcp_delegator, ref_avrcp_protocol, avdtp_connection, ref_sinks

  async def _terminate_connection_from_ref(self) -> None:
    self.logger.info("[DUT] Terminate connection.")
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      ref_acl = self.ref.device.find_connection_by_bd_addr(
          hci.Address(self.dut.address), transport=hci.BT_BR_EDR_TRANSPORT
      )
      if ref_acl is None:
        self.logger.info("[REF] No ACL connection found.")
        return
      await ref_acl.disconnect()
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected,
          lambda e: (e.address == self.ref.address),
      )

  async def _avrcp_key_click(
      self,
      ref_avrcp_protocol: avrcp.Protocol,
      key: avc.PassThroughFrame.OperationId,
  ) -> None:
    await ref_avrcp_protocol.send_key_event(key, pressed=True)
    await ref_avrcp_protocol.send_key_event(key, pressed=False)

  async def test_pair_and_connect(self) -> None:
    """Tests A2DP connection establishment right after a pairing session.

    Test steps:
      1. Setup A2DP on REF.
      2. Create bond from DUT.
      3. Wait A2DP connected on DUT (Android should autoconnect A2DP as AG).
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.A2DP) as dut_cb:
      self._setup_a2dp_device([_A2dpCodec.SBC])

      self.logger.info("[DUT] Connect and pair REF.")
      await self.classic_connect_and_pair()

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: (e.state == android_constants.ConnectionState.CONNECTED),
      )
      self.logger.info("[DUT] Wait for A2DP becomes active.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged,
          lambda e: (e.address == self.ref.address),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

  async def test_paired_connect_outgoing(self) -> None:
    """Tests A2DP connection establishment where pairing is not involved.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Terminate ACL connection.
      3. Trigger connection from DUT.
      4. Wait A2DP connected on DUT.
      5. Disconnect from DUT.
      6. Wait A2DP disconnected on DUT.
    """
    await self.test_pair_and_connect()
    await self._terminate_connection_from_ref()

    with self.dut.bl4a.register_callback(bl4a_api.Module.A2DP) as dut_cb:
      self.logger.info("[DUT] Reconnect.")
      self.dut.bt.connect(self.ref.address)

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: (e.state == android_constants.ConnectionState.CONNECTED),
      )
      self.logger.info("[DUT] Wait for A2DP becomes active.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged,
          lambda e: (e.address == self.ref.address),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      self.logger.info("[DUT] Disconnect.")
      self.dut.bt.disconnect(self.ref.address)

      self.logger.info("[DUT] Wait for A2DP disconnected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: (e.state == android_constants.ConnectionState.DISCONNECTED),
      )

  async def test_paired_connect_incoming(self) -> None:
    """Tests A2DP connection establishment where pairing is not involved.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Terminate ACL connection.
      3. Trigger connection from REF.
      4. Wait A2DP connected on DUT.
      5. Disconnect from REF.
      6. Wait A2DP disconnected on DUT.
    """
    await self.test_pair_and_connect()
    await self._terminate_connection_from_ref()

    with self.dut.bl4a.register_callback(bl4a_api.Module.A2DP) as dut_cb:
      self.logger.info("[REF] Reconnect.")
      dut_ref_acl = await self.ref.device.connect(
          str(self.dut.address),
          bumble.core.BT_BR_EDR_TRANSPORT,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      self.logger.info("[REF] Authenticate and encrypt connection.")
      await dut_ref_acl.authenticate()
      await dut_ref_acl.encrypt()

      self.logger.info("[REF] Connect A2DP.")
      server = await avdtp.Protocol.connect(dut_ref_acl)
      server.add_sink(_A2dpCodec.AAC.get_default_capabilities())

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: (e.state == android_constants.ConnectionState.CONNECTED),
      )
      self.logger.info("[DUT] Wait for A2DP becomes active.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged,
          lambda e: (e.address == self.ref.address),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      self.logger.info("[REF] Disconnect.")
      await dut_ref_acl.disconnect()

      self.logger.info("[DUT] Wait for A2DP disconnected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: (e.state == android_constants.ConnectionState.DISCONNECTED),
      )

  @navi_test_base.parameterized(
      (_Issuer.DUT, [_A2dpCodec.SBC]),
      (_Issuer.DUT, [_A2dpCodec.SBC, _A2dpCodec.AAC]),
      (_Issuer.DUT, [_A2dpCodec.SBC, _A2dpCodec.AAC, _A2dpCodec.APTX]),
      (_Issuer.DUT, [_A2dpCodec.SBC, _A2dpCodec.AAC, _A2dpCodec.APTX_HD]),
      (_Issuer.DUT, [_A2dpCodec.SBC, _A2dpCodec.AAC, _A2dpCodec.LDAC]),
      (_Issuer.REF, [_A2dpCodec.SBC]),
      (_Issuer.REF, [_A2dpCodec.SBC, _A2dpCodec.AAC]),
  )
  @navi_test_base.retry(2)
  async def test_stream_start_and_stop(
      self,
      issuer: _Issuer,
      ref_codecs: list[_A2dpCodec],
  ) -> None:
    """Tests A2DP streaming controlled by the given issuer (DUT or REF).

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Start stream from the given issuer.
      3. Stop stream from DUT from the given issuer.

    Args:
      issuer: device to issue the volume change command.
      ref_codecs: A2DP codecs supported by REF.
    """
    # Select preferred codec and sink.
    for codec in _A2dpCodec:
      if codec in ref_codecs and codec in self.dut_supported_codecs:
        preferred_codec = codec
        break
    else:
      self.fail("No supported codec found on REF.")

    self.logger.info("Preferred codec: %s", preferred_codec.name)

    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)

    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.A2DP) as dut_cb,
        self.dut.bl4a.register_callback(
            bl4a_api.Module.PLAYER
        ) as dut_player_cb,
    ):
      (
          ref_avrcp_delegator,
          ref_avrcp_protocol,
          ref_avdtp_connection,
          ref_sinks,
      ) = await self._setup_a2dp_connection(ref_codecs)
      del ref_avrcp_delegator, ref_avdtp_connection  # Unused.

      if not (ref_sink := ref_sinks.get(preferred_codec)):
        self.fail("No sink found for codec %s." % preferred_codec.name)

      # If there is a playback, wait until it ends.
      if self.dut.bt.isA2dpPlaying(self.ref.address):
        self.logger.info("[DUT] A2DP is streaming, wait for A2DP stopped.")
        await dut_cb.wait_for_event(
            bl4a_api.A2dpPlayingStateChanged,
            predicate=lambda e: (e.state == _A2dpState.NOT_PLAYING),
        )
      async with (
          self.assert_not_timeout(
              _DEFAULT_STEP_TIMEOUT_SECONDS,
              msg="[REF] A2DP is streaming, wait for A2DP stopped.",
          ),
          ref_sink.condition,
      ):
        await ref_sink.condition.wait_for(
            lambda: ref_sink.last_command != LocalSinkWrapper.Command.START
        )

      # Register the sink buffer to receive the packets.
      buffer = a2dp_ext.register_sink_buffer(ref_sink.impl, preferred_codec)

      if issuer == _Issuer.DUT:
        self.logger.info("[DUT] Start stream.")
        self.dut.bt.audioPlaySine()
      else:
        self.logger.info("[REF] Start stream.")
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          await self._avrcp_key_click(
              ref_avrcp_protocol, avc.PassThroughFrame.OperationId.PLAY
          )
        self.logger.info("[DUT] Wait for playback started.")
        await dut_player_cb.wait_for_event(
            bl4a_api.PlayerIsPlayingChanged,
            lambda e: (cast(bl4a_api.PlayerIsPlayingChanged, e).is_playing),
        )

      self.logger.info("[DUT] Wait for A2DP started.")
      await dut_cb.wait_for_event(
          callback_type=bl4a_api.A2dpPlayingStateChanged,
          predicate=lambda e: (e.state == _A2dpState.PLAYING),
      )
      async with (
          self.assert_not_timeout(
              _DEFAULT_STEP_TIMEOUT_SECONDS, msg="[REF] Wait for A2DP started."
          ),
          ref_sink.condition,
      ):
        await ref_sink.condition.wait_for(
            lambda: ref_sink.last_command == LocalSinkWrapper.Command.START
        )

      # Streaming for 1 second.
      await asyncio.sleep(1.0)

      if issuer == _Issuer.DUT:
        self.logger.info("[DUT] Stop stream.")
        self.dut.bt.audioPause()
      else:
        self.logger.info("[REF] Stop stream.")
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          await self._avrcp_key_click(
              ref_avrcp_protocol, avc.PassThroughFrame.OperationId.PAUSE
          )
        self.logger.info("[DUT] Wait for playback stopped.")
        await dut_player_cb.wait_for_event(
            bl4a_api.PlayerIsPlayingChanged,
            lambda e: (not cast(bl4a_api.PlayerIsPlayingChanged, e).is_playing),
        )

      self.logger.info("[DUT] Wait for A2DP stopped.")
      await dut_cb.wait_for_event(
          callback_type=bl4a_api.A2dpPlayingStateChanged,
          predicate=lambda e: (e.state == _A2dpState.NOT_PLAYING),
      )
      async with (
          self.assert_not_timeout(
              _DEFAULT_STEP_TIMEOUT_SECONDS, msg="[REF] Wait for A2DP stopped."
          ),
          ref_sink.condition,
      ):
        await ref_sink.condition.wait_for(
            lambda: ref_sink.last_command == LocalSinkWrapper.Command.SUSPEND
        )
      if buffer is not None:
        dominant_frequency = audio.get_dominant_frequency(
            buffer, format=preferred_codec.name
        )
        self.logger.info("Dominant frequency: %.2f", dominant_frequency)
        # Dominant frequency is not accurate on emulator.
        if not self.dut.device.is_emulator:
          self.assertAlmostEqual(dominant_frequency, 1000, delta=10)

  @navi_test_base.parameterized(_Issuer.DUT, _Issuer.REF)
  async def test_set_absolute_volume(self, issuer: _Issuer) -> None:
    """Tests setting absolute volume.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Set absolute volume.

    Args:
      issuer: device to issue the volume change command.
    """
    ref_avrcp_delegator, ref_avrcp_protocol, *_ = (
        await self._setup_a2dp_connection([_A2dpCodec.SBC])
    )

    dut_max_volume = self.dut.bt.getMaxVolume(_StreamType.MUSIC)
    dut_min_volume = self.dut.bt.getMinVolume(_StreamType.MUSIC)

    def android_to_avrcp_volume(volume: int) -> int:
      # Android JVM uses ROUND_HALF_UP policy, while Python uses ROUND_HALF_EVEN
      # by default, so we need to specify policy here.
      return int(
          decimal.Decimal(
              volume / dut_max_volume * _AVRCP_MAX_VOLUME
          ).to_integral_exact(rounding=decimal.ROUND_HALF_UP)
      )

    async with (
        self.assert_not_timeout(
            _DEFAULT_STEP_TIMEOUT_SECONDS,
            msg="[REF] Wait for initial volume indicator.",
        ),
        ref_avrcp_delegator.condition,
    ):
      await ref_avrcp_delegator.condition.wait_for(
          lambda: (
              android_to_avrcp_volume(self.dut.bt.getVolume(_StreamType.MUSIC))
              == ref_avrcp_delegator.volume
          )
      )

    # DUT's VCS client might not be stable at the beginning. If we set volume
    # immediately, the volume might not be set correctly.
    await asyncio.sleep(_PREPARE_TIME_SECONDS)

    with self.dut.bl4a.register_callback(bl4a_api.Module.AUDIO) as dut_audio_cb:
      for dut_expected_volume in range(dut_min_volume, dut_max_volume + 1):
        if self.dut.bt.getVolume(_StreamType.MUSIC) == dut_expected_volume:
          continue

        ref_expected_volume = android_to_avrcp_volume(dut_expected_volume)

        if issuer == _Issuer.DUT:
          self.logger.info("[DUT] Set volume to %d.", dut_expected_volume)
          self.dut.bt.setVolume(_StreamType.MUSIC, dut_expected_volume)
        else:
          self.logger.info("[REF] Set volume to %d.", ref_expected_volume)
          ref_avrcp_delegator.volume = ref_expected_volume
          ref_avrcp_protocol.notify_volume_changed(ref_expected_volume)

        self.logger.info("[DUT] Wait for volume changed.")
        volume_changed_event = await dut_audio_cb.wait_for_event(
            callback_type=bl4a_api.VolumeChanged,
            predicate=lambda e: (e.stream_type == _StreamType.MUSIC),
        )
        self.assertEqual(volume_changed_event.volume_value, dut_expected_volume)

        # There won't be volume changed events on REF as issuer.
        if issuer == _Issuer.DUT:
          async with (
              self.assert_not_timeout(
                  _DEFAULT_STEP_TIMEOUT_SECONDS,
                  msg="[REF] Wait for volume changed.",
              ),
              ref_avrcp_delegator.condition,
          ):
            await ref_avrcp_delegator.condition.wait_for(
                lambda: ref_avrcp_delegator.volume == ref_expected_volume  # pylint: disable=cell-var-from-loop
            )

  @navi_test_base.retry(3)
  async def test_avrcp_previous_next_track(self) -> None:
    """Tests moving to previous and next track over AVRCP."""
    ref_avrcp_protocol = (await self._setup_a2dp_connection([_A2dpCodec.SBC]))[
        1
    ]

    # Allow repeating to avoid the end of the track.
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)
    # Generate a sine wave audio file, and push it to DUT twice.
    with tempfile.NamedTemporaryFile() as local_file:
      audio_segment = pydub.generators.Sine(freq=440).to_audio_segment()
      audio_segment.export(local_file, format="mp3")
      for i in range(2):
        self.dut.adb.push([
            local_file.name,
            f"/data/media/{self.dut.adb.current_user_id}/Music/sample-{i}.mp3",
        ])

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.PLAYER
    ) as dut_player_cb:
      # Play the first track.
      self.dut.bt.audioPlayFile("/storage/self/primary/Music/sample-0.mp3")
      # Add the second track to the player.
      self.dut.bt.addMediaItem("/storage/self/primary/Music/sample-1.mp3")

      self.logger.info("[DUT] Wait for playback started.")
      await dut_player_cb.wait_for_event(
          bl4a_api.PlayerIsPlayingChanged,
          lambda e: (cast(bl4a_api.PlayerIsPlayingChanged, e).is_playing),
      )

      self.logger.info("[REF] Go to the next track.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await self._avrcp_key_click(
            ref_avrcp_protocol, avc.PassThroughFrame.OperationId.FORWARD
        )

      self.logger.info("[DUT] Wait for track transition.")
      await dut_player_cb.wait_for_event(
          bl4a_api.PlayerMediaItemTransition,
          lambda e: (
              "sample-1.mp3" in cast(bl4a_api.PlayerMediaItemTransition, e).uri
          ),
      )

      self.logger.info("[REF] Go back to the previous track.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await self._avrcp_key_click(
            ref_avrcp_protocol, avc.PassThroughFrame.OperationId.BACKWARD
        )

      self.logger.info("[DUT] Wait for track transition.")
      await dut_player_cb.wait_for_event(
          bl4a_api.PlayerMediaItemTransition,
          lambda e: (
              "sample-0.mp3" in cast(bl4a_api.PlayerMediaItemTransition, e).uri
          ),
      )

  async def test_noisy_handling(self) -> None:
    """Tests enabling noisy handling, and verify the player is paused after A2DP disconnected.

    Test steps:
      1. Enable noisy handling.
      2. Setup A2DP connection.
      3. Start streaming.
      4. Disconnect from REF.
      5. Wait for player paused.
    """
    if self.dut.device.is_emulator:
      self.skipTest("b/406208447 - Noisy handling is flaky on emulator.")

    # Enable audio noisy handling.
    self.dut.bt.setHandleAudioBecomingNoisy(True)

    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.A2DP) as dut_a2dp_cb,
        self.dut.bl4a.register_callback(
            bl4a_api.Module.PLAYER
        ) as dut_player_cb,
    ):
      await self._setup_a2dp_connection([_A2dpCodec.SBC])
      self.logger.info("[DUT] Start stream.")
      self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ALL)
      self.dut.bt.audioPlaySine()

      self.logger.info("[DUT] Wait for playback started.")
      await dut_player_cb.wait_for_event(
          bl4a_api.PlayerIsPlayingChanged,
          lambda e: ((cast(bl4a_api.PlayerIsPlayingChanged, e).is_playing)),
      )
      if not self.dut.bt.isA2dpPlaying(self.ref.address):
        self.logger.info("[DUT] Wait for A2DP playing.")
        await dut_a2dp_cb.wait_for_event(
            bl4a_api.A2dpPlayingStateChanged,
            lambda e: (e.state == android_constants.A2dpState.PLAYING),
        )

    # Streaming for 1 second.
    await asyncio.sleep(1.0)

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.PLAYER
    ) as dut_player_cb:
      ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
          hci.Address(self.dut.address), transport=hci.BT_BR_EDR_TRANSPORT
      )
      if ref_dut_acl is None:
        self.fail("No ACL connection found?")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.logger.info("[REF] Disconnect.")
        await ref_dut_acl.disconnect()

      self.logger.info("[DUT] Wait for player paused.")
      await dut_player_cb.wait_for_event(
          bl4a_api.PlayerIsPlayingChanged,
          lambda e: (not (cast(bl4a_api.PlayerIsPlayingChanged, e).is_playing)),
      )


if __name__ == "__main__":
  test_runner.main()
