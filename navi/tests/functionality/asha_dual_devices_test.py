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
import contextlib
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

# pylint: disable=cell-var-from-loop

_DEFAULT_STEP_TIMEOUT_SECONDS = 5.0
_DEFAULT_ADVERTISING_INTERVAL = 100
_STREAMING_TIME_SECONDS = 1.0
_PROPERTY_ASHA_ENABLED = 'bluetooth.profile.asha.central.enabled'
_ASHA_MIN_VOLUME = -128
_ASHA_MAX_VOLUME = 0
_HISYNC_ID = bytes([0x12, 0x34, 0x56, 0x78, 0x90, 0xAB, 0xCD, 0xEF])
_DEFAULT_ADVERTISING_PARAMETERS = device.AdvertisingParameters(
    own_address_type=hci.OwnAddressType.RANDOM,
    primary_advertising_interval_min=_DEFAULT_ADVERTISING_INTERVAL,
    primary_advertising_interval_max=_DEFAULT_ADVERTISING_INTERVAL,
)

_Module: TypeAlias = bl4a_api.Module
_CallbackHandler: TypeAlias = bl4a_api.CallbackHandler


class AshaDualDevicesTest(navi_test_base.MultiDevicesTestBase):
  NUM_REF_DEVICES = 2
  ref_asha_services: list[asha.AshaService] = []

  @override
  async def async_setup_class(self) -> None:
    self.condition = asyncio.Condition()
    await super().async_setup_class()

    if self.dut.getprop(_PROPERTY_ASHA_ENABLED) != 'true':
      raise signals.TestAbortClass('ASHA is not supported on DUT.')

  @override
  async def async_setup_test(self) -> None:
    self.ref_asha_services = list[asha.AshaService]()
    await super().async_setup_test()
    await self._prepare_paired_devices()

    async def on_state_change() -> None:
      async with self.condition:
        self.condition.notify_all()

    watcher = pyee_extensions.EventWatcher()
    self.test_case_context.enter_context(watcher)
    for asha_service in self.ref_asha_services:
      watcher.on(asha_service, asha_service.Event.STARTED, on_state_change)
      watcher.on(asha_service, asha_service.Event.STOPPED, on_state_change)

    self.logger.info('Wait for all ASHA services to be stopped')
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      async with self.condition:
        await self.condition.wait_for(
            lambda: all(
                asha_service.active_codec is None
                for asha_service in self.ref_asha_services
            )
        )

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
            connect_profiles=True,
        )
        self.logger.info(
            '[DUT] Wait for ASHA connected to %s', ref.random_address
        )
        await dut_cb.wait_for_event(
            bl4a_api.ProfileConnectionStateChanged(
                address=ref.random_address,
                state=android_constants.ConnectionState.CONNECTED,
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
            bl4a_api.AclDisconnected(
                address=ref_address, transport=android_constants.Transport.LE
            )
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
              bl4a_api.ProfileConnectionStateChanged(
                  address=ref_address,
                  state=android_constants.ConnectionState.CONNECTED,
              ),
          )

  @navi_test_base.parameterized(
      bl4a_api.AudioAttributes.Usage.VOICE_COMMUNICATION,
      bl4a_api.AudioAttributes.Usage.MEDIA,
  )
  async def test_streaming(self, usage: bl4a_api.AudioAttributes.Usage) -> None:
    """Tests ASHA streaming.

    Test Steps:
      1. Establish ASHA connection.
      2. (Optional) Start phone call.
      3. Start streaming.
      4. Verify audio data is received.
      5. Stop streaming.

    Args:
      usage: The usage of stream to test.
    """

    audio_sinks = [asyncio.Queue[bytes](), asyncio.Queue[bytes]()]

    for asha_service, audio_sink in zip(self.ref_asha_services, audio_sinks):
      asha_service.audio_sink = audio_sink.put_nowait

    with contextlib.ExitStack() as exit_stack:
      if usage == bl4a_api.AudioAttributes.Usage.VOICE_COMMUNICATION:
        self.logger.info('[DUT] Start phone call')
        exit_stack.enter_context(
            self.dut.bl4a.make_phone_call(
                caller_name='Pixel Bluetooth',
                caller_number='123456789',
                direction=constants.Direction.OUTGOING,
            )
        )

      self.logger.info('[DUT] Set audio attributes.')
      self.dut.bl4a.set_audio_attributes(
          bl4a_api.AudioAttributes(usage=usage), handle_audio_focus=False
      )

      self.logger.info('[DUT] Start streaming')
      await asyncio.to_thread(self.dut.bt.audioPlaySine)
      self.test_case_context.callback(self.dut.bt.audioStop)
      for i in range(len(self.refs)):
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          self.logger.info('[REF-%d] Wait for audio started', i)
          async with self.condition:
            await self.condition.wait_for(
                lambda: self.ref_asha_services[i].active_codec is not None
            )
          self.logger.info('[REF-%d] Wait for audio data', i)
          await audio_sinks[i].get()

      await asyncio.sleep(_STREAMING_TIME_SECONDS)

      self.logger.info('[DUT] Stop streaming')
      await asyncio.to_thread(self.dut.bt.audioStop)
      for i in range(len(self.refs)):
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          self.logger.info('[REF-%d] Wait for audio stopped', i)
          async with self.condition:
            await self.condition.wait_for(
                lambda: self.ref_asha_services[i].active_codec is None
            )

  async def test_set_volume(self) -> None:
    """Tests ASHA set volume.

    Test Steps:
      1. Establish ASHA connection.
      2. Set volume to min.
      3. Verify volume changed to -128.
      4. Set volume to max.
      5. Verify volume changed to 0.
    """
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
      self.dut.bt.setAshaVolume(_ASHA_MIN_VOLUME)
      for i in range(len(self.refs)):
        self.logger.info('[REF-%d] Wait for volume changed', i)
        await volume_lists[i].wait_for_target_value(_ASHA_MIN_VOLUME)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info('[DUT] Set volume to max')
      self.dut.bt.setAshaVolume(_ASHA_MAX_VOLUME)
      for i in range(len(self.refs)):
        self.logger.info('[REF-%d] Wait for volume changed', i)
        await volume_lists[i].wait_for_target_value(_ASHA_MAX_VOLUME)


if __name__ == '__main__':
  test_runner.main()
