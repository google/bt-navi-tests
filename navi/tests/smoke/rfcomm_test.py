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
import uuid

from bumble import core
from bumble import device
from bumble import pairing
from bumble import rfcomm
from bumble import smp
from mobly import test_runner
from mobly import records
from typing_extensions import override

from navi.tests import navi_test_base
from navi.utils import bl4a_api

_DEFAULT_STEP_TIMEOUT_SECONDS = 5.0
_TRANSMISSION_TIMEOUT_SECONDS = 180.0
_TEST_DATA = bytes(i % 256 for i in range(10000))


@enum.unique
class _Variant(enum.Enum):
  SECURE = "secure"
  INSECURE = "insecure"


_PairingDelegate = pairing.PairingDelegate


class RfcommTest(navi_test_base.TwoDevicesTestBase):

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()

    # Using highest authentication level to allow secure sockets.
    @override
    def pairing_config_factory(
        connection: device.Connection,
    ) -> pairing.PairingConfig:
      del connection  # Unused parameter.
      return pairing.PairingConfig(
          delegate=_PairingDelegate(
              io_capability=(
                  _PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT
              )
          )
      )

    self.ref.device.pairing_config_factory = pairing_config_factory
    # Disable CTKD.
    self.ref.device.l2cap_channel_manager.deregister_fixed_channel(
        smp.SMP_BR_CID
    )
    # Clear SDP records.
    self.ref.device.sdp_service_records.clear()

  @override
  def on_fail(self, record: records.TestResultRecord):
    super().on_fail(record)
    self.dut.reload_snippet()

  async def _setup_pairing(self) -> None:
    ref_dut_acl = await self.classic_connect_and_pair()

    # Terminate ACL connection after pairing.
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      self.logger.info("[DUT] Wait for disconnected.")
      await ref_dut_acl.disconnect()
      await dut_cb.wait_for_event(bl4a_api.AclDisconnected)

    # Wait for 2 seconds to let controllers become idle.
    await asyncio.sleep(datetime.timedelta(seconds=2).total_seconds())

  async def _transmission_test(
      self,
      ref_dut_dlc: rfcomm.DLC,
      dut_ref_dlc: bl4a_api.RfcommChannel,
  ) -> None:
    """Tests transmissting data between DUT and REF over RFCOMM.

    (Not a standalone test.)

    Args:
      ref_dut_dlc: DLC instance of REF, connected to DUT.
      dut_ref_dlc: DLC token of DUT, connected to REF.
    """
    # Store received SDUs in queue.
    ref_sdu_rx_queue = asyncio.Queue[bytes]()
    ref_dut_dlc.sink = ref_sdu_rx_queue.put_nowait

    self.logger.info("Start sending data from REF to DUT")
    async with self.assert_not_timeout(_TRANSMISSION_TIMEOUT_SECONDS):
      ref_dut_dlc.write(_TEST_DATA)
      data_read = await dut_ref_dlc.read(len(_TEST_DATA))
      self.assertEqual(data_read, _TEST_DATA)

    async def ref_rx_task() -> bytearray:
      data_read = bytearray()
      while len(data_read) < len(_TEST_DATA):
        data_read += await ref_sdu_rx_queue.get()
      return data_read

    self.logger.info("Start sending data from DUT to REF")
    async with self.assert_not_timeout(_TRANSMISSION_TIMEOUT_SECONDS):
      data_read, _ = await asyncio.gather(
          ref_rx_task(),
          dut_ref_dlc.write(_TEST_DATA),
      )
      self.assertEqual(data_read, _TEST_DATA)

  @navi_test_base.parameterized(_Variant.SECURE, _Variant.INSECURE)
  async def test_incoming_connection(self, variant: _Variant) -> None:
    """Tests RFCOMM incoming connection, read and write.

    Typical duration: 30-60s.

    Test steps:
      1. Open RFCOMM server on DUT.
      2. Connect ACL from REF to DUT.
      3. Connect RFCOMM from REF to DUT.
      4. Transmit SDU from REF to DUT.
      5. Transmit SDU from DUT to REF.

    Args:
      variant: Whether Secure API is used. (They have the same behavior for
        Bluetooth device in version >=2.1)
    """
    await self._setup_pairing()

    self.logger.info("[DUT] Listen RFCOMM.")
    rfcomm_uuid = str(uuid.uuid4())
    server = self.dut.bl4a.create_rfcomm_server(
        rfcomm_uuid,
        secure=variant == _Variant.SECURE,
    )

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self.ref.device.connect(
        str(self.dut.address),
        transport=core.BT_BR_EDR_TRANSPORT,
    )
    await ref_dut_acl.authenticate()
    await ref_dut_acl.encrypt(True)

    self.logger.info("[REF] Find RFCOMM channel.")
    channel = await rfcomm.find_rfcomm_channel_with_uuid(
        ref_dut_acl, rfcomm_uuid
    )
    if not channel:
      self.fail("Failed to find RFCOMM channel with UUID.")

    self.logger.info("[REF] Connect RFCOMM channel to DUT.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_rfcomm = await rfcomm.Client(ref_dut_acl).start()

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_dut_dlc, dut_ref_dlc = await asyncio.gather(
          ref_rfcomm.open_dlc(channel),
          server.accept(),
      )

    await self._transmission_test(ref_dut_dlc, dut_ref_dlc)

  @navi_test_base.parameterized(_Variant.SECURE, _Variant.INSECURE)
  async def test_outgoing_connection(self, variant: _Variant) -> None:
    """Tests RFCOMM outgoing connection, read and write.

    Typical duration: 30-60s.

    Test steps:
      1. Open RFCOMM server on REF.
      2. Connect RFCOMM from REF to DUT.
      3. Transmit SDU from REF to DUT.
      4. Transmit SDU from DUT to REF.

    Args:
      variant: Whether Secure API is used. (They have the same behavior for
        Bluetooth device in version >=2.1)
    """
    await self._setup_pairing()

    ref_accept_future = asyncio.get_running_loop().create_future()
    channel = rfcomm.Server(self.ref.device).listen(
        acceptor=ref_accept_future.set_result
    )
    self.logger.info("[REF] Listen RFCOMM on channel %d.", channel)

    self.logger.info("[DUT] Connect RFCOMM channel to REF.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_dut_dlc, dut_ref_dlc = await asyncio.gather(
          ref_accept_future,
          self.dut.bl4a.create_rfcomm_channel(
              address=self.ref.address,
              secure=variant == _Variant.SECURE,
              channel_or_uuid=channel,
          ),
      )

    await self._transmission_test(ref_dut_dlc, dut_ref_dlc)


if __name__ == "__main__":
  test_runner.main()
