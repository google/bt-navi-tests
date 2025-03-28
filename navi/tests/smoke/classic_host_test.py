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
import datetime

from bumble import core
from bumble import hci
from mobly import asserts
from mobly import test_runner

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import pyee_extensions


class ClassicHostTest(navi_test_base.TwoDevicesTestBase):

  @navi_test_base.retry(max_count=2)
  async def test_outgoing_classic_acl(self) -> None:
    """Test outgoing Classic ACL connection.

    Test steps:
      1. Create connection from DUT.
      2. Wait for ACL connected on both devices.
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      self.dut.bt.createBond(
          self.ref.address, android_constants.Transport.CLASSIC
      )

      await self.ref.device.accept(
          f'{self.dut.address}/P',
          timeout=datetime.timedelta(seconds=15).total_seconds(),
      )
      await dut_cb.wait_for_event(
          bl4a_api.AclConnected, lambda e: (e.address == self.ref.address)
      )
      # disconnect() doesn't work, because it can only remove profile
      # connections.
      self.dut.bt.cancelBond(self.ref.address)
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected,
          predicate=lambda e: (e.address == self.ref.address),
          timeout=datetime.timedelta(seconds=30),
      )

  @navi_test_base.retry(max_count=2)
  async def test_incoming_classic_acl(self) -> None:
    """Test incoming Classic ACL connection.

    Test steps:
      1. Create connection from REF.
      2. Wait for ACL connected on both devices.
      3. Disconnect from REF.
      4. Wait for ACL disconnected on both devices.
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      ref_dut_acl = await self.ref.device.connect(
          f'{self.dut.address}/P', transport=core.BT_BR_EDR_TRANSPORT
      )

      await dut_cb.wait_for_event(
          bl4a_api.AclConnected, lambda e: (e.address == self.ref.address)
      )

      await ref_dut_acl.disconnect()
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected, lambda e: (e.address == self.ref.address)
      )

  @navi_test_base.retry(max_count=2)
  async def test_inquiry(self) -> None:
    """Test inquiry.

    Test steps:
      1. Set REF in discoverable mode.
      2. Start discovery on DUT.
      3. Wait for DUT discovered or timeout(15 seconds).
      4. Check result(should be discovered).
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      await self.ref.device.set_discoverable(True)
      self.dut.bt.startInquiry()

      await dut_cb.wait_for_event(
          bl4a_api.DeviceFound, lambda e: (e.address == self.ref.address)
      )

  async def test_discoverable(self) -> None:
    """Test DUT in discoverable mode.

    Test steps:
      1. Set DUT in discoverable mode.
      2. Start discovery on REF.
      3. Wait for DUT discovered or timeout(15 seconds).
      4. Check result(should be discovered).
    """
    self.dut.bt.setScanMode(android_constants.ScanMode.CONNECTABLE_DISCOVERABLE)

    with pyee_extensions.EventWatcher() as watcher:
      inquiry_future = asyncio.events.get_running_loop().create_future()

      @watcher.on(self.ref.device, 'inquiry_result')
      def on_inquiry_result(address: hci.Address, *_) -> None:
        if address == hci.Address(f'{self.dut.address}/P'):
          inquiry_future.set_result(None)

      await self.ref.device.start_discovery()
      await asyncio.tasks.wait_for(
          inquiry_future, timeout=datetime.timedelta(seconds=15).total_seconds()
      )

  async def test_not_discoverable(self) -> None:
    """Test DUT in not discoverable mode.

    Test steps:
      1. Set DUT in not discoverable mode.
      2. Start discovery on REF.
      3. Wait for DUT discovered or timeout(15 seconds).
      4. Check result(should not be discovered).
    """
    self.dut.bt.setScanMode(android_constants.ScanMode.NONE)

    with pyee_extensions.EventWatcher() as watcher:
      inquiry_future = asyncio.events.get_running_loop().create_future()

      @watcher.on(self.ref.device, 'inquiry_result')
      def on_inquiry_result(address: hci.Address, *_) -> None:
        if address == hci.Address(f'{self.dut.address}/P'):
          inquiry_future.set_result(None)

      await self.ref.device.start_discovery()
      with contextlib.suppress(asyncio.exceptions.TimeoutError):
        await asyncio.tasks.wait_for(
            inquiry_future,
            timeout=datetime.timedelta(seconds=15).total_seconds(),
        )
        asserts.assert_is_none(inquiry_future.result())


if __name__ == '__main__':
  test_runner.main()
