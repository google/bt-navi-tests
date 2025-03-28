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
import datetime
import enum

from bumble import core
from bumble import hci
from bumble import l2cap
from bumble import pairing
from mobly import test_runner
from mobly import records
from typing_extensions import override

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api


class Variant(enum.Enum):
  SECURE = "secure"
  INSECURE = "insecure"


_PairingDelegate = pairing.PairingDelegate
_DEFAULT_TIMEOUT_SECONDS = 5.0
_TEST_DATA = bytes(i % 256 for i in range(10000))


class L2capTest(navi_test_base.TwoDevicesTestBase):
  _ANDROID_AUTO_ALLOCATE_PSM = -2

  @override
  def on_fail(self, record: records.TestResultRecord) -> None:
    super().on_fail(record)
    self.dut.reload_snippet()

  async def _setup_le_pairing(self) -> None:
    await self.le_connect_and_pair(hci.OwnAddressType.RANDOM)

    # Terminate ACL connection after pairing.
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      self.logger.info("[DUT] Wait for disconnected.")
      ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
          hci.Address(self.dut.address),
          core.BT_LE_TRANSPORT,
      )
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        if ref_dut_acl:
          await ref_dut_acl.disconnect()
        await self.ref.device.power_off()
        await self.ref.device.power_on()
      await dut_cb.wait_for_event(bl4a_api.AclDisconnected)

  async def _test_transmission(
      self,
      ref_dut_l2cap_channel: l2cap.LeCreditBasedChannel,
      dut_ref_l2cap_channel: bl4a_api.L2capChannel,
  ) -> None:
    # Store received SDUs in queue.
    ref_sdu_rx_queue = asyncio.Queue[bytes]()
    ref_dut_l2cap_channel.sink = ref_sdu_rx_queue.put_nowait

    self.logger.info("Start sending data from REF to DUT")
    ref_dut_l2cap_channel.write(_TEST_DATA)
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      data_read = await dut_ref_l2cap_channel.read(len(_TEST_DATA))
    self.assertEqual(data_read, _TEST_DATA)

    async def ref_rx_task() -> bytearray:
      data_read = bytearray()
      while len(data_read) < len(_TEST_DATA):
        data_read += await ref_sdu_rx_queue.get()
      return data_read

    self.logger.info("Start sending data from DUT to REF")
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      _, data_read = await asyncio.gather(
          dut_ref_l2cap_channel.write(_TEST_DATA),
          ref_dut_l2cap_channel.connection.abort_on(
              "disconnection", ref_rx_task()
          ),
      )
      self.assertEqual(data_read, _TEST_DATA)

  @navi_test_base.parameterized(Variant.SECURE, Variant.INSECURE)
  @navi_test_base.retry(3)
  async def test_incoming_connection(self, variant: Variant) -> None:
    """Test L2CAP incoming connection, read and write.

    Typical duration: 30-60s.

    Test steps:
      1. Open L2CAP server on DUT.
      2. Start advertising on DUT.
      3. Connect ACL from REF to DUT.
      4. Connect L2CAP from REF to DUT.
      5. Transmit data from REF to DUT.
      6. Transmit data from DUT to REF.

    Args:
      variant: Whether encryption is required.
    """
    if variant == Variant.SECURE:
      await self._setup_le_pairing()

    secure = variant == Variant.SECURE

    server = self.dut.bl4a.create_l2cap_server(
        secure=secure,
        transport=android_constants.Transport.LE,
    )
    self.logger.info("[DUT] Listen L2CAP on PSM %d", server.psm)

    self.logger.info("[DUT] Start advertising.")
    await self.dut.bl4a.start_legacy_advertiser(
        settings=bl4a_api.LegacyAdvertiseSettings(
            own_address_type=android_constants.AddressTypeStatus.PUBLIC
        ),
    )

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self.ref.device.connect(
        f"{self.dut.address}/P",
        transport=core.BT_LE_TRANSPORT,
        timeout=datetime.timedelta(seconds=15).total_seconds(),
        own_address_type=hci.OwnAddressType.RANDOM,
    )

    # Workaround: Request feature exchange to avoid connection failure.
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      await ref_dut_acl.get_remote_le_features()

    if secure:
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        await ref_dut_acl.encrypt(True)

    self.logger.info("[REF] Connect L2CAP channel to DUT.")
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      ref_dut_l2cap_channel, dut_ref_l2cap_channel = await asyncio.gather(
          ref_dut_acl.create_l2cap_channel(
              l2cap.LeCreditBasedChannelSpec(psm=server.psm)
          ),
          server.accept(),
      )

    await self._test_transmission(ref_dut_l2cap_channel, dut_ref_l2cap_channel)

  @navi_test_base.parameterized(Variant.SECURE, Variant.INSECURE)
  async def test_outgoing_connection(self, variant: Variant) -> None:
    """Test L2CAP outgoing connection, read and write.

    Typical duration: 30-60s.

    Test steps:
      1. Open L2CAP server on REF.
      2. Start advertising on REF.
      3. Connect L2CAP from REF to DUT.
      4. Transmit SDU from REF to DUT for 256 times.
      5. Transmit SDU from DUT to REF for 256 times.

    Args:
      variant: Whether encryption is required.
    """
    if variant == Variant.SECURE:
      await self._setup_le_pairing()

    secure = variant == Variant.SECURE

    ref_accept_future = asyncio.get_running_loop().create_future()
    server = self.ref.device.create_l2cap_server(
        spec=l2cap.LeCreditBasedChannelSpec(),
        handler=ref_accept_future.set_result,
    )
    self.logger.info("[REF] Listen L2CAP on PSM %d", server.psm)

    self.logger.info("[REF] Start advertising.")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM
    )

    # On some emulator images (at least until SDK Level 35), stack may not be
    # able to connect to a random address if it's not scanned yet.
    self.logger.info("[DUT] Start scanning for REF.")
    scanner = self.dut.bl4a.start_scanning(
        scan_filter=bl4a_api.ScanFilter(
            device=self.ref.random_address,
            address_type=android_constants.AddressTypeStatus.RANDOM,
        ),
    )
    with scanner:
      self.logger.info("[DUT] Wait for scan result.")
      await scanner.wait_for_event(bl4a_api.ScanResult)

    self.logger.info("[DUT] Connect L2CAP channel to REF.")
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      ref_dut_l2cap_channel, dut_ref_l2cap_channel = await asyncio.gather(
          ref_accept_future,
          self.dut.bl4a.create_l2cap_channel(
              address=self.ref.random_address,
              secure=secure,
              psm=server.psm,
              transport=android_constants.Transport.LE,
              address_type=android_constants.AddressTypeStatus.RANDOM,
          ),
      )

    await self._test_transmission(ref_dut_l2cap_channel, dut_ref_l2cap_channel)


if __name__ == "__main__":
  test_runner.main()
