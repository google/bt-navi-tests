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

"""Tests for LE Broadcast Isochronous Channels."""

import asyncio

from bumble import core
from bumble import device
from bumble import hci
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.tests import navi_test_base
from navi.tests.firmware import test_base
from navi.utils import retry

_DEFAULT_TIMEOUT_SECONDS = 10.0
_BROADCAST_CODE = b'\xdb\x03\x80d\xa3\xb4\xd5\xc5-x\xe9\x8dkC\x02q'


class LeBroadcastTest(test_base.DualDeviceTestBase):

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if not self.dut.device.supports_le_features(
        hci.LeFeatureMask.ISOCHRONOUS_BROADCASTER
    ) or not self.ref.device.supports_le_features(
        hci.LeFeatureMask.ISOCHRONOUS_BROADCASTER
    ):
      raise signals.TestAbortClass(
          'LE Broadcast Isochronous Channels is not supported.'
      )

  async def _create_big(
      self,
      source_device: device.Device,
      broadcast_code: bytes | None = None,
  ) -> device.Big:
    """Creates a LE Broadcast Isochronous Group on the source device.

    Args:
      source_device: The source device to create the BIG on.
      broadcast_code: The broadcast code to use.

    Returns:
      The created LE Broadcast Isochronous Group.
    """
    self.logger.info('[Source] Creating advertising set.')
    advertising_set = await source_device.create_advertising_set(
        advertising_parameters=device.AdvertisingParameters(
            advertising_event_properties=device.AdvertisingEventProperties(
                is_connectable=False
            ),
            own_address_type=hci.OwnAddressType.RANDOM,
            primary_advertising_interval_min=100,
            primary_advertising_interval_max=200,
        ),
        periodic_advertising_parameters=device.PeriodicAdvertisingParameters(
            periodic_advertising_interval_min=80,
            periodic_advertising_interval_max=160,
        ),
        auto_restart=True,
        auto_start=True,
    )
    self.logger.info('[Source] Starting periodic advertising.')
    await advertising_set.start_periodic()
    self.logger.info('[Source] Creating big.')
    big = await source_device.create_big(
        advertising_set,
        parameters=device.BigParameters(
            num_bis=2,
            sdu_interval=10000,
            max_sdu=100,
            max_transport_latency=65,
            rtn=4,
            broadcast_code=broadcast_code,
        ),
    )
    return big

  @retry.retry_on_exception()
  async def _create_pa_sync(
      self, sink_device: device.Device, advertising_set: device.AdvertisingSet
  ) -> device.PeriodicAdvertisingSync:
    """Creates a LE Periodic Advertising Sync on the sink device.

    Args:
      sink_device: The sink device to create the PA Sync on.
      advertising_set: The advertising set to sync.

    Returns:
      The created LE Periodic Advertising Sync.
    """

    advertisements = asyncio.Queue[device.Advertisement]()
    sink_device.on(device.Device.EVENT_ADVERTISEMENT, advertisements.put_nowait)
    self.logger.info('[Sink] Starting scanning.')
    await sink_device.start_scanning()

    while advertisement := await advertisements.get():
      if advertisement.address == advertising_set.random_address:
        self.logger.info('[Sink] Found advertisement.')
        break

    self.logger.info('[Sink] Creating periodic advertising sync.')
    pa_sync = await sink_device.create_periodic_advertising_sync(
        advertiser_address=advertisement.address, sid=advertisement.sid
    )
    if pa_sync.state != pa_sync.State.ESTABLISHED:
      pa_sync_result = asyncio.get_running_loop().create_future()
      pa_sync.once(
          pa_sync.EVENT_ESTABLISHMENT, lambda: pa_sync_result.set_result(None)
      )
      pa_sync.once(
          pa_sync.EVENT_ESTABLISHMENT_ERROR,
          lambda: pa_sync_result.set_exception(hci.HCI_Error(pa_sync.status)),
      )
      self.logger.info('[Sink] Waiting for PA sync establishment.')
      try:
        await pa_sync_result
      finally:
        if pa_sync.state == pa_sync.State.PENDING:
          self.logger.info('[Sink] Cancel PA sync.')
          await pa_sync.terminate()
        self.logger.info('[Sink] Stopping scanning.')
        await sink_device.stop_scanning()
    return pa_sync

  async def _create_big_sync(
      self,
      sink_device: device.Device,
      big: device.Big,
      broadcast_code: bytes | None = None,
  ) -> device.BigSync:
    """Creates a LE Broadcast Isochronous Group Sync on the sink device.

    Args:
      sink_device: The sink device to create the BIG Sync on.
      big: The BIG to sync.
      broadcast_code: The broadcast code to use.

    Returns:
      The created LE Broadcast Isochronous Group Sync.
    """
    pa_sync = await self._create_pa_sync(sink_device, big.advertising_set)
    big_info_advertisements = asyncio.Queue[device.BigInfoAdvertisement]()
    pa_sync.on(
        pa_sync.EVENT_BIGINFO_ADVERTISEMENT,
        big_info_advertisements.put_nowait,
    )
    self.logger.info('[Sink] Wait for big info advertisement.')
    await big_info_advertisements.get()

    self.logger.info('[Sink] Creating big sync.')
    big_sync = await sink_device.create_big_sync(
        pa_sync,
        device.BigSyncParameters(
            big_sync_timeout=0x4000, bis=[1, 2], broadcast_code=broadcast_code
        ),
    )
    await sink_device.stop_scanning()
    return big_sync

  async def _big_transfer(
      self,
      source_to_sink: device.Connection,
      sink_to_source: device.Connection,
      adv_set: device.AdvertisingSet,
  ) -> device.BigInfoAdvertisement:
    transfers = asyncio.Queue[device.PeriodicAdvertisingSync]()

    @sink_to_source.device.on(
        sink_to_source.device.EVENT_PERIODIC_ADVERTISING_SYNC_TRANSFER
    )
    def _(
        transfer: device.PeriodicAdvertisingSync, connection: device.Connection
    ):
      del connection  # Unused.
      transfers.put_nowait(transfer)

    self.logger.info('[Source] Transferring periodic info.')
    await adv_set.transfer_periodic_info(source_to_sink)
    self.logger.info('[Sink] Waiting for transfer.')
    pa_sync = await transfers.get()
    big_info_advertisements = asyncio.Queue[device.BigInfoAdvertisement]()
    pa_sync.on(
        pa_sync.EVENT_BIGINFO_ADVERTISEMENT,
        big_info_advertisements.put_nowait,
    )
    self.logger.info('[Sink] Waiting for big info advertisement.')
    big_info_advertisement = await big_info_advertisements.get()
    return big_info_advertisement

  @navi_test_base.named_parameterized(
      unencrypted=dict(broadcast_code=None),
      encrypted=dict(broadcast_code=_BROADCAST_CODE),
  )
  async def test_create_big(self, broadcast_code: bytes | None) -> None:
    """Test creating Broadcast Isochronous Group on DUT.

    Test steps:
      1. Create a LE Big on DUT.
      2. Create a LE Big Sync on REF.

    Args:
      broadcast_code: The broadcast code to use.
    """
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      big = await self._create_big(
          self.dut.device, broadcast_code=broadcast_code
      )
      await self._create_big_sync(
          self.ref.device, big, broadcast_code=broadcast_code
      )

  @navi_test_base.named_parameterized(
      unencrypted=dict(broadcast_code=None),
      encrypted=dict(broadcast_code=_BROADCAST_CODE),
  )
  async def test_create_big_sync(self, broadcast_code: bytes | None) -> None:
    """Test creating Big Sync on DUT.

    Test steps:
      1. Create a LE Big on REF.
      2. Create a LE Big Sync on DUT.

    Args:
      broadcast_code: The broadcast code to use.
    """
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      big = await self._create_big(
          self.ref.device, broadcast_code=broadcast_code
      )
      await self._create_big_sync(
          self.dut.device, big, broadcast_code=broadcast_code
      )

  async def test_terminate_big(self) -> None:
    """Test terminating Big on DUT.

    Test steps:
      1. Create a LE Big on REF.
      2. Create a LE Big Sync on DUT.
      3. Terminate the LE Big on DUT.
    """
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      big = await self._create_big(self.dut.device, broadcast_code=None)
      self.logger.info('[DUT] Terminating big.')
      await big.terminate()

  async def test_big_sync_terminate(self) -> None:
    """Test terminating Big Sync on DUT.

    Test steps:
      1. Create a LE Big on REF.
      2. Create a LE Big Sync on DUT.
      3. Terminate the LE Big on DUT.
    """
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      big = await self._create_big(self.ref.device)
      big_sync = await self._create_big_sync(self.dut.device, big)
      self.logger.info('[DUT] Terminating big sync.')
      await big_sync.terminate()

  async def test_big_sync_lost(self) -> None:
    """Test Big Sync lost due to source termination.

    Test steps:
      1. Create a LE Big on REF.
      2. Create a LE Big Sync on DUT.
      3. Terminate the LE Big on REF.
      4. Verify that the LE Big Sync is terminated on DUT.
    """
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      big = await self._create_big(self.ref.device)
      big_sync = await self._create_big_sync(self.dut.device, big)
      big_sync_terminations = asyncio.Queue[int]()
      big_sync.on(big_sync.Event.TERMINATION, big_sync_terminations.put_nowait)
      self.logger.info('[REF] Terminating big.')
      await big.terminate()
      self.logger.info('[DUT] Waiting for big sync termination.')
      await big_sync_terminations.get()

  @navi_test_base.named_parameterized(
      outgoing=dict(is_outgoing=True),
      incoming=dict(is_outgoing=False),
  )
  async def test_transfer(self, is_outgoing: bool) -> None:
    """Test transferring Periodic Advertising Sync.

    Test steps:
      1. Create a LE Big.
      2. Create a LE Big Sync.
      3. Transfer the LE Big.
      4. Verify that the LE Big Sync is transferred.

    Args:
      is_outgoing: True if the transfer is outgoing, False if it is incoming.
    """
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      sink_device = self.dut.device if is_outgoing else self.ref.device
      source_device = self.ref.device if is_outgoing else self.dut.device
      await sink_device.send_command(
          hci.HCI_LE_Set_Default_Periodic_Advertising_Sync_Transfer_Parameters_Command(
              mode=0x02,  # PA Report enabled, duplicate non-filtered.
              skip=0x00,
              sync_timeout=0x4000,
              cte_type=0x00,  # No CTE type limitation,
          ),
          check_result=True,
      )
      big = await self._create_big(source_device)
      source_to_sink, sink_to_source = await self.create_connection(
          central=source_device,
          peripheral=sink_device,
          link_type=core.BT_LE_TRANSPORT,
      )
      await self._big_transfer(
          source_to_sink, sink_to_source, big.advertising_set
      )


if __name__ == '__main__':
  test_runner.main()
