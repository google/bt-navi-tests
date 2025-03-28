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

from bumble import core
from bumble import hci
from mobly import test_runner
from typing_extensions import override

from navi.tests import navi_test_base
from navi.tests.benchmark import performance_tool
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import retry

_DEFAULT_REPEAT_TIMES = 100
_ADVERTISING_INTERVAL_MIN = 20
_SETUP_TIMEOUT_SEC = 10.0

_Callback = bl4a_api.CallbackHandler


class LeGapTest(navi_test_base.TwoDevicesTestBase):

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    @retry.retry_on_exception()
    async def setup_inner() -> None:
      async with self.assert_not_timeout(_SETUP_TIMEOUT_SEC):
        await self.ref.open()

      self.assertTrue(self.dut.bt.enable())
      self.assertTrue(self.dut.bt.factoryReset())

    await setup_inner()

  @retry.retry_on_exception()
  async def _cleanup_connections(self) -> None:
    # There might be crash during the test.
    # Make sure Bluetooth is enabled before test start.
    self.assertTrue(self.dut.bt.enable())

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      ref_address = self.ref.address
      connected_on_transport = [
          transport
          for transport in [
              android_constants.Transport.LE,
              android_constants.Transport.CLASSIC,
          ]
          if self.dut.bt.getDeviceConnected(ref_address, transport)
      ]
      async with self.assert_not_timeout(_SETUP_TIMEOUT_SEC):
        await self.ref.reset()
      for transport in connected_on_transport:
        await dut_cb.wait_for_event(
            bl4a_api.AclDisconnected,
            lambda e: (
                e.transport == transport and e.address == ref_address  # pylint: disable=cell-var-from-loop
            ),
        )
    # Leave a gap between tests.
    await asyncio.sleep(1.0)

  async def test_le_connection_outgoing(self) -> None:
    """Test make outgoing LE connections."""
    success_count = 0
    for _ in range(_DEFAULT_REPEAT_TIMES):
      try:
        await self.ref.device.start_advertising(
            own_address_type=hci.OwnAddressType.RANDOM,
            auto_restart=False,
            advertising_interval_min=_ADVERTISING_INTERVAL_MIN,
            advertising_interval_max=_ADVERTISING_INTERVAL_MIN,
        )
        with performance_tool.Stopwatch() as stop_watch:
          client = await self.dut.bl4a.connect_gatt_client(
              self.ref.random_address,
              transport=android_constants.Transport.LE,
              address_type=android_constants.AddressTypeStatus.RANDOM,
              retry_count=0,
          )
        self.logger.info(
            "Success connection in %.2f seconds",
            stop_watch.elapsed_time.total_seconds(),
        )
        await client.disconnect()
        client.close()
        success_count += 1
      except (core.BaseBumbleError, AssertionError):
        self.logger.exception("Failed to make LE connection")
      finally:
        await self._cleanup_connections()
    self.logger.info(
        "Passes: %d / Attempts: %d", success_count, _DEFAULT_REPEAT_TIMES
    )
    self.record_data({
        "Test Name": self.current_test_info.name,
        "properties": {
            "passes": success_count,
            "attempts": _DEFAULT_REPEAT_TIMES,
        },
    })


if __name__ == "__main__":
  test_runner.main()
