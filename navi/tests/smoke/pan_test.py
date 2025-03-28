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

"""Tests for Personal Area Network (PAN) profile implementation on Android."""

import asyncio
from typing import TypeAlias

from bumble import core
from mobly import test_runner
from mobly import signals

from navi.bumble_ext import pan
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api

_PROPERTY_PAN_NAP_ENABLED = "bluetooth.profile.pan.nap.enabled"
_PROPERTY_PAN_PANU_ENABLED = "bluetooth.profile.pan.panu.enabled"
_DEFAULT_FRAME_TIMEOUT_SECONDS = 30.0

_CallbackHandler: TypeAlias = bl4a_api.CallbackHandler
_Module: TypeAlias = bl4a_api.Module


class PanTest(navi_test_base.TwoDevicesTestBase):
  panu_enabled: bool
  nap_enabled: bool

  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    self.panu_enabled = self.dut.getprop(_PROPERTY_PAN_PANU_ENABLED) == "true"
    self.nap_enabled = self.dut.getprop(_PROPERTY_PAN_NAP_ENABLED) == "true"

    if not self.panu_enabled and not self.nap_enabled:
      raise signals.TestAbortClass("PANU and NAP are both disabled.")

  async def test_panu_connection(self) -> None:
    """Tests making a PAN connection from REF(PANU) to DUT(NAP).

    When PAN is connected, Tethering service on DUT should properly route
    network traffic to PAN and send some frames like ARP or DHCP.

    Test steps:
      1. Pair DUT and REF.
      2. Connect PAN from DUT(PANU) to REF(NAP).
      3. Wait for Ethernet frame from DUT.
    """
    if not self.panu_enabled:
      self.skipTest("PANU is disabled.")

    ref_pan_connection_result: asyncio.Future[pan.Connection] = (
        asyncio.get_running_loop().create_future()
    )
    ref_pan_server = pan.Server(self.ref.device)
    ref_pan_server.on("connection", ref_pan_connection_result.set_result)
    self.ref.device.sdp_service_records = {
        1: pan.make_panu_service_record(1),
        2: pan.make_gn_service_record(2),
    }
    await self.classic_connect_and_pair()

    with self.dut.bl4a.register_callback(_Module.PAN) as dut_cb:
      self.logger.info("[DUT] Connect PANU.")
      self.dut.bt.setPanConnectionPolicy(
          self.ref.address, android_constants.ConnectionPolicy.ALLOWED
      )

      self.logger.info("[REF] Wait for PANU connection.")
      ref_pan_connection = await ref_pan_connection_result
      # Set up the Ethernet frame sink.
      ref_frame_queue = asyncio.Queue[pan.EthernetFrame]()
      ref_pan_connection.ethernet_sink = ref_frame_queue.put_nowait

      self.logger.info("[DUT] Wait for PANU connection.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: e.state == android_constants.ConnectionState.CONNECTED,
      )

      self.logger.info("[REF] Wait for Ethernet frame from DUT.")
      async with self.assert_not_timeout(_DEFAULT_FRAME_TIMEOUT_SECONDS):
        await ref_frame_queue.get()

  async def test_nap_connection(self) -> None:
    """Tests making a PAN connection from DUT(PANU) to REF(NAP).

    When PAN is connected, Tethering service on DUT should properly route
    network traffic to PAN and send some frames like ARP or DHCP.

    Test steps:
      1. Pair DUT and REF.
      2. Enable NAP on DUT.
      3. Connect PAN from REF(PANU) to DUT(NAP).
      4. Wait for Ethernet frame from DUT.
    """
    if not self.nap_enabled:
      self.skipTest("NAP is disabled.")

    self.ref.device.sdp_service_records = {
        1: pan.make_panu_service_record(1),
        2: pan.make_gn_service_record(2),
    }
    ref_dut_acl = await self.classic_connect_and_pair()

    # Enable NAP on DUT.
    self.dut.bt.setPanTetheringEnabled(True)

    with self.dut.bl4a.register_callback(_Module.PAN) as dut_cb:
      self.logger.info("[DUT] Connect PANU.")
      ref_pan_connection = await pan.Connection.connect(
          ref_dut_acl,
          source_service=core.BT_PANU_SERVICE,
          destination_service=core.BT_NAP_SERVICE,
      )
      ref_frame_queue = asyncio.Queue[pan.EthernetFrame]()
      ref_pan_connection.ethernet_sink = ref_frame_queue.put_nowait

      self.logger.info("[DUT] Wait for PANU connection.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: e.state == android_constants.ConnectionState.CONNECTED,
      )

      self.logger.info("[REF] Wait for Ethernet frame from DUT.")
      async with self.assert_not_timeout(_DEFAULT_FRAME_TIMEOUT_SECONDS):
        await ref_frame_queue.get()


if __name__ == "__main__":
  test_runner.main()
