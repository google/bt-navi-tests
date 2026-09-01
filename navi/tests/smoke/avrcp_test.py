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
from collections.abc import Iterable
import decimal
import sys
import tempfile
from typing import TypeAlias
import wave
import xml.etree.ElementTree as ET

from bumble import avc
from bumble import avdtp
from bumble import avrcp
from bumble import core
from bumble import hci
from bumble import l2cap
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import a2dp as a2dp_ext
from navi.bumble_ext import avdtp as avdtp_ext
from navi.bumble_ext import avrcp as avrcp_ext
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants

_A2DP_SERVICE_RECORD_HANDLE = 1
_AVRCP_CONTROLLER_RECORD_HANDLE = 2
_AVRCP_TARGET_RECORD_HANDLE = 3
_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_PROPERTY_AVRCP_BROWSABLE_MEDIA_PLAYER_ENABLED = (
    "bluetooth.avrcp.browsable_media_player.enabled"
)
_SAMPLE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c6360000200000500017a5eab3f0000000049454e44ae42"
    "6082"
)
_SHUFFLE_MODES = {
    avrcp.ApplicationSetting.ShuffleOnOffStatus.ALL_TRACKS_SHUFFLE: True,
    avrcp.ApplicationSetting.ShuffleOnOffStatus.OFF: False,
}
_REPEAT_MODES = {
    avrcp.ApplicationSetting.RepeatModeStatus.SINGLE_TRACK_REPEAT: (
        android_constants.RepeatMode.ONE
    ),
    avrcp.ApplicationSetting.RepeatModeStatus.ALL_TRACK_REPEAT: (
        android_constants.RepeatMode.ALL
    ),
    avrcp.ApplicationSetting.RepeatModeStatus.OFF: (
        android_constants.RepeatMode.OFF
    ),
}
_AVRCP_VERSION_MAP = {
    "avrcp13": (1, 3),
    "avrcp14": (1, 4),
    "avrcp15": (1, 5),
    "avrcp16": (1, 6),
}
_AVRCP_TO_AVCTP_VERSION_MAP = {
    (1, 3): (1, 2),
    (1, 4): (1, 3),
    (1, 5): (1, 4),
    (1, 6): (1, 4),
}

_SAMPLE_TRACK = bl4a_api.MediaItem(
    id="/classic/k545.ogg",
    title="Piano Sonata No. 16",
    playable=True,
    browsable=False,
)
_SAMPLE_FOLDER = bl4a_api.MediaItem(
    id="/classic",
    title="Classic",
    browsable=True,
    playable=False,
    children=[_SAMPLE_TRACK],
)

_Issuer = constants.TestRole
_StreamType: TypeAlias = android_constants.StreamType
_A2dpCodec = a2dp_ext.A2dpCodec
_AttributeId = avrcp.ApplicationSetting.AttributeId


class AvrcpDelegate(avrcp.Delegate):

  def __init__(self, supported_events: Iterable[avrcp.EventId] = ()):
    super().__init__(supported_events)
    self.condition = asyncio.Condition()

  async def set_absolute_volume(self, volume: int) -> None:
    await super().set_absolute_volume(volume)

    async with self.condition:
      self.condition.notify_all()


class AvrcpTest(navi_test_base.TwoDevicesTestBase):

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if (
        self.dut.getprop(android_constants.Property.A2DP_SOURCE_ENABLED)
        != "true"
    ):
      raise signals.TestAbortClass("[DUT] A2DP is not enabled.")

  @override
  async def async_teardown_test(self) -> None:
    await super().async_teardown_test()

    self.logger.info("[DUT] Stop audio.")
    self.dut.bt.audioStop()
    self.logger.info("[DUT] Set shuffle mode to OFF and repeat mode to OFF.")
    self.dut.bt.setShuffleMode(False)
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.OFF)

  def _setup_a2dp_device(
      self,
      codecs: list[_A2dpCodec],
      features: int,
  ) -> tuple[avdtp_ext.Listener, avrcp.Protocol]:
    """Sets up A2DP profile on REF.

    Args:
      codecs: A2DP codecs supported by REF.
      features: AVRCP controller features supported by REF.

    Returns:
      A tuple of (avdtp.Listener, avrcp.Protocol).
    """
    listener = a2dp_ext.setup_sink_server(
        self.ref.device,
        [codec.get_default_capabilities() for codec in codecs],
        _A2DP_SERVICE_RECORD_HANDLE,
    )
    avrcp_delegator = AvrcpDelegate(
        supported_events=(avrcp.EventId.VOLUME_CHANGED,)  # type: ignore[wrong-arg-types]
    )
    avrcp_protocol = avrcp_ext.setup_server(
        self.ref.device,
        avrcp_controller_handle=_AVRCP_CONTROLLER_RECORD_HANDLE,
        avrcp_target_handle=_AVRCP_TARGET_RECORD_HANDLE,
        delegate=avrcp_delegator,
        avrcp_controller_features=features,
    )

    return listener, avrcp_protocol

  async def _setup_a2dp_connection(
      self,
      ref_codecs: list[_A2dpCodec],
      ref_features: int = avrcp.ControllerFeatures.CATEGORY_1,
  ) -> tuple[
      avrcp.Protocol,
      avdtp.Protocol,
  ]:
    """Sets up A2DP connection between DUT and REF.

    Args:
      ref_codecs: A2DP codecs supported by REF.
      ref_features: AVRCP controller features supported by REF.

    Returns:
      A tuple of (avrcp.Protocol, avdtp.Protocol).
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.A2DP) as dut_cb:
      self.logger.info("[REF] Setup A2DP.")
      ref_avdtp_listener, ref_avrcp_protocol = self._setup_a2dp_device(
          ref_codecs, ref_features
      )
      avrcp_opened = asyncio.Event()
      ref_avrcp_protocol.once(ref_avrcp_protocol.EVENT_START, avrcp_opened.set)

      ref_avdtp_connections = asyncio.Queue[avdtp.Protocol]()
      ref_avdtp_listener.on(
          ref_avdtp_listener.EVENT_CONNECTION, ref_avdtp_connections.put
      )

      await self.classic_connect_and_pair(connect_profiles=True)

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )

      self.logger.info("[REF] Wait for A2DP connected.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_avdtp_connection = await ref_avdtp_connections.get()

      self.logger.info("[DUT] Wait for A2DP becomes active.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      self.logger.info("[REF] Wait for AVRCP connected.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await avrcp_opened.wait()

    return ref_avrcp_protocol, ref_avdtp_connection

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

  def _generate_and_push_image_file(self, path_on_device: str) -> None:
    # 1x1 transparent PNG (valid hex)
    with tempfile.NamedTemporaryFile(
        delete=(sys.platform != "win32")
    ) as local_file:
      local_file.write(_SAMPLE_PNG)
      local_file.flush()
      self.dut.adb.push([local_file.name, path_on_device])

  async def _avrcp_key_click(
      self,
      ref_avrcp_protocol: avrcp.Protocol,
      key: avc.PassThroughFrame.OperationId,
  ) -> None:
    self.logger.info("[REF] Press %s.", key.name)
    await ref_avrcp_protocol.send_key_event(key, pressed=True)

    self.logger.info("[REF] Release %s.", key.name)
    await ref_avrcp_protocol.send_key_event(key, pressed=False)

  async def test_sdp_discovery(self) -> None:
    """Tests SDP discovery from REF.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Discover A2DP source SDP records from REF.
      3. Discover AVRCP target SDP records from REF.
    """

    ref_acl_connection = await self.classic_connect_and_pair()

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Discover A2DP source SDP records.")
      a2dp_source_sdp_records = await a2dp_ext.SourceSdpRecord.find(
          ref_acl_connection
      )
      self.assertLen(a2dp_source_sdp_records, 1)
      a2dp_source_sdp_record = a2dp_source_sdp_records[0]
      self.logger.info(
          "[REF] Found A2DP source SDP records: %s.", a2dp_source_sdp_record
      )

      # Check A2DP version.
      if self.dut.bt.getSdkVersion() >= 36:
        self.assertGreaterEqual(a2dp_source_sdp_record.a2dp_version, (1, 4))
      else:
        self.assertGreaterEqual(a2dp_source_sdp_record.a2dp_version, (1, 3))

      # Check AVDTP version.
      self.assertGreaterEqual(a2dp_source_sdp_record.avdtp_version, (1, 3))

      # Check A2DP source features.
      if a2dp_source_sdp_record.supported_features is None:
        self.fail("No supported features found in A2DP source SDP record.")
      self.assertIn(
          a2dp_ext.SourceSdpRecord.Features.PLAYER,
          a2dp_source_sdp_record.supported_features,
      )

      self.logger.info("[REF] Discover AVRCP target SDP records.")
      avrcp_target_sdp_records = await avrcp.TargetServiceSdpRecord.find(
          ref_acl_connection
      )
      self.assertLen(avrcp_target_sdp_records, 1)
      avrcp_target_sdp_record = avrcp_target_sdp_records[0]
      self.logger.info(
          "[REF] Found AVRCP target SDP records: %s.", avrcp_target_sdp_record
      )

      # Check AVRCP version.
      expected_avrcp_version = _AVRCP_VERSION_MAP.get(
          self.dut.getprop(android_constants.Property.AVRCP_VERSION)
      )
      if expected_avrcp_version:
        # If the AVRCP version is set in the system property, verify it.
        self.assertEqual(
            avrcp_target_sdp_record.avrcp_version, expected_avrcp_version
        )
      else:
        # Otherwise, verify the AVRCP version is at least 1.5.
        self.assertGreaterEqual(avrcp_target_sdp_record.avrcp_version, (1, 5))

      # Check AVCTP version.
      self.assertEqual(
          avrcp_target_sdp_record.avctp_version,
          _AVRCP_TO_AVCTP_VERSION_MAP[avrcp_target_sdp_record.avrcp_version],
      )

      # Check AVRCP target features.
      expected_features = [avrcp.TargetFeatures.CATEGORY_1]
      if avrcp_target_sdp_record.avrcp_version > (1, 3):
        expected_features.extend([
            avrcp.TargetFeatures.SUPPORTS_MULTIPLE_MEDIA_PLAYER_APPLICATIONS,
            avrcp.TargetFeatures.SUPPORTS_BROWSING,
            avrcp.TargetFeatures.PLAYER_APPLICATION_SETTINGS,
        ])
      if avrcp_target_sdp_record.avrcp_version >= (1, 6):
        expected_features.append(avrcp.TargetFeatures.SUPPORTS_COVER_ART)
      self.assertContainsSubset(
          expected_features,
          avrcp.TargetFeatures(avrcp_target_sdp_record.supported_features),
      )

  @navi_test_base.parameterized(_Issuer.DUT, _Issuer.REF)
  async def test_set_absolute_volume(self, issuer: _Issuer) -> None:
    """Tests setting absolute volume.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Set absolute volume from issuer.
      3. Verify the volume is changed on DUT and REF.

    Args:
      issuer: device to issue the volume change command.
    """
    ref_avrcp_protocol, _ = await self._setup_a2dp_connection([_A2dpCodec.SBC])

    # Trigger an A2DP playback session to make sure the volume control is
    # ready.
    with self.dut.bl4a.register_callback(bl4a_api.Module.A2DP) as dut_a2dp_cb:
      self.logger.info("[DUT] Start audio.")
      self.dut.bt.audioPlaySine()
      if not self.dut.bt.isA2dpPlaying(self.ref.address):
        self.logger.info("[DUT] Wait for A2DP playing state.")
        await dut_a2dp_cb.wait_for_event(
            bl4a_api.A2dpPlayingStateChanged(
                address=self.ref.address,
                state=android_constants.A2dpState.PLAYING,
            )
        )

    ref_avrcp_delegator = ref_avrcp_protocol.delegate
    assert isinstance(ref_avrcp_delegator, AvrcpDelegate)

    self.logger.info("[DUT] Get max volume.")
    dut_max_volume = self.dut.bt.getMaxVolume(_StreamType.MUSIC)

    self.logger.info("[DUT] Get min volume.")
    dut_min_volume = self.dut.bt.getMinVolume(_StreamType.MUSIC)

    self.logger.info("[DUT] Set current volume to %d.", dut_max_volume // 2)
    self.dut.bt.setVolume(_StreamType.MUSIC, dut_max_volume // 2)

    def android_to_avrcp_volume(volume: int) -> int:
      # Android JVM uses ROUND_HALF_UP policy, while Python uses ROUND_HALF_EVEN
      # by default, so we need to specify policy here.
      return int(
          decimal.Decimal(
              (volume * avrcp.SetAbsoluteVolumeCommand.MAXIMUM_VOLUME)
              / dut_max_volume
          ).to_integral_exact(rounding=decimal.ROUND_HALF_UP)
      )

    self.logger.info("[REF] Wait for initial volume indicator.")
    async with (
        self.assert_not_timeout(
            _DEFAULT_STEP_TIMEOUT_SECONDS,
        ),
        ref_avrcp_delegator.condition,
    ):
      await ref_avrcp_delegator.condition.wait_for(
          lambda: (
              android_to_avrcp_volume(self.dut.bt.getVolume(_StreamType.MUSIC))
              == ref_avrcp_delegator.volume
          )
      )

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
        await dut_audio_cb.wait_for_event(
            bl4a_api.VolumeChanged(
                stream_type=_StreamType.MUSIC, volume_value=dut_expected_volume
            ),
        )
        self.assertEmpty(
            [
                event
                for event in dut_audio_cb.get_all_events(bl4a_api.VolumeChanged)
                if event.stream_type == _StreamType.MUSIC
            ],
            "Multiple volume changed events found.",
        )

        # There won't be volume changed events on REF as issuer.
        self.logger.info("[REF] Wait for volume changed.")
        if issuer == _Issuer.DUT:
          async with (
              self.assert_not_timeout(
                  _DEFAULT_STEP_TIMEOUT_SECONDS,
              ),
              ref_avrcp_delegator.condition,
          ):
            await ref_avrcp_delegator.condition.wait_for(
                lambda: ref_avrcp_delegator.volume == ref_expected_volume  # pylint: disable=cell-var-from-loop
            )

  @navi_test_base.retry(3)
  async def test_previous_next_track(self) -> None:
    """Tests moving to previous and next track over AVRCP.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Start stream from REF.
      3. Move to the next track from REF.
      4. Move back to the previous track from REF.
    """
    ref_avrcp_protocol, _ = await self._setup_a2dp_connection([_A2dpCodec.SBC])

    self.logger.info("[DUT] Set repeat mode to ONE.")
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)

    self.logger.info("[DUT] Generate wave file.")
    self._generate_and_push_wave_file(
        f"/data/media/{self.dut.adb.current_user_id}/Music/sample.wav"
    )
    app_uri = "/storage/self/primary/Music/sample.wav"
    media_item_1 = bl4a_api.MediaItem(id="1", uri=app_uri)
    media_item_2 = bl4a_api.MediaItem(id="2", uri=app_uri)

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.PLAYER
    ) as dut_player_cb:
      self.logger.info("[DUT] Set media item to 1.")
      self.dut.bl4a.play_media_item(media_item_1)

      self.logger.info("[DUT] Add media item of 2.")
      self.dut.bl4a.add_media_item(media_item_2)

      self.logger.info("[DUT] Wait for playback started.")
      await dut_player_cb.wait_for_event(
          bl4a_api.PlayerIsPlayingChanged(is_playing=True)
      )

      self.logger.info("[REF] Go to the next track.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await self._avrcp_key_click(
            ref_avrcp_protocol, avc.PassThroughFrame.OperationId.FORWARD
        )

      self.logger.info("[DUT] Wait for track transition.")
      await dut_player_cb.wait_for_event(
          bl4a_api.PlayerMediaItemTransition(media_item=media_item_2),
      )

      self.logger.info("[REF] Go back to the previous track.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await self._avrcp_key_click(
            ref_avrcp_protocol, avc.PassThroughFrame.OperationId.BACKWARD
        )

      self.logger.info("[DUT] Wait for track transition.")
      await dut_player_cb.wait_for_event(
          bl4a_api.PlayerMediaItemTransition(media_item=media_item_1),
      )

  @navi_test_base.retry(3)
  async def test_pause_and_resume(self) -> None:
    """Tests pause and resume over AVRCP.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Start stream from DUT.
      3. Pause stream from REF.
    """
    self.logger.info("[DUT] Set repeat mode to ONE.")
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)

    dut_player_cb = self.dut.bl4a.register_callback(bl4a_api.Module.PLAYER)
    self.test_case_context.enter_context(dut_player_cb)

    ref_avrcp_protocol, _ = await self._setup_a2dp_connection([_A2dpCodec.SBC])

    self.logger.info("[DUT] Start playback.")
    self.dut.bt.audioPlaySine()

    self.logger.info("[DUT] Wait for playback started.")
    await dut_player_cb.wait_for_event(
        bl4a_api.PlayerIsPlayingChanged(is_playing=True)
    )

    self.logger.info("[REF] Pause playback.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await self._avrcp_key_click(
          ref_avrcp_protocol, avc.PassThroughFrame.OperationId.PAUSE
      )

    self.logger.info("[DUT] Wait for playback stopped.")
    await dut_player_cb.wait_for_event(
        bl4a_api.PlayerIsPlayingChanged(is_playing=False)
    )

    self.logger.info("[REF] Resume playback.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await self._avrcp_key_click(
          ref_avrcp_protocol, avc.PassThroughFrame.OperationId.PLAY
      )

    self.logger.info("[DUT] Wait for playback resumed.")
    await dut_player_cb.wait_for_event(
        bl4a_api.PlayerIsPlayingChanged(is_playing=True)
    )

  @navi_test_base.retry(3)
  async def test_fast_forward_rewind(self) -> None:
    """Tests fast forward and rewind over AVRCP.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Start stream from REF.
      3. Fast forward from REF.
      4. Rewind from REF.
    """
    ref_avrcp_protocol, _ = await self._setup_a2dp_connection([_A2dpCodec.SBC])

    dut_player_cb = self.dut.bl4a.register_callback(bl4a_api.Module.PLAYER)
    self.test_case_context.enter_context(dut_player_cb)

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

    self.logger.info("[DUT] Wait for playback started.")
    await dut_player_cb.wait_for_event(
        bl4a_api.PlayerIsPlayingChanged(is_playing=True)
    )

    self.logger.info("[REF] Fast forward.")
    async with asyncio.timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await self._avrcp_key_click(
          ref_avrcp_protocol, avc.PassThroughFrame.OperationId.FAST_FORWARD
      )

    self.logger.info("[DUT] Wait for position discontinuity.")
    await dut_player_cb.wait_for_event(
        bl4a_api.PositionDiscontinuity,
        lambda e: (e.new_position_ms > e.old_position_ms),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    self.logger.info("[REF] Rewind.")
    async with asyncio.timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await self._avrcp_key_click(
          ref_avrcp_protocol, avc.PassThroughFrame.OperationId.REWIND
      )

    self.logger.info("[DUT] Wait for position discontinuity.")
    await dut_player_cb.wait_for_event(
        bl4a_api.PositionDiscontinuity,
        lambda e: (e.new_position_ms < e.old_position_ms),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

  @navi_test_base.retry(3)
  async def test_notification_on_playback_state_change(self) -> None:
    """Tests notification on playback state change.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Start stream from DUT and check notification.
      3. Pause stream from DUT and check notification.
      4. Resume stream from DUT and check notification.
      5. Stop stream from DUT and check notification.
    """
    ref_avrcp_protocol, _ = await self._setup_a2dp_connection([_A2dpCodec.SBC])

    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)

    self.logger.info("[DUT] Stop playback.")
    self.dut.bt.audioStop()

    self.logger.info("[REF] Register for the playback status.")
    playback_status_iter = ref_avrcp_protocol.monitor_playback_status()

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      status = await anext(playback_status_iter)
      if status == avrcp.PlayStatus.PAUSED:
        self.logger.info("[REF] Wait for playback stopped.")
        await anext(playback_status_iter)

        # Interim response of current playback state.
        await anext(playback_status_iter)

    self.logger.info("[DUT] Start playback.")
    self.dut.bt.audioPlaySine()

    self.logger.info("[REF] Wait for playback started.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      status = await anext(playback_status_iter)
    self.assertEqual(status, avrcp.PlayStatus.PLAYING)

    # Interim response of current playback state.
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      status = await anext(playback_status_iter)
    self.assertEqual(status, avrcp.PlayStatus.PLAYING)

    self.logger.info("[DUT] Pause playback.")
    self.dut.bt.audioPause()

    self.logger.info("[REF] Wait for playback state changed to paused.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      status = await anext(playback_status_iter)
    self.assertEqual(status, avrcp.PlayStatus.PAUSED)

    # Interim response of current playback state.
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      status = await anext(playback_status_iter)
    self.assertEqual(status, avrcp.PlayStatus.PAUSED)

    self.logger.info("[DUT] Resume playback.")
    self.dut.bt.audioResume()

    self.logger.info("[REF] Wait for playback state changed to playing.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      status = await anext(playback_status_iter)
    self.assertEqual(status, avrcp.PlayStatus.PLAYING)

    # Interim response of current playback state.
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      status = await anext(playback_status_iter)
    self.assertEqual(status, avrcp.PlayStatus.PLAYING)

    self.logger.info("[DUT] Stop playback.")
    self.dut.bt.audioStop()

    self.logger.info("[REF] Wait for playback state changed to stopped.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      status = await anext(playback_status_iter)
    self.assertEqual(status, avrcp.PlayStatus.STOPPED)

  @navi_test_base.retry(3)
  async def test_notification_on_playback_position_change(self) -> None:
    """Tests notification on play position change.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Start stream from DUT and check notification.
      3. Fast forward from DUT and check notification.
      4. Rewind from DUT and check notification.
    """
    ref_avrcp_protocol, _ = await self._setup_a2dp_connection([_A2dpCodec.SBC])

    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)

    dut_player_cb = self.dut.bl4a.register_callback(bl4a_api.Module.PLAYER)
    self.test_case_context.enter_context(dut_player_cb)

    self.logger.info("[DUT] Start playback.")
    self.dut.bt.audioPlaySine()

    self.logger.info("[DUT] Wait for playback started.")
    await dut_player_cb.wait_for_event(
        bl4a_api.PlayerIsPlayingChanged(is_playing=True)
    )

    pb_position_iter = ref_avrcp_protocol.monitor_playback_position(1)
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Wait for playback position.")
      first_position = await anext(pb_position_iter)

      self.logger.info("[REF] Wait for playback position again.")
      second_position = await anext(pb_position_iter)

    self.assertGreater(second_position, first_position)

  async def test_browsing(self) -> None:
    """Tests browsing over AVRCP.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Connect to browsing channel.
      3. Browse media player list.
      4. Set browsing player.
      5. Browse media browser apps.
      6. Change path to snippet media browser service.
      7. Browse media browser service.
      8. Change path to sample folder.
      9. Browse sample folder.
      10. Play sample track.
      11. Check if the media item is added to the player.
    """
    # Default value for this property is true, need to set it explicitly.
    if (
        self.dut.getprop(_PROPERTY_AVRCP_BROWSABLE_MEDIA_PLAYER_ENABLED)
        == "false"
    ):
      self.skipTest("Browsable media player is not enabled.")

    media_library_session = self.dut.bl4a.register_media_library_session(
        bl4a_api.MediaItem(
            id="/",
            title="Root",
            browsable=True,
            playable=False,
            children=[_SAMPLE_FOLDER],
        )
    )
    self.test_case_context.enter_context(media_library_session)

    ref_avrcp_protocol, _ = await self._setup_a2dp_connection(
        [_A2dpCodec.SBC],
        ref_features=(
            avrcp.ControllerFeatures.CATEGORY_1
            | avrcp.ControllerFeatures.SUPPORTS_BROWSING
        ),
    )
    ref_dut_connection = list(self.ref.device.connections.values())[0]

    self.logger.info("[REF] Connect to browsing channel.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      browsing_channel = await avrcp_ext.BrowsingController.connect(
          ref_dut_connection
      )
      self.logger.info("[REF] Browse media player list.")
      media_player_items = await browsing_channel.get_folder_items(
          scope=avrcp.Scope(avrcp.Scope.MEDIA_PLAYER_LIST)
      )
      self.assertLen(media_player_items, 1)
      player = media_player_items[0]
      assert isinstance(player, avrcp_ext.Player)
      self.assertEqual(player.displayable_name, "Bluetooth Player")

      self.logger.info("[REF] Set browsing player.")
      await browsing_channel.set_browsed_player(player.player_id)

      # Each folder under Bluetooth Player root should represent a media browser
      # service.
      self.logger.info("[REF] Get media browser apps.")
      browser_services = await browsing_channel.get_folder_items(
          scope=avrcp.Scope(avrcp.Scope.MEDIA_PLAYER_VIRTUAL_FILESYSTEM)
      )
      browser_service = next(
          (
              browser_service
              for browser_service in browser_services
              if isinstance(browser_service, avrcp_ext.Folder)
              and (
                  # Bluetooth uses the app label (if available) or the package
                  # name of the media browser app as the display name of the
                  # media browser service.
                  browser_service.displayable_name
                  == android_constants.PACKAGE_NAME_BLUETOOTH_SNIPPET
              )
          ),
          None,
      )
      if not browser_service:
        self.fail("No media browser service found.")

      self.logger.info("[REF] Change Folder to snippet media browser service.")
      number_of_items = await browsing_channel.change_path(
          direction=avrcp.ChangePathCommand.Direction.DOWN,
          folder_uid=browser_service.folder_uid,
      )

      self.logger.info("[REF] Browse media browser service.")
      folder_items = await browsing_channel.get_folder_items(
          scope=avrcp.Scope(avrcp.Scope.MEDIA_PLAYER_VIRTUAL_FILESYSTEM),
          start_item=0,
          end_item=number_of_items,
      )
      folder_item = folder_items[0]
      assert isinstance(folder_item, avrcp_ext.Folder)
      self.assertEqual(folder_item.displayable_name, _SAMPLE_FOLDER.title)

      self.logger.info("[REF] Change path to %s.", folder_item.displayable_name)
      number_of_items = await browsing_channel.change_path(
          direction=avrcp.ChangePathCommand.Direction.DOWN,
          folder_uid=folder_item.folder_uid,
      )

      self.logger.info("[REF] Browse %s.", folder_item.displayable_name)
      folder_items = await browsing_channel.get_folder_items(
          scope=avrcp.Scope(avrcp.Scope.MEDIA_PLAYER_VIRTUAL_FILESYSTEM),
          start_item=0,
          end_item=number_of_items,
      )
      media_element = folder_items[0]
      assert isinstance(media_element, avrcp_ext.MediaElement)
      self.assertEqual(media_element.displayable_name, _SAMPLE_TRACK.title)

      self.logger.info("[REF] Play %s.", media_element.displayable_name)
      await ref_avrcp_protocol.send_avrcp_command(
          avc.CommandFrame.CommandType.CONTROL,
          avrcp.PlayItemCommand(
              scope=avrcp.Scope(avrcp.Scope.MEDIA_PLAYER_VIRTUAL_FILESYSTEM),
              uid=media_element.media_element_uid,
              uid_counter=0,
          ),
      )

      self.logger.info("[DUT] Wait for media item added.")
      assert _SAMPLE_TRACK.id is not None
      await media_library_session.wait_for_event(
          bl4a_api.MediaItemAdded(media_id=_SAMPLE_TRACK.id)
      )

  @navi_test_base.retry(3)
  async def test_now_playing_items(self) -> None:
    """Tests browsing now playing items over AVRCP.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Connect to browsing channel.
      3. Browse media player list.
      4. Set addressed player.
      5. Browse now playing items.
      6. Add a media item to the player.
      7. Validate the now playing items are updated.
    """

    # Default value for this property is true, need to set it explicitly.
    if (
        self.dut.getprop(_PROPERTY_AVRCP_BROWSABLE_MEDIA_PLAYER_ENABLED)
        == "false"
    ):
      self.skipTest("Browsable media player is not enabled.")

    player_cb = self.dut.bl4a.register_callback(bl4a_api.Module.PLAYER)
    self.test_case_context.enter_context(player_cb)
    self._generate_and_push_wave_file(
        f"/data/media/{self.dut.adb.current_user_id}/Music/sample.wav"
    )
    initial_media_item = bl4a_api.MediaItem(
        title="sample-1",
        artist="sample-artist",
        album="sample-album",
        uri="/storage/self/primary/Music/sample.wav",
    )
    self.dut.bl4a.play_media_item(initial_media_item)

    self.logger.info("[DUT] Wait for playback started.")
    await player_cb.wait_for_event(
        bl4a_api.PlayerIsPlayingChanged(is_playing=True)
    )

    ref_avrcp_protocol, _ = await self._setup_a2dp_connection(
        [_A2dpCodec.SBC],
        ref_features=(
            avrcp.ControllerFeatures.CATEGORY_1
            | avrcp.ControllerFeatures.SUPPORTS_BROWSING
        ),
    )
    ref_dut_connection = list(self.ref.device.connections.values())[0]

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Connect to browsing channel.")
      browsing_channel = await avrcp_ext.BrowsingController.connect(
          ref_dut_connection
      )
      self.logger.info("[REF] Browse media player list.")
      media_player_items = await browsing_channel.get_folder_items(
          scope=avrcp.Scope(avrcp.Scope.MEDIA_PLAYER_LIST)
      )
      self.assertLen(media_player_items, 1)
      player = media_player_items[0]
      assert isinstance(player, avrcp_ext.Player)
      self.assertEqual(player.displayable_name, "Bluetooth Player")

      self.logger.info("[REF] Set addressed player.")
      await ref_avrcp_protocol.send_avrcp_command(
          avc.CommandFrame.CommandType.CONTROL,
          avrcp.SetAddressedPlayerCommand(player_id=player.player_id),
      )

      self.logger.info("[REF] Browse now playing.")
      items = await browsing_channel.get_folder_items(
          scope=avrcp.Scope.NOW_PLAYING
      )
      self.assertLen(items, 1)
      item = items[0]
      assert isinstance(item, avrcp_ext.MediaElement)
      self.assertEqual(item.displayable_name, initial_media_item.title)
      self.assertEqual(
          item.attributes[avrcp.MediaAttributeId.TITLE],
          initial_media_item.title,
      )
      self.assertEqual(
          item.attributes[avrcp.MediaAttributeId.ARTIST_NAME],
          initial_media_item.artist,
      )
      self.assertEqual(
          item.attributes[avrcp.MediaAttributeId.ALBUM_NAME],
          initial_media_item.album,
      )

      now_playing_content_changed_iter = (
          ref_avrcp_protocol.monitor_now_playing_content()
      )
      # First yield is from INTERIM response
      await anext(now_playing_content_changed_iter)

      self.logger.info("[DUT] Add a media item.")
      new_media_item = bl4a_api.MediaItem(
          title="sample-2",
          artist="sample-artist",
          album="sample-album",
          uri="/storage/self/primary/Music/sample.wav",
      )
      self.dut.bl4a.add_media_item(new_media_item)

      self.logger.info("[REF] Wait for now playing content changed.")
      await anext(now_playing_content_changed_iter)

      self.logger.info("[REF] Browse now playing again.")
      items = await browsing_channel.get_folder_items(
          scope=avrcp.Scope.NOW_PLAYING
      )
      self.assertLen(items, 2)
      item = items[1]
      assert isinstance(item, avrcp_ext.MediaElement)
      self.assertEqual(item.displayable_name, new_media_item.title)
      self.assertEqual(
          item.attributes[avrcp.MediaAttributeId.TITLE], new_media_item.title
      )
      self.assertEqual(
          item.attributes[avrcp.MediaAttributeId.ARTIST_NAME],
          new_media_item.artist,
      )
      self.assertEqual(
          item.attributes[avrcp.MediaAttributeId.ALBUM_NAME],
          new_media_item.album,
      )

  async def test_cover_art(self) -> None:
    """Tests AVRCP Cover Art by playing a track from the DUT.

    Test steps:
      1. Push a test image to the DUT.
      2. Play a media item with the test image as artwork on the DUT.
      3. Connect A2DP and AVRCP.
      4. Retrieve the now playing items and get the Cover Art Image Handle.
      5. Discover the BIP SDP record on the DUT and get the L2CAP PSM.
      6. Connect to the BIP server using CoverArtClient.
      7. Retrieve and verify the image properties.
      8. Retrieve the image itself and verify it matches the pushed image.
    """
    # Early skip if AVRCP version is set and is < 1.6
    avrcp_version_prop = self.dut.getprop(
        android_constants.Property.AVRCP_VERSION
    )
    if avrcp_version_prop:
      avrcp_version = _AVRCP_VERSION_MAP.get(avrcp_version_prop)
      if avrcp_version and avrcp_version < (1, 6):
        self.skipTest(
            f"AVRCP version is {avrcp_version_prop}, Cover Art requires 1.6+"
        )

    player_cb = self.dut.bl4a.register_callback(bl4a_api.Module.PLAYER)
    self.test_case_context.enter_context(player_cb)

    user_id = self.dut.adb.current_user_id
    image_path_on_device = f"/data/media/{user_id}/Music/cover.png"
    self._generate_and_push_image_file(image_path_on_device)

    audio_path_on_device = f"/data/media/{user_id}/Music/cover_art_audio.wav"
    self._generate_and_push_wave_file(audio_path_on_device)

    audio_uri = "/storage/self/primary/Music/cover_art_audio.wav"
    artwork_content_uri = f"content://{android_constants.PACKAGE_NAME_BLUETOOTH_SNIPPET}.FileProvider/shares/Music/cover.png"
    track_with_cover = bl4a_api.MediaItem(
        title="Track with Cover",
        artist="Artist",
        album="Album",
        uri=audio_uri,
        artwork_uri=artwork_content_uri,
    )

    self.logger.info("[DUT] Play media item with cover art.")
    self.dut.bl4a.play_media_item(track_with_cover)

    self.logger.info("[DUT] Wait for playback started.")
    await player_cb.wait_for_event(
        bl4a_api.PlayerIsPlayingChanged(is_playing=True)
    )

    # Connect A2DP and AVRCP
    ref_avrcp_protocol, _ = await self._setup_a2dp_connection(
        [_A2dpCodec.SBC],
        ref_features=(
            avrcp.ControllerFeatures.CATEGORY_1
            | avrcp.ControllerFeatures.SUPPORTS_BROWSING
        ),
    )

    # Find the ACL connection
    ref_acl_connection = self.ref.device.find_connection_by_bd_addr(
        hci.Address(self.dut.address),
        transport=core.PhysicalTransport.BR_EDR,
    )
    assert ref_acl_connection, "Failed to find ACL connection"

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Connect Browsing Channel.")
      browsing_channel = await avrcp_ext.BrowsingController.connect(
          ref_acl_connection
      )

      self.logger.info("[REF] Get media players.")
      players = await browsing_channel.get_folder_items(
          scope=avrcp.Scope.MEDIA_PLAYER_LIST
      )
      self.assertLen(players, 1)
      player = players[0]
      assert isinstance(player, avrcp_ext.Player)

      self.logger.info("[REF] Set addressed player.")
      await ref_avrcp_protocol.send_avrcp_command(
          avc.CommandFrame.CommandType.CONTROL,
          avrcp.SetAddressedPlayerCommand(player_id=player.player_id),
      )

      # Now connect to the BIP server first, so that the DUT knows we support
      # Cover Art and won't filter out the handle.
      self.logger.info("[REF] Find BIP SDP record.")
      avrcp_sdp_records = await avrcp_ext.TargetServiceSdpRecord.find(
          ref_acl_connection
      )
      self.assertLen(avrcp_sdp_records, 1)
      avrcp_sdp_record = avrcp_sdp_records[0]
      if avrcp_sdp_record.avrcp_version < (1, 6):
        self.skipTest(
            f"AVRCP version is {avrcp_sdp_record.avrcp_version}, Cover Art"
            " requires 1.6+"
        )
      assert (
          avrcp_sdp_record.bip_goep_l2cap_psm is not None
      ), "BIP L2CAP PSM is missing"

      self.logger.info("[REF] Connect BIP L2CAP channel.")
      bip_bearer = await ref_acl_connection.create_l2cap_channel(
          l2cap.ClassicChannelSpec(
              psm=avrcp_sdp_record.bip_goep_l2cap_psm,
              mode=l2cap.TransmissionMode.ENHANCED_RETRANSMISSION,
              fcs_enabled=True,
          )
      )

      bip_client = avrcp_ext.CoverArtClient(bip_bearer)
      self.logger.info("[REF] Connect BIP OBEX session.")
      await bip_client.connect()

      self.logger.info(
          "[REF] Get element attributes for currently playing track."
      )
      attrs = await ref_avrcp_protocol.get_element_attributes(
          element_identifier=0,
          attribute_ids=[
              avrcp.MediaAttributeId.TITLE,
              avrcp.MediaAttributeId.DEFAULT_COVER_ART,
          ],
      )
      self.logger.info("Retrieved attributes: %s", attrs)

      cover_art_handle = next(
          (
              attr.attribute_value
              for attr in attrs
              if attr.attribute_id == avrcp.MediaAttributeId.DEFAULT_COVER_ART
          ),
          None,
      )

      assert (
          cover_art_handle is not None
      ), "Cover Art Handle is missing in element attributes"
      self.logger.info("Found Cover Art Handle: %s", cover_art_handle)
      self.assertTrue(cover_art_handle.isdigit() and len(cover_art_handle) == 7)

      self.logger.info("[REF] Get Image Properties.")
      properties = await bip_client.get_image_properties(cover_art_handle)
      self.logger.info(
          "Image Properties: %s", properties.decode("utf-8", errors="replace")
      )
      try:
        root = ET.fromstring(properties)
      except ET.ParseError as e:
        self.fail(f"Failed to parse image properties XML: {e}")

      self.assertEqual(root.tag, "image-properties")
      self.assertEqual(root.attrib.get("handle"), cover_art_handle)

      images = root.findall("native") + root.findall("variant")
      self.assertNotEmpty(
          images, "No <native> or <variant> elements found in properties"
      )
      self.assertTrue(
          any(img.attrib.get("encoding") == "JPEG" for img in images),
          "Expected JPEG image format in properties",
      )

      self.logger.info("[REF] Get Image.")
      image_data = await bip_client.get_image(cover_art_handle)
      self.logger.info("Retrieved image size: %d", len(image_data))

      # Android's Cover Art service scales the image to 200x200 and always
      # compresses it to JPEG (quality 100) before sending, regardless of the
      # original format (e.g. PNG). So we expect a valid JPEG header.
      self.assertGreater(len(image_data), 500)
      self.assertStartsWith(
          image_data,
          b"\xff\xd8\xff",
          "Image data does not start with JPEG magic bytes",
      )

  @navi_test_base.retry(3)
  async def test_list_player_application_setting_attributes(self) -> None:
    """Tests list player application setting attributes.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. List player application setting attributes.
      3. List player application setting values for each attribute.
      4. Get current player application setting value for each attribute.
    """
    self.dut.bt.setShuffleMode(False)
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.OFF)

    ref_avrcp_protocol, _ = await self._setup_a2dp_connection([_A2dpCodec.SBC])

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Get player application setting attributes.")
      supported_settings = (
          await ref_avrcp_protocol.list_supported_player_app_settings()
      )
      self.assertContainsSubset(
          [
              avrcp.ApplicationSetting.AttributeId.REPEAT_MODE,
              avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF,
          ],
          supported_settings.keys(),
      )
      self.assertContainsSubset(
          [
              avrcp.ApplicationSetting.RepeatModeStatus.OFF,
              avrcp.ApplicationSetting.RepeatModeStatus.SINGLE_TRACK_REPEAT,
              avrcp.ApplicationSetting.RepeatModeStatus.ALL_TRACK_REPEAT,
              avrcp.ApplicationSetting.RepeatModeStatus.GROUP_REPEAT,
          ],
          supported_settings[avrcp.ApplicationSetting.AttributeId.REPEAT_MODE],
      )
      self.assertContainsSubset(
          [
              avrcp.ApplicationSetting.ShuffleOnOffStatus.OFF,
              avrcp.ApplicationSetting.ShuffleOnOffStatus.ALL_TRACKS_SHUFFLE,
              avrcp.ApplicationSetting.ShuffleOnOffStatus.GROUP_SHUFFLE,
          ],
          supported_settings[
              avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF
          ],
      )

      self.logger.info("[REF] Get current player application setting values.")
      current_settings = await ref_avrcp_protocol.get_player_app_settings([
          avrcp.ApplicationSetting.AttributeId.REPEAT_MODE,
          avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF,
      ])
      self.assertEqual(
          current_settings[avrcp.ApplicationSetting.AttributeId.REPEAT_MODE],
          avrcp.ApplicationSetting.RepeatModeStatus.OFF,
      )
      self.assertEqual(
          current_settings[avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF],
          avrcp.ApplicationSetting.ShuffleOnOffStatus.OFF,
      )

  @navi_test_base.retry(3)
  async def test_notification_on_player_application_setting_change(
      self,
  ) -> None:
    """Tests notification on player application setting change.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Set shuffle mode to on on DUT and check the status on REF.
      3. Set repeat mode to all on DUT and check the status on REF.
      4. Set shuffle mode to off on DUT and check the status on REF.
      5. Set repeat mode to single on DUT and check the status on REF.
      6. Set shuffle mode to on on DUT and check the status on REF.
      7. Set repeat mode to off on DUT and check the status on REF.
    """
    self.logger.info("[DUT] Set shuffle mode to on.")
    self.dut.bt.setShuffleMode(True)
    current_shuffle_mode = (
        avrcp.ApplicationSetting.ShuffleOnOffStatus.ALL_TRACKS_SHUFFLE
    )
    self.logger.info("[DUT] Set repeat mode to all.")
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ALL)
    current_repeat_mode = (
        avrcp.ApplicationSetting.RepeatModeStatus.ALL_TRACK_REPEAT
    )

    ref_avrcp_protocol, _ = await self._setup_a2dp_connection([_A2dpCodec.SBC])

    settings_iter = ref_avrcp_protocol.monitor_player_application_settings()

    def response_to_dict(
        response: list[avrcp.PlayerApplicationSettingChangedEvent.Setting],
    ) -> dict[int, int]:
      return {setting.attribute_id: setting.value_id for setting in response}

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      for ref_shuffle_mode in (
          avrcp.ApplicationSetting.ShuffleOnOffStatus.OFF,
          avrcp.ApplicationSetting.ShuffleOnOffStatus.ALL_TRACKS_SHUFFLE,
      ):
        self.logger.info("[REF] Wait for interim response.")
        settings = response_to_dict(await anext(settings_iter))
        self.assertEqual(
            settings.get(_AttributeId.SHUFFLE_ON_OFF), current_shuffle_mode
        )

        dut_shuffle_mode = _SHUFFLE_MODES[ref_shuffle_mode]
        self.logger.info("[DUT] Set shuffle mode to %r.", dut_shuffle_mode)
        self.dut.bt.setShuffleMode(dut_shuffle_mode)

        self.logger.info("[REF] Wait for changed response.")
        settings = response_to_dict(await anext(settings_iter))
        self.assertEqual(
            settings.get(_AttributeId.SHUFFLE_ON_OFF), ref_shuffle_mode
        )
        current_shuffle_mode = ref_shuffle_mode

      for ref_repeat_mode in (
          avrcp.ApplicationSetting.RepeatModeStatus.OFF,
          avrcp.ApplicationSetting.RepeatModeStatus.SINGLE_TRACK_REPEAT,
          avrcp.ApplicationSetting.RepeatModeStatus.ALL_TRACK_REPEAT,
      ):
        self.logger.info("[REF] Wait for interim response.")
        settings = response_to_dict(await anext(settings_iter))
        self.assertEqual(
            settings.get(_AttributeId.REPEAT_MODE), current_repeat_mode
        )

        dut_repeat_mode = _REPEAT_MODES[ref_repeat_mode]
        self.logger.info("[DUT] Set repeat mode to %r.", dut_repeat_mode)
        self.dut.bt.audioSetRepeat(dut_repeat_mode)

        self.logger.info("[REF] Wait for changed response.")
        settings = response_to_dict(await anext(settings_iter))
        self.assertEqual(
            settings.get(_AttributeId.REPEAT_MODE), ref_repeat_mode
        )
        current_repeat_mode = ref_repeat_mode

  @navi_test_base.retry(3)
  async def test_set_player_application_settings(
      self,
  ) -> None:
    """Tests set player application settings from REF.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Set shuffle mode to off on DUT and check the status on REF.
      3. Set shuffle mode to on on DUT and check the status on REF.
    """
    self.dut.bt.setShuffleMode(False)
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.OFF)

    ref_avrcp_protocol, _ = await self._setup_a2dp_connection([_A2dpCodec.SBC])
    callback = self.dut.bl4a.register_callback(bl4a_api.Module.PLAYER)

    for ref_shuffle_mode, ref_repeat_mode in (
        (
            avrcp.ApplicationSetting.ShuffleOnOffStatus.ALL_TRACKS_SHUFFLE,
            avrcp.ApplicationSetting.RepeatModeStatus.SINGLE_TRACK_REPEAT,
        ),
        (
            avrcp.ApplicationSetting.ShuffleOnOffStatus.OFF,
            avrcp.ApplicationSetting.RepeatModeStatus.ALL_TRACK_REPEAT,
        ),
        (
            avrcp.ApplicationSetting.ShuffleOnOffStatus.ALL_TRACKS_SHUFFLE,
            avrcp.ApplicationSetting.RepeatModeStatus.OFF,
        ),
    ):
      self.logger.info(
          "[REF] Set player application settings to shuffle: %r, repeat: %r.",
          ref_shuffle_mode,
          ref_repeat_mode,
      )
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await ref_avrcp_protocol.send_avrcp_command(
            avc.CommandFrame.CommandType.CONTROL,
            avrcp.SetPlayerApplicationSettingValueCommand(
                attribute=[
                    avrcp.ApplicationSetting.AttributeId.SHUFFLE_ON_OFF,  # type: ignore
                    avrcp.ApplicationSetting.AttributeId.REPEAT_MODE,  # type: ignore
                ],
                value=[ref_shuffle_mode, ref_repeat_mode],
            ),
        )

      self.logger.info("[DUT] Wait for shuffle mode enabled changed.")
      await callback.wait_for_event(
          bl4a_api.PlayerShuffleModeEnabledChanged(
              enabled=_SHUFFLE_MODES[ref_shuffle_mode]
          )
      )
      self.logger.info("[DUT] Wait for repeat mode changed.")
      await callback.wait_for_event(
          bl4a_api.PlayerRepeatModeChanged(mode=_REPEAT_MODES[ref_repeat_mode])
      )


if __name__ == "__main__":
  test_runner.main()
