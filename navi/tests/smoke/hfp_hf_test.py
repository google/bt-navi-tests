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

"""Tests related to Bluetooth HFP(Hands-Free Profile) HF role on Pixel."""

import asyncio
from collections.abc import Iterable, Sequence

from bumble import core
from bumble import device as bumble_device
from bumble import hci
from bumble import hfp
from bumble import rfcomm
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import hfp as hfp_ext
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants

_HfpState = android_constants.ConnectionState
_CallState = android_constants.CallState
_Callback = bl4a_api.CallbackHandler
_DEFAULT_STEP_TIMEOUT_SECONDS = 5.0
_HFP_AG_SDP_HANDLE = 1
_HFP_HF_ENABLED_PROPERTY = "bluetooth.profile.hfp.hf.enabled"
_PROPERTY_SWB_SUPPORTED = "bluetooth.hfp.swb.supported"
_MIN_HFP_VOLUME = 1
_MAX_HFP_VOLUME = 15
_STREAM_TYPE_CALL = android_constants.StreamType.CALL


class HfpHfTest(navi_test_base.TwoDevicesTestBase):
  ag_protocol: hfp.AgProtocol | None = None
  ref_hfp_protocols: asyncio.Queue[hfp.AgProtocol]

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.getprop(_HFP_HF_ENABLED_PROPERTY) != "true":
      raise signals.TestAbortClass("DUT does not have HFP HF enabled.")

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    self.ref_hfp_protocols = asyncio.Queue[hfp.AgProtocol]()

  @classmethod
  def _ag_configuration(
      cls,
      supported_ag_features: Iterable[hfp.AgFeature] | None = None,
      supported_ag_indicators: Sequence[hfp.AgIndicator] | None = None,
      supported_hf_indicators: Iterable[hfp.HfIndicator] | None = None,
      supported_ag_call_hold_operations: (
          Iterable[hfp.CallHoldOperation] | None
      ) = None,
      supported_audio_codecs: Iterable[hfp.AudioCodec] | None = None,
  ) -> hfp.AgConfiguration:
    return hfp.AgConfiguration(
        supported_ag_features=(
            supported_ag_features
            or [
                hfp.AgFeature.ENHANCED_CALL_STATUS,
            ]
        ),
        supported_ag_indicators=(
            supported_ag_indicators
            or [
                hfp.AgIndicatorState.call(),
                hfp.AgIndicatorState.callsetup(),
                hfp.AgIndicatorState.service(),
                hfp.AgIndicatorState.signal(),
                hfp.AgIndicatorState.roam(),
                hfp.AgIndicatorState.callheld(),
                hfp.AgIndicatorState.battchg(),
            ]
        ),
        supported_hf_indicators=supported_hf_indicators or [],
        supported_ag_call_hold_operations=(
            supported_ag_call_hold_operations or []
        ),
        supported_audio_codecs=supported_audio_codecs or [hfp.AudioCodec.CVSD],
    )

  async def _terminate_connection_from_ref(self) -> None:
    if not (
        dut_ref_acl := self.ref.device.find_connection_by_bd_addr(
            hci.Address(self.dut.address)
        )
    ):
      return

    self.logger.info("[REF] Terminate connection.")
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      await dut_ref_acl.disconnect()
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected,
          lambda e: (e.address == self.ref.address),
      )

  async def _wait_for_hfp_state(
      self, dut_cb: _Callback, state: _HfpState
  ) -> None:
    self.logger.info("[DUT] Wait for HFP state %s.", state)
    await dut_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged,
        lambda e: (e.address == self.ref.address and e.state == state),
    )

  def _setup_ag_device(self, configuration: hfp.AgConfiguration) -> None:
    def on_dlc(dlc: rfcomm.DLC):
      self.ref_hfp_protocols.put_nowait(hfp.AgProtocol(dlc, configuration))

    self.ref.device.sdp_service_records = {
        _HFP_AG_SDP_HANDLE: hfp.make_ag_sdp_records(
            service_record_handle=_HFP_AG_SDP_HANDLE,
            rfcomm_channel=rfcomm.Server(self.ref.device).listen(on_dlc),
            configuration=configuration,
        )
    }

  async def _connect_hfp_from_ref(
      self, config: hfp.AgConfiguration
  ) -> hfp.AgProtocol:
    if not (
        dut_ref_acl := self.ref.device.find_connection_by_bd_addr(
            hci.Address(self.dut.address)
        )
    ):
      self.logger.info("[REF] Connect.")
      dut_ref_acl = await self.ref.device.connect(
          self.dut.address,
          core.BT_BR_EDR_TRANSPORT,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      self.logger.info("[REF] Authenticate and encrypt connection.")
      await dut_ref_acl.authenticate()
      await dut_ref_acl.encrypt()

    sdp_record = await hfp.find_hf_sdp_record(dut_ref_acl)
    if not sdp_record:
      self.fail("DUT does not have HFP SDP record.")
    rfcomm_channel = sdp_record[0]

    self.logger.info("[REF] Found HFP RFCOMM channel %s.", rfcomm_channel)

    self.logger.info("[REF] Open RFCOMM Channel.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      multiplexer = await rfcomm.Client(dut_ref_acl).start()
      dlc = await multiplexer.open_dlc(rfcomm_channel)
    return hfp.AgProtocol(dlc, config)

  async def test_pair_and_connect(self) -> None:
    """Tests HFP connection establishment right after a pairing session.

    Test steps:
      1. Setup HFP on REF.
      2. Create bond from DUT.
      3. Wait HFP connected on DUT.(Android should autoconnect HFP as HF)
    """
    config = self._ag_configuration()
    self._setup_ag_device(config)

    self.logger.info("[DUT] Connect and pair REF.")
    with self.dut.bl4a.register_callback(bl4a_api.Module.HFP_HF) as dut_cb:
      await self.classic_connect_and_pair()

      self.logger.info("[DUT] Wait for HFP connected.")
      await self._wait_for_hfp_state(dut_cb, _HfpState.CONNECTED)

  async def test_paired_connect_outgoing(self) -> None:
    """Tests HFP connection establishment where pairing is not involved.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Terminate ACL connection.
      3. Trigger connection from DUT.
      4. Wait HFP connected on DUT.
      5. Disconnect from DUT.
      6. Wait HFP disconnected on DUT.
    """
    await self.test_pair_and_connect()
    await self._terminate_connection_from_ref()

    with self.dut.bl4a.register_callback(bl4a_api.Module.HFP_HF) as dut_cb:

      self.logger.info("[DUT] Reconnect.")
      self.dut.bt.connect(self.ref.address)
      await self._wait_for_hfp_state(dut_cb, _HfpState.CONNECTED)

      self.logger.info("[DUT] Disconnect.")
      self.dut.bt.disconnect(self.ref.address)
      await self._wait_for_hfp_state(dut_cb, _HfpState.DISCONNECTED)

  async def test_paired_connect_incoming(self) -> None:
    """Tests HFP connection establishment where pairing is not involved.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Terminate ACL connection.
      3. Trigger connection from REF.
      4. Wait HFP connected on DUT.
      5. Disconnect from REF.
      6. Wait HFP disconnected on DUT.
    """
    configuration = self._ag_configuration()
    await self.test_pair_and_connect()
    await self._terminate_connection_from_ref()

    with self.dut.bl4a.register_callback(bl4a_api.Module.HFP_HF) as dut_cb:
      await self._connect_hfp_from_ref(configuration)

      self.logger.info("[DUT] Wait for HFP connected.")
      await self._wait_for_hfp_state(dut_cb, _HfpState.CONNECTED)

      await self._terminate_connection_from_ref()
      await self._wait_for_hfp_state(dut_cb, _HfpState.DISCONNECTED)

  @navi_test_base.parameterized(
      hfp.AudioCodec.CVSD,
      hfp.AudioCodec.MSBC,
      hfp.AudioCodec.LC3_SWB,
  )
  async def test_sco_connection_with_codec_negotiation(
      self, codec: hfp.AudioCodec
  ) -> None:
    """Tests SCO connection establishment.

    Test steps:
      1. Setup pairing between DUT and REF.
      2. Make SCO connection from AG(REF).
      3. Terminate SCO connection from AG(REF).

    Args:
      codec: Codec used in the SCO connection.
    """

    self._setup_ag_device(
        self._ag_configuration(
            supported_audio_codecs=[
                hfp.AudioCodec.CVSD,
                hfp.AudioCodec.MSBC,
                hfp.AudioCodec.LC3_SWB,
            ],
            supported_ag_features=[
                hfp.AgFeature.ENHANCED_CALL_STATUS,
                hfp.AgFeature.CODEC_NEGOTIATION,
            ],
        )
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
        if self.dut.getprop(_PROPERTY_SWB_SUPPORTED) != "true":
          raise signals.TestSkip("SWB is not supported on the device.")
        esco_parameters = hfp_ext.ESCO_PARAMETERS_LC3_T2
      case _:
        self.fail(f"Unsupported codec: {codec}")

    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.HFP_HF) as dut_hfp_cb,
    ):
      await self.classic_connect_and_pair()

      self.logger.info("[DUT] Wait for HFP connected.")
      await self._wait_for_hfp_state(dut_hfp_cb, _HfpState.CONNECTED)

      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_hfp_protocol = await self.ref_hfp_protocols.get()

      # WearService may disable the audio route.
      self.dut.bt.hfpHfSetAudioRouteAllowed(self.ref.address, True)

      self.logger.info("[REF] Negotiate codec.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await ref_hfp_protocol.negotiate_codec(codec)

      self.logger.info("[REF] Create SCO.")
      connection = ref_hfp_protocol.dlc.multiplexer.l2cap_channel.connection
      sco_links = asyncio.Queue[bumble_device.ScoLink]()
      self.ref.device.on("sco_connection", sco_links.put_nowait)
      await self.ref.device.send_command(
          hci.HCI_Enhanced_Setup_Synchronous_Connection_Command(
              connection_handle=connection.handle, **esco_parameters.asdict()
          )
      )

      self.logger.info("[DUT] Wait for SCO connected.")
      await dut_hfp_cb.wait_for_event(
          bl4a_api.HfpHfAudioStateChanged,
          lambda e: (
              e.address == self.ref.address and e.state == _HfpState.CONNECTED
          ),
      )

      self.logger.info("[REF] Terminate SCO.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        sco_link = await sco_links.get()
        await sco_link.disconnect()

      self.logger.info("[DUT] Wait for SCO disconnected.")
      await dut_hfp_cb.wait_for_event(
          bl4a_api.HfpHfAudioStateChanged,
          lambda e: (
              e.address == self.ref.address
              and e.state == _HfpState.DISCONNECTED
          ),
      )

  @navi_test_base.parameterized(
      constants.TestRole.DUT,
      constants.TestRole.REF,
  )
  async def test_set_volume(self, issuer: constants.TestRole) -> None:
    """Tests setting speaker volume over HFP.

    Test steps:
      1. Setup HFP connection between DUT and REF.
      2. Set volume from DUT or REF.
      3. Check the volume on the other side.

    Args:
      issuer: Device which requests the volume.
    """
    configuration = self._ag_configuration()
    self._setup_ag_device(configuration)

    max_system_call_volume = self.dut.bt.getMaxVolume(_STREAM_TYPE_CALL)
    min_system_call_volume = self.dut.bt.getMinVolume(_STREAM_TYPE_CALL)

    def system_call_volume_to_hfp_volume(system_call_volume: int) -> int:
      return (
          (system_call_volume - min_system_call_volume)
          * (_MAX_HFP_VOLUME - _MIN_HFP_VOLUME)
          // (max_system_call_volume - min_system_call_volume)
      ) + _MIN_HFP_VOLUME

    # TODO: Remove volume reset before test when conversion is
    # fixed.
    self.dut.bt.setVolume(_STREAM_TYPE_CALL, 2)
    expect_dut_volume = max(
        min_system_call_volume,
        (self.dut.bt.getVolume(_STREAM_TYPE_CALL) + 1)
        % (max_system_call_volume + 1),
    )
    expect_hfp_volume = system_call_volume_to_hfp_volume(expect_dut_volume)

    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.HFP_HF) as dut_hfp_cb,
        self.dut.bl4a.register_callback(bl4a_api.Module.AUDIO) as dut_audio_cb,
    ):
      await self.classic_connect_and_pair()

      ref_volumes = asyncio.Queue[int]()
      self.logger.info("[REF] Wait for HFP connected.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_hfp_protocol = await self.ref_hfp_protocols.get()
      ref_hfp_protocol.on("speaker_volume", ref_volumes.put_nowait)

      self.logger.info("[DUT] Wait for HFP connected.")
      await self._wait_for_hfp_state(dut_hfp_cb, _HfpState.CONNECTED)

      self.logger.info("[REF] Wait for initial volume.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await ref_volumes.get()

      if issuer == constants.TestRole.DUT:
        self.logger.info("[DUT] Set volume to %d.", expect_dut_volume)
        self.dut.bt.setVolume(_STREAM_TYPE_CALL, expect_dut_volume)

        self.logger.info("[REF] Wait for volume changed.")
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          actual_hfp_volume = await ref_volumes.get()
          self.assertEqual(actual_hfp_volume, expect_hfp_volume)
      else:
        self.logger.info("[REF] Set volume to %d.", expect_hfp_volume)
        ref_hfp_protocol.set_speaker_volume(expect_hfp_volume)

      self.logger.info("[DUT] Wait for volume changed.")
      volume_changed_event = await dut_audio_cb.wait_for_event(
          bl4a_api.VolumeChanged, lambda e: e.stream_type == _STREAM_TYPE_CALL
      )
      self.assertEqual(volume_changed_event.volume_value, expect_dut_volume)

  async def test_update_battery_level(self) -> None:
    """Tests updating battery level indicator from HF.

    Test steps:
      1. Setup HFP connection between DUT and REF.
      2. Update battery indicator from HF.
      3. Check the battery indicator from AG.
    """
    configuration = self._ag_configuration(
        supported_ag_features=[
            hfp.AgFeature.HF_INDICATORS,
            hfp.AgFeature.ENHANCED_CALL_STATUS,
            # TODO: Remove this feature when feature check is
            # fixed on Android.
            hfp.AgFeature.EXTENDED_ERROR_RESULT_CODES,
        ],
        supported_hf_indicators=[
            hfp.HfIndicator.BATTERY_LEVEL,
        ],
    )
    self._setup_ag_device(configuration)
    # Mock battery level to avoid unexpected changing during the test.
    initial_battery_level = 50
    self.dut.shell(f"dumpsys battery set level {initial_battery_level}")

    with self.dut.bl4a.register_callback(bl4a_api.Module.HFP_HF) as dut_cb:
      await self.classic_connect_and_pair()

      self.logger.info("[REF] Wait for HFP connected.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_hfp_protocol = await self.ref_hfp_protocols.get()

      hf_indicators = asyncio.Queue[hfp.HfIndicatorState]()
      ref_hfp_protocol.on("hf_indicator", hf_indicators.put_nowait)

      self.logger.info("[DUT] Wait for HFP connected.")
      await self._wait_for_hfp_state(dut_cb, _HfpState.CONNECTED)

      self.logger.info("[REF] Wait for initial battery level.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        hf_indicator = await hf_indicators.get()
        # The battery initial battery level cannot be mocked, so we don't check
        # the value here.
        self.assertEqual(hf_indicator.indicator, hfp.HfIndicator.BATTERY_LEVEL)

      # Set battery level.
      expected_battery_level = 100
      self.dut.shell(f"dumpsys battery set level {expected_battery_level}")

      self.logger.info("[REF] Wait for updated battery level.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        hf_indicator = await hf_indicators.get()
        self.assertEqual(hf_indicator.indicator, hfp.HfIndicator.BATTERY_LEVEL)
        self.assertEqual(hf_indicator.current_status, expected_battery_level)


if __name__ == "__main__":
  test_runner.main()
