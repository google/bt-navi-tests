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
import logging
import pathlib

from bumble import gatt
from bumble import gatt_client as gatt_client_module
from bumble import gatt_server
from bumble import hci
from bumble import host
from bumble import l2cap
from bumble import pairing
from bumble import rfcomm
from mobly import test_runner
from mobly import records
from typing_extensions import override

from navi.tests import navi_test_base
from navi.tests.benchmark import performance_tool
from navi.utils import adb_snippets
from navi.utils import android_constants
from navi.utils import bl4a_api

_Callback = bl4a_api.CallbackHandler
_PairingDelegate = pairing.PairingDelegate
_Phy = android_constants.Phy
_CodedPhyOption = android_constants.CodedPhyOption

_RX_THRESHOLD = 6
_L2CAP_HEADER_SIZE = 4
_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_TRANSMISSION_TIMEOUT_SECONDS = 180.0
_GATT_PAYLOAD_SIZE = 495
_EXPECTED_THROUGHPUT_BYTES_PER_SECOND = {
    (_Phy.LE_1M, _CodedPhyOption.NO_PREFERRED): 60e3,
    (_Phy.LE_2M, _CodedPhyOption.NO_PREFERRED): 120e3,
    (_Phy.LE_CODED, _CodedPhyOption.S2): 30e3,
    (_Phy.LE_CODED, _CodedPhyOption.S8): 10e3,
}
_SERVICE_UUID = "eb4d86c3-4274-4724-a17b-387ad0cba6c3"
_CHARACTERISTIC_UUID = "eb4d86c3-4274-4724-a17b-387ad0cba6c4"
_BUMBLE_SPAM_MODULES = (
    l2cap,
    rfcomm,
    host,
    hci,
    gatt,
    gatt_client_module,
    gatt_server,
)


class ThroughputTest(navi_test_base.TwoDevicesTestBase):
  """Tests throughput of different transport.

  Note that the performance could be affected a lot by the HCI throughput and
  latency on Bumble. For example, running this test on a Cloudtop with Pontis
  might lead to lower bandwidth in comparison to running on a local machine.
  """

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    # Disable logging of bumble modules to avoid log spam.
    for module in _BUMBLE_SPAM_MODULES:
      module.logger.setLevel(logging.INFO)

  @override
  async def async_teardown_class(self) -> None:
    await super().async_teardown_class()
    # Re-enable logging of bumble modules.
    for module in _BUMBLE_SPAM_MODULES:
      module.logger.setLevel(logging.DEBUG)

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()

    # Using highest authentication level to allow secure sockets.
    self.ref.device.pairing_config_factory = lambda _: pairing.PairingConfig(
        delegate=_PairingDelegate(
            io_capability=(
                _PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT
            )
        )
    )

  @override
  async def async_teardown_test(self) -> None:
    await super().async_teardown_test()
    adb_snippets.download_btsnoop(
        self.dut.device, self.current_test_info.output_path
    )

    # Dump Bumble snoop logs.
    with open(
        pathlib.Path(self.current_test_info.output_path, "bumble_btsnoop.log"),
        "wb",
    ) as f:
      f.write(self.ref.snoop_buffer.getbuffer())

  @override
  def on_fail(self, record: records.TestResultRecord) -> None:
    # Snippet might be stuck during tests. Reload it to recover.
    self.dut.reload_snippet()

  @navi_test_base.parameterized(
      (_Phy.LE_1M, _CodedPhyOption.NO_PREFERRED),
      (_Phy.LE_2M, _CodedPhyOption.NO_PREFERRED),
      (_Phy.LE_CODED, _CodedPhyOption.S2),
      (_Phy.LE_CODED, _CodedPhyOption.S8),
  )
  @navi_test_base.retry(2)
  async def test_le_l2cap(
      self,
      phy: android_constants.Phy,
      coded_options: android_constants.CodedPhyOption,
  ) -> None:
    """Tests LE L2CAP throughput.

    Args:
      phy: The PHY to be used for the connection.
      coded_options: The coded PHY options to be used for the connection.
    """
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.PUBLIC
    )

    self.logger.info("[DUT] Connect GATT to REF.")
    dut_gatt_client = await self.dut.bl4a.connect_gatt_client(
        address=self.ref.address,
        transport=android_constants.Transport.LE,
        address_type=android_constants.AddressTypeStatus.PUBLIC,
    )
    self.logger.info("[DUT] Set MTU and PHY.")
    # Trigger Data Length Extension.
    await dut_gatt_client.request_mtu(517)
    # Set preferred PHY.
    new_tx_phy, new_rx_phy = await dut_gatt_client.set_preferred_phy(
        tx_phy=phy.to_mask(),
        rx_phy=phy.to_mask(),
        phy_options=coded_options,
    )
    self.assertEqual(new_tx_phy, phy)
    self.assertEqual(new_rx_phy, phy)

    ref_accept_future: asyncio.Future[l2cap.LeCreditBasedChannel] = (
        asyncio.get_running_loop().create_future()
    )
    server = self.ref.device.create_l2cap_server(
        # Same configuration as Android.
        spec=l2cap.LeCreditBasedChannelSpec(
            mtu=65535, mps=251, max_credits=65535
        ),
        handler=ref_accept_future.set_result,
    )
    self.logger.info("[REF] Listen L2CAP on PSM %d", server.psm)

    self.logger.info("[DUT] Connect L2CAP channel to REF.")
    ref_dut_l2cap_channel, dut_ref_l2cap_channel = await asyncio.gather(
        ref_accept_future,
        self.dut.bl4a.create_l2cap_channel(
            address=self.ref.address,
            secure=False,
            psm=server.psm,
            transport=android_constants.Transport.LE,
            address_type=android_constants.AddressTypeStatus.PUBLIC,
        ),
    )
    # Set the MPS to MAX_LEN - HEADER_SIZE(4) to avoid SDU fragmentation.
    assert self.ref.device.host.le_acl_packet_queue
    ref_dut_l2cap_channel.peer_mps = min(
        ref_dut_l2cap_channel.peer_mps,
        self.ref.device.host.le_acl_packet_queue.max_packet_size
        - _L2CAP_HEADER_SIZE,
    )

    # Store received SDUs in queue.
    ref_sdu_rx_queue = asyncio.Queue[bytes]()
    ref_dut_l2cap_channel.sink = ref_sdu_rx_queue.put_nowait
    expected_throughput_bytes_per_second = (
        _EXPECTED_THROUGHPUT_BYTES_PER_SECOND[(phy, coded_options)]
    )
    total_bytes = int(expected_throughput_bytes_per_second * 20.0)

    async def ref_rx_task():
      bytes_received = 0
      while bytes_received < total_bytes:
        bytes_received += len(await ref_sdu_rx_queue.get())

    self.logger.info("Start sending data from DUT to REF")
    with performance_tool.Stopwatch() as tx_stopwatch:
      async with self.assert_not_timeout(_TRANSMISSION_TIMEOUT_SECONDS):
        await asyncio.gather(
            dut_ref_l2cap_channel.write(bytes(total_bytes)),
            ref_rx_task(),
        )

    self.logger.info("Start sending data from REF to DUT")
    with performance_tool.Stopwatch() as rx_stopwatch:
      async with self.assert_not_timeout(_TRANSMISSION_TIMEOUT_SECONDS):
        ref_dut_l2cap_channel.write(bytes(total_bytes))
        await asyncio.gather(dut_ref_l2cap_channel.read(total_bytes))

    tx_throughput = total_bytes / (tx_stopwatch.elapsed_time).total_seconds()
    rx_throughput = total_bytes / (rx_stopwatch.elapsed_time).total_seconds()
    self.logger.info("Tx Throughput: %.2f KB/s", tx_throughput / 1024)
    self.logger.info("Rx Throughput: %.2f KB/s", rx_throughput / 1024)
    self.record_data(
        navi_test_base.RecordData(
            test_name=self.current_test_info.name,
            properties={
                "tx_throughput_bytes_per_second": tx_throughput,
                "rx_throughput_bytes_per_second": rx_throughput,
            },
        )
    )

  @navi_test_base.parameterized(
      (_Phy.LE_1M, _CodedPhyOption.NO_PREFERRED),
      (_Phy.LE_2M, _CodedPhyOption.NO_PREFERRED),
      (_Phy.LE_CODED, _CodedPhyOption.S2),
      (_Phy.LE_CODED, _CodedPhyOption.S8),
  )
  @navi_test_base.retry(2)
  async def test_le_gatt(
      self,
      phy: android_constants.Phy,
      coded_options: android_constants.CodedPhyOption,
  ) -> None:
    """Tests LE GATT throughput.

    Args:
      phy: The PHY to be used for the connection.
      coded_options: The coded PHY options to be used for the connection.
    """
    ref_written_queue = asyncio.Queue[bytes]()
    expected_throughput_bytes_per_second = (
        _EXPECTED_THROUGHPUT_BYTES_PER_SECOND[(phy, coded_options)]
    )
    total_bytes = int(expected_throughput_bytes_per_second * 20.0)

    ref_characteristic = gatt.Characteristic(
        uuid=_CHARACTERISTIC_UUID,
        properties=(
            gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE
            | gatt.Characteristic.Properties.NOTIFY
        ),
        permissions=gatt.Characteristic.Permissions.WRITEABLE,
        value=gatt.CharacteristicValue(
            write=lambda _, value: ref_written_queue.put_nowait(value)
        ),
    )
    self.ref.device.add_service(
        gatt.Service(
            uuid=_SERVICE_UUID,
            characteristics=[ref_characteristic],
        )
    )
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.PUBLIC
    )

    self.logger.info("[DUT] Connect to REF.")
    gatt_client = await self.dut.bl4a.connect_gatt_client(
        address=self.ref.address,
        transport=android_constants.Transport.LE,
        address_type=android_constants.AddressTypeStatus.PUBLIC,
    )
    self.logger.info("[DUT] Set MTU and PHY.")
    # Trigger Data Length Extension.
    await gatt_client.request_mtu(517)
    # Set preferred PHY.
    new_tx_phy, new_rx_phy = await gatt_client.set_preferred_phy(
        tx_phy=phy.to_mask(),
        rx_phy=phy.to_mask(),
        phy_options=coded_options,
    )
    self.assertEqual(new_tx_phy, phy)
    self.assertEqual(new_rx_phy, phy)
    characteristic_handle = bl4a_api.find_characteristic_by_uuid(
        _CHARACTERISTIC_UUID, await gatt_client.discover_services()
    ).handle
    # Subscribe to notifications.
    await gatt_client.subscribe_characteristic_notifications(
        characteristic_handle
    )

    async def ref_rx_task():
      bytes_received = 0
      while bytes_received < total_bytes:
        bytes_received += len(await ref_written_queue.get())

    async def ref_tx_task():
      bytes_sent = 0
      while bytes_sent < total_bytes:
        payload_size = min(total_bytes - bytes_sent, _GATT_PAYLOAD_SIZE)
        await self.ref.device.gatt_server.notify_subscribers(
            ref_characteristic, bytes(payload_size)
        )
        bytes_sent += payload_size

    async def dut_rx_task():
      bytes_received = 0
      while True:
        # Snippet RPC latency is very high, so we can only poll events until
        # all of them have been received.
        bytes_received += sum(
            len(event.value)
            for event in await asyncio.to_thread(
                lambda: gatt_client.get_all_events(
                    bl4a_api.GattCharacteristicChanged
                )
            )
        )
        if bytes_received >= total_bytes:
          break

    self.logger.info("Start sending data from DUT to REF")
    with performance_tool.Stopwatch() as tx_stopwatch:
      async with self.assert_not_timeout(_TRANSMISSION_TIMEOUT_SECONDS):
        await asyncio.gather(
            gatt_client.write_characteristic_long(
                characteristic_handle,
                bytes(total_bytes),
                mtu=_GATT_PAYLOAD_SIZE,
                write_type=android_constants.GattWriteType.NO_RESPONSE,
            ),
            ref_rx_task(),
        )

    self.logger.info("Start sending data from REF to DUT")
    with performance_tool.Stopwatch() as rx_stopwatch:
      async with self.assert_not_timeout(_TRANSMISSION_TIMEOUT_SECONDS):
        await asyncio.gather(
            dut_rx_task(),
            ref_tx_task(),
        )

    tx_throughput = total_bytes / (tx_stopwatch.elapsed_time).total_seconds()
    rx_throughput = total_bytes / (rx_stopwatch.elapsed_time).total_seconds()
    self.logger.info("Tx Throughput: %.2f KB/s", tx_throughput / 1024)
    self.logger.info("Rx Throughput: %.2f KB/s", rx_throughput / 1024)
    self.record_data(
        navi_test_base.RecordData(
            test_name=self.current_test_info.name,
            properties={
                "tx_throughput_bytes_per_second": tx_throughput,
                "rx_throughput_bytes_per_second": rx_throughput,
            },
        )
    )

  @navi_test_base.retry(2)
  async def test_rfcomm(self) -> None:
    """Tests RFCOMM throughput."""

    await self.classic_connect_and_pair()

    ref_accept_future: asyncio.Future[rfcomm.DLC] = (
        asyncio.get_running_loop().create_future()
    )
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
              secure=True,
              channel_or_uuid=channel,
          ),
      )

    # Store received SDUs in queue.
    ref_sdu_rx_queue = asyncio.Queue[bytes]()
    ref_dut_dlc.sink = ref_sdu_rx_queue.put_nowait
    # Set the threshold to 6 to avoid running out of buffer.
    ref_dut_dlc.rx_credits_threshold = _RX_THRESHOLD
    total_bytes = 4 * 1024 * 1024  # 4 MB

    async def ref_rx_task():
      bytes_received = 0
      while bytes_received < total_bytes:
        bytes_received += len(await ref_sdu_rx_queue.get())

    self.logger.info("Start sending data from DUT to REF")
    with performance_tool.Stopwatch() as tx_stopwatch:
      async with self.assert_not_timeout(_TRANSMISSION_TIMEOUT_SECONDS):
        await asyncio.gather(
            dut_ref_dlc.write(bytes(total_bytes)),
            ref_rx_task(),
        )

    self.logger.info("Start sending data from REF to DUT")
    with performance_tool.Stopwatch() as rx_stopwatch:
      async with self.assert_not_timeout(_TRANSMISSION_TIMEOUT_SECONDS):
        ref_dut_dlc.write(bytes(total_bytes))
        await dut_ref_dlc.read(total_bytes)

    tx_throughput = total_bytes / (tx_stopwatch.elapsed_time).total_seconds()
    rx_throughput = total_bytes / (rx_stopwatch.elapsed_time).total_seconds()
    self.logger.info("Tx Throughput: %.2f KB/s", tx_throughput / 1024)
    self.logger.info("Rx Throughput: %.2f KB/s", rx_throughput / 1024)
    self.record_data(
        navi_test_base.RecordData(
            test_name=self.current_test_info.name,
            properties={
                "tx_throughput_bytes_per_second": tx_throughput,
                "rx_throughput_bytes_per_second": rx_throughput,
            },
        )
    )


if __name__ == "__main__":
  test_runner.main()
