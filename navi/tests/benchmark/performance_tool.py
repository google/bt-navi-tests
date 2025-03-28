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

"""Performance tool for Bluetooth tests."""

import asyncio
import datetime

from typing_extensions import Self

from navi.bumble_ext import crown
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import retry


_Callback = bl4a_api.CallbackHandler
_SETUP_TIMEOUT_SEC = 30.0


class Stopwatch:
  """A stopwatch that can be used to measure elapsed time."""

  start_time: datetime.datetime
  end_time: datetime.datetime | None = None

  def __init__(self) -> None:
    self.start_time = datetime.datetime.now()

  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type, exc_value, traceback):
    self.end_time = datetime.datetime.now()

  @property
  def elapsed_time(self) -> datetime.timedelta:
    return (self.end_time or datetime.datetime.now()) - self.start_time


@retry.retry_on_exception()
async def cleanup_connections(
    dut: navi_test_base.AndroidSnippetDeviceWrapper,
    ref: crown.CrownDevice,
) -> None:
  """Cleans up connections after finishing the test.

  Args:
    dut: The Android device wrapper.
    ref: The Crown device.
  """

  with dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
    ref_address = ref.address
    connected_on_transport = [
        transport
        for transport in [
            android_constants.Transport.CLASSIC,
            android_constants.Transport.LE,
        ]
        if dut.bt.getDeviceConnected(ref_address, transport)
    ]
    async with asyncio.timeout(_SETUP_TIMEOUT_SEC):
      await ref.reset()
    for transport in connected_on_transport:
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected,
          lambda e: (
              e.transport == transport and e.address == ref_address  # pylint: disable=cell-var-from-loop
          ),
      )
  # Leave a gap between tests.
  await asyncio.sleep(1.0)
