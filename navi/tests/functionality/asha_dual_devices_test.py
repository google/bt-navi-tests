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
import contextlib
import enum
import functools
from typing import TypeAlias, cast

from bumble import core
from bumble import device
from bumble import hci
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import asha
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import pyee_extensions

_DEFAULT_STEP_TIMEOUT_SECONDS = 5.0
_DEFAULT_ADVERTISING_INTERVAL = 100
_STREAMING_TIME_SECONDS = 1.0
_PROPERTY_ASHA_ENABLED = 'bluetooth.profile.asha.central.enabled'
_HISYNC_ID = bytes([0x12, 0x34, 0x56, 0x78, 0x90, 0xAB, 0xCD, 0xEF])
_DEFAULT_ADVERTISING_PARAMETERS = device.AdvertisingParameters(
    own_address_type=hci.OwnAddressType.RANDOM,
    primary_advertising_interval_min=_DEFAULT_ADVERTISING_INTERVAL,
    primary_advertising_interval_max=_DEFAULT_ADVERTISING_INTERVAL,
)

_Module: TypeAlias = bl4a_api.Module
_CallbackHandler: TypeAlias = bl4a_api.CallbackHandler


@enum.unique
class _StreamType(enum.Enum):
  MEDIA = enum.auto()
  VOICE_COMM = enum.auto()


class AshaDualDevicesTest(navi_test_base.MultiDevicesTestBase):
  NUM_REF_DEVICES = 2
  ref_asha_services: list[asha.AshaService] = []

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if self.dut.getprop(_PROPERTY_ASHA_ENABLED) != 'true':
      raise signals.TestAbortClass('ASHA is not supported on DUT.')

  @override
  async def async_setup_test(self) -> None:
    self.ref_asha_services = []
    await super().async_setup_test()
    await self._prepare_paired_devices()

  async def _prepare_paired_devices(self) -> None:
    """Pairs DUT with REF devices."""

    for i, dev in enumerate(self.refs):
      if i == 0:
        device_capabilities = asha.DeviceCapabilities.IS_DUAL
      else:
        device_capabilities = (
            asha.DeviceCapabilities.IS_DUAL | asha.DeviceCapabilities.IS_RIGHT
        )
      asha_service = asha.AshaService(
          capability=asha.DeviceCapabilities(device_capabilities),
          hisyncid=_HISYNC_ID,
          device=dev.device,
      )
      self.ref_asha_services.append(asha_service)
      dev.device.add_service(asha_service)

    with self.dut.bl4a.register_callback(_Module.ASHA) as dut_cb:
      for ref in self.refs:
        await self.le_connect_and_pair(
            ref_address_type=hci.OwnAddressType.RANDOM,
            ref=ref,
        )
        self.logger.info(
            '[DUT] Wait for ASHA connected to %s', ref.random_address
        )
        await dut_cb.wait_for_event(
            callback_type=bl4a_api.ProfileConnectionStateChanged,
            predicate=lambda e: (
                e.address == ref.random_address  # pylint: disable=cell-var-from-loop
                and e.state == android_constants.ConnectionState.CONNECTED
            ),
        )

  async def test_active_devices_should_contain_both_sides(self) -> None:
    """Tests that both sides of the dual device are active."""
    self.assertCountEqual(
        self.dut.bt.getActiveDevices(android_constants.Profile.HEARING_AID),
        [ref.random_address for ref in self.refs],
    )

  @navi_test_base.retry(max_count=3)
  async def test_reconnect(self) -> None:
    """Tests reconnecting ASHA from DUT to REF devices.

    Test steps:
      1. Disconnect ACL from REF devices.
      2. Restart advertising on REF devices.
      3. Wait for DUT to reconnect to REF devices.
    """

    with self.dut.bl4a.register_callback(_Module.ADAPTER) as dut_cb:
      for ref in self.refs:
        ref_address = ref.random_address
        if not (
            acl := ref.device.find_connection_by_bd_addr(
                hci.Address(self.dut.address),
                transport=core.BT_LE_TRANSPORT,
            )
        ):
          continue
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          await acl.disconnect()
        self.logger.info('[DUT] Wait for ACL disconnected from %s', ref_address)
        await dut_cb.wait_for_event(
            bl4a_api.AclDisconnected,
            predicate=lambda e: (e.address == ref_address),  # pylint: disable=cell-var-from-loop
        )

    with self.dut.bl4a.register_callback(_Module.ASHA) as dut_cb:
      for ref, asha_service in zip(self.refs, self.ref_asha_services):
        ref_address = ref.random_address
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          await ref.device.create_advertising_set(
              advertising_parameters=_DEFAULT_ADVERTISING_PARAMETERS,
              advertising_data=asha_service.get_advertising_data(),
          )
        if ref_address in self.dut.bt.getActiveDevices(
            android_constants.Profile.HEARING_AID
        ):
          self.logger.info('[DUT] ASHA already connected to %s', ref_address)
        else:
          self.logger.info('[DUT] Wait for ASHA connected to %s', ref_address)
          await dut_cb.wait_for_event(
              callback_type=bl4a_api.ProfileConnectionStateChanged,
              predicate=lambda e: (
                  e.address == ref_address  # pylint: disable=cell-var-from-loop
                  and e.state == android_constants.ConnectionState.CONNECTED
              ),
          )

  @navi_test_base.parameterized(_StreamType.MEDIA, _StreamType.VOICE_COMM)
  async def test_streaming(self, stream_type: _StreamType) -> None:
    """Tests ASHA streaming.

    Test Steps:
      1. Establish ASHA connection.
      2. (Optional) Start phone call.
      3. Start streaming.
      4. Verify audio data is received.
      5. Stop streaming.

    Args:
      stream_type: The stream type to test.
    """

    audio_sinks = [asyncio.Queue[bytes](), asyncio.Queue[bytes]()]

    for asha_service, audio_sink in zip(self.ref_asha_services, audio_sinks):
      asha_service.audio_sink = audio_sink.put_nowait

    watcher = pyee_extensions.EventWatcher()
    start_event_lists = [
        watcher.async_monitor(asha_service, asha.AshaService.Event.STARTED)
        for asha_service in self.ref_asha_services
    ]
    stop_event_lists = [
        watcher.async_monitor(asha_service, asha.AshaService.Event.STOPPED)
        for asha_service in self.ref_asha_services
    ]

    with contextlib.ExitStack() as exit_stack:
      if stream_type == _StreamType.VOICE_COMM:
        self.logger.info('[DUT] Start phone call')
        exit_stack.enter_context(
            self.dut.bl4a.make_phone_call(
                caller_name='Pixel Bluetooth',
                caller_number='123456789',
                direction=constants.Direction.OUTGOING,
            )
        )

      self.logger.info('[DUT] Start streaming')
      await asyncio.to_thread(self.dut.bt.audioPlaySine)
      for i in range(len(self.refs)):
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          self.logger.info('[REF] Wait for audio started on %dth device', i)
          await start_event_lists[i].get()
          self.logger.info('[REF] Wait for audio data on %dth device', i)
          await audio_sinks[i].get()

      await asyncio.sleep(_STREAMING_TIME_SECONDS)

      self.logger.info('[DUT] Stop streaming')
      await asyncio.to_thread(self.dut.bt.audioStop)
      for i in range(len(self.refs)):
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          self.logger.info('[REF] Wait for audio stopped on %dth device', i)
          await stop_event_lists[i].get()

  async def test_set_volume(self) -> None:
    """Tests ASHA set volume.

    Test Steps:
      1. Establish ASHA connection.
      2. Set volume to min.
      3. Verify volume changed to -128.
      4. Set volume to max.
      5. Verify volume changed to 0.
    """
    stream_type = android_constants.StreamType.MUSIC

    volume_lists = [
        pyee_extensions.EventTriggeredValueObserver(
            ref_asha_service,
            asha.AshaService.Event.VOLUME_CHANGED,
            functools.partial(
                lambda service: cast(asha.AshaService, service).volume,
                ref_asha_service,
            ),
        )
        for ref_asha_service in self.ref_asha_services
    ]

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info('[DUT] Set volume to min')
      self.dut.bt.setVolume(stream_type, self.dut.bt.getMinVolume(stream_type))
      for i in range(len(self.refs)):
        self.logger.info('[REF] Wait for volume changed on %dth device', i)
        await volume_lists[i].wait_for_target_value(-128)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info('[DUT] Set volume to max')
      self.dut.bt.setVolume(stream_type, self.dut.bt.getMaxVolume(stream_type))
      for i in range(len(self.refs)):
        self.logger.info('[REF] Wait for volume changed on %dth device', i)
        await volume_lists[i].wait_for_target_value(0)


if __name__ == '__main__':
  test_runner.main()
