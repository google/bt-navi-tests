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
import enum
import itertools
import uuid

from bumble import core
from bumble import device
from bumble import hci
from mobly import test_runner

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import pyee_extensions

_DEFAULT_TIMEOUT_SECONDS = 15.0
_MIN_ADVERTISING_INTERVAL_MS = 20
_DISCOVERY_TIMEOUT_SECONDS = 12.0

_OwnAddressType = hci.OwnAddressType
_AdvertisingData = core.AdvertisingData


class _AdvertisingVariant(enum.Enum):
  LEGACY_NO_ADV_DATA = enum.auto()
  EXTENDED_ADV_DATA_1_BYTES = enum.auto()
  EXTENDED_ADV_DATA_200_BYTES = enum.auto()


class LeHostTest(navi_test_base.TwoDevicesTestBase):

  @navi_test_base.parameterized(
      _OwnAddressType.PUBLIC,
      _OwnAddressType.RANDOM,
  )
  @navi_test_base.retry(max_count=2)
  async def test_outgoing_connect_disconnect(
      self, ref_address_type: hci.OwnAddressType
  ) -> None:
    """Tests outgoing LE connection and disconnection.

    Test steps:
      1. Start advertising on REF.
      2. Connect REF from DUT.
      3. Wait for BLE connected.
      4. Disconnect REF from DUT.

    Args:
      ref_address_type: address type of REF device used in advertisements.
    """
    match ref_address_type:
      case _OwnAddressType.PUBLIC:
        ref_address = str(self.ref.address)
      case _OwnAddressType.RANDOM:
        ref_address = str(self.ref.random_address)
      case _:
        self.fail(f'Invalid address type {ref_address_type}.')

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:

      # [REF] Start advertising.
      await self.ref.device.start_advertising(
          own_address_type=ref_address_type,
          advertising_type=device.AdvertisingType.UNDIRECTED_CONNECTABLE_SCANNABLE,
          advertising_interval_min=_MIN_ADVERTISING_INTERVAL_MS,
          advertising_interval_max=_MIN_ADVERTISING_INTERVAL_MS,
      )

      # [DUT] Connect GATT.
      gatt_client = await self.dut.bl4a.connect_gatt_client(
          address=ref_address,
          transport=android_constants.Transport.LE,
          address_type=ref_address_type,
      )
      await dut_cb.wait_for_event(
          bl4a_api.AclConnected,
          predicate=lambda e: (e.address == ref_address),
      )
      # [DUT] Disconnect GATT.
      await gatt_client.disconnect()
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected,
          predicate=lambda e: (e.address == ref_address),
      )

  @navi_test_base.retry(max_count=2)
  async def test_incoming_connect_disconnect(self) -> None:
    """Tests incoming LE connection and disconnection.

    Test steps:
      1. Start advertising on DUT.
      2. Connect DUT from REF.
      3. Wait for BLE connected.
      4. Disconnect DUT from REF.
    """

    # [DUT] Start advertising with Public address.
    await self.dut.bl4a.start_legacy_advertiser(
        bl4a_api.LegacyAdvertiseSettings(
            own_address_type=_OwnAddressType.PUBLIC
        ),
    )

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      # [REF] Connect GATT.
      ref_dut_acl = await self.ref.device.connect(
          f'{self.dut.address}/P',
          core.BT_LE_TRANSPORT,
          own_address_type=_OwnAddressType.PUBLIC,
      )
      await ref_dut_acl.get_remote_le_features()

      # [DUT] Wait for LE-ACL connected.
      await dut_cb.wait_for_event(
          bl4a_api.AclConnected,
          predicate=lambda e: e.address == self.ref.address,
      )

      # [REF] Disconnect.
      await ref_dut_acl.disconnect()
      # [DUT] Wait for LE-ACL disconnected.
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected,
          predicate=lambda e: e.address == self.ref.address,
      )

  @navi_test_base.parameterized(
      _AdvertisingVariant.LEGACY_NO_ADV_DATA,
      _AdvertisingVariant.EXTENDED_ADV_DATA_1_BYTES,
      _AdvertisingVariant.EXTENDED_ADV_DATA_200_BYTES,
  )
  async def test_scan(
      self, ref_advertising_variant: _AdvertisingVariant
  ) -> None:
    """Tests scanning remote devices.

    Test steps:
      1. Start advertising on REF.
      2. Start scanning on DUT.
      3. Wait for matched scan result.

    Args:
      ref_advertising_variant: advertising variant of REF device.
    """
    match ref_advertising_variant:
      case _AdvertisingVariant.LEGACY_NO_ADV_DATA:
        advertising_data = b''
        advertising_properties = device.AdvertisingEventProperties(
            is_connectable=True,
            is_scannable=True,
            is_legacy=True,
        )
      case _AdvertisingVariant.EXTENDED_ADV_DATA_1_BYTES:
        advertising_data = bytes(1)
        advertising_properties = device.AdvertisingEventProperties(
            is_connectable=True,
        )
      case _AdvertisingVariant.EXTENDED_ADV_DATA_200_BYTES:
        advertising_data = bytes(200)
        advertising_properties = device.AdvertisingEventProperties(
            is_connectable=True,
        )
      case _:
        self.fail(f'Invalid advertising variant {ref_advertising_variant}.')

    # [REF] Start advertising.
    await self.ref.device.create_advertising_set(
        advertising_parameters=device.AdvertisingParameters(
            primary_advertising_interval_min=_MIN_ADVERTISING_INTERVAL_MS,
            primary_advertising_interval_max=_MIN_ADVERTISING_INTERVAL_MS,
            own_address_type=_OwnAddressType.PUBLIC,
            advertising_event_properties=advertising_properties,
        ),
        advertising_data=advertising_data,
    )
    # [DUT] Start scanning.
    with self.dut.bl4a.start_scanning(
        scan_settings=bl4a_api.ScanSettings(
            legacy=False,
        ),
        scan_filter=bl4a_api.ScanFilter(
            device=self.ref.address,
            address_type=android_constants.AddressTypeStatus.PUBLIC,
        ),
    ) as scan_cb:
      # [DUT] Wait for advertising report(scan result) from REF.
      event = await scan_cb.wait_for_event(bl4a_api.ScanResult)
      self.assertEqual(event.address, self.ref.address)

  async def test_advertising_with_service_uuid(self) -> None:
    """Tests advertising using RPA, with Service UUID included in AdvertisingData.

    Test steps:
      1. Start advertising on DUT.
      2. Start scanning on REF.
      3. Wait for matched scan result.
    """
    with pyee_extensions.EventWatcher() as watcher:
      # Generate a random UUID for testing.
      service_uuid = str(uuid.uuid4())

      # [DUT] Start advertising with service UUID and RPA.
      advertise = await self.dut.bl4a.start_legacy_advertiser(
          bl4a_api.LegacyAdvertiseSettings(
              own_address_type=_OwnAddressType.PUBLIC
          ),
          bl4a_api.AdvertisingData(service_uuids=[service_uuid]),
      )

      # [REF] Scan for DUT.
      scan_results = asyncio.Queue[device.Advertisement]()

      @watcher.on(self.ref.device, 'advertisement')
      def on_advertising_report(adv: device.Advertisement) -> None:
        if (
            service_uuids := adv.data.get(
                _AdvertisingData.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS
            )
        ) and service_uuid in service_uuids:
          scan_results.put_nowait(adv)

      await self.ref.device.start_scanning()
      # [REF] Wait for advertising report(scan result) from DUT.
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        await scan_results.get()
      advertise.stop()

  async def test_advertising_with_public_address(self) -> None:
    """Tests advertising using Public Address.

    Test steps:
      1. Start advertising on DUT.
      2. Start scanning on REF.
      3. Wait for matched scan result.
    """
    with pyee_extensions.EventWatcher() as watcher:
      # [DUT] Start advertising with service UUID and Public address.
      advertise = await self.dut.bl4a.start_legacy_advertiser(
          bl4a_api.LegacyAdvertiseSettings(
              own_address_type=_OwnAddressType.PUBLIC
          ),
      )

      # [REF] Scan for DUT.
      scan_results = asyncio.Queue[device.Advertisement]()
      dut_address = hci.Address(f'{self.dut.address}/P')

      @watcher.on(self.ref.device, 'advertisement')
      def on_advertising_report(adv: device.Advertisement) -> None:
        if adv.address == dut_address:
          scan_results.put_nowait(adv)

      await self.ref.device.start_scanning()
      # [REF] Wait for advertising report(scan result) from DUT.
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        await scan_results.get()
      advertise.stop()

  @navi_test_base.parameterized(
      *itertools.product(
          (hci.Phy.LE_1M, hci.Phy.LE_2M, hci.Phy.LE_CODED),
          (
              android_constants.AddressTypeStatus.PUBLIC,
              android_constants.AddressTypeStatus.RANDOM,
              android_constants.AddressTypeStatus.RANDOM_NON_RESOLVABLE,
          ),
      )
  )
  async def test_extended_advertising(
      self, phy: int, own_address_type: android_constants.AddressTypeStatus
  ) -> None:
    """Tests extended advertising, with different primary Phy settings.

    Test steps:
      1. Start advertising on DUT.
      2. Start scanning on REF.
      3. Wait for matched scan result.

    Args:
      phy: PHY option used in extended advertising.
      own_address_type: type of address used in the advertisement.
    """
    # Generate a random UUID for testing.
    service_uuid = str(uuid.uuid4())

    self.logger.info('[DUT] Start advertising with service UUID.')
    advertise = await self.dut.bl4a.start_extended_advertising_set(
        bl4a_api.AdvertisingSetParameters(
            secondary_phy=phy,
            own_address_type=own_address_type,
        ),
        bl4a_api.AdvertisingData(service_uuids=[service_uuid]),
    )

    # [REF] Scan for DUT.
    scan_results = asyncio.Queue[device.Advertisement]()

    def on_advertising_report(adv: device.Advertisement) -> None:
      if (
          service_uuids := adv.data.get(
              _AdvertisingData.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS
          )
      ) and service_uuid in service_uuids:
        scan_results.put_nowait(adv)

    with pyee_extensions.EventWatcher() as watcher:
      watcher.on(self.ref.device, 'advertisement', on_advertising_report)

      self.logger.info('[REF] Start scanning for DUT.')
      await self.ref.device.start_scanning()

      self.logger.info('[REF] Wait for advertising report from DUT.')
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        advertisement = await scan_results.get()
      advertise.stop()
      self.assertEqual(advertisement.secondary_phy, phy)

      match own_address_type:
        case android_constants.AddressTypeStatus.PUBLIC:
          self.assertEqual(
              advertisement.address, hci.Address(f'{self.dut.address}/P')
          )
        case android_constants.AddressTypeStatus.RANDOM:
          self.assertTrue(advertisement.address.is_random)
          self.assertTrue(advertisement.address.is_resolvable)
        case android_constants.AddressTypeStatus.RANDOM_NON_RESOLVABLE:
          self.assertTrue(advertisement.address.is_random)
          self.assertFalse(advertisement.address.is_resolvable)
        case _:
          self.fail(f'Invalid address type {own_address_type}.')

  @navi_test_base.retry(max_count=2)
  async def test_le_discovery(self) -> None:
    """Test discover LE devices.

    Test steps:
      1. Disable Classic scan and start advertising on REF.
      2. Start discovery on REF.
      3. Wait for matched scan result.
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:

      await self.ref.device.set_scan_enable(
          inquiry_scan_enabled=0, page_scan_enabled=0
      )
      # [REF] Start advertising.
      await self.ref.device.start_advertising(
          own_address_type=_OwnAddressType.PUBLIC,
          advertising_type=device.AdvertisingType.UNDIRECTED_CONNECTABLE_SCANNABLE,
          advertising_interval_min=_MIN_ADVERTISING_INTERVAL_MS,
          advertising_interval_max=_MIN_ADVERTISING_INTERVAL_MS,
          advertising_data=bytes(
              _AdvertisingData([
                  (
                      _AdvertisingData.FLAGS,
                      bytes(
                          [_AdvertisingData.LE_GENERAL_DISCOVERABLE_MODE_FLAG]
                      ),
                  ),
                  (
                      _AdvertisingData.COMPLETE_LOCAL_NAME,
                      'Super Bumble'.encode(),
                  ),
              ])
          ),
      )
      self.dut.bt.startInquiry()

      await dut_cb.wait_for_event(
          bl4a_api.DeviceFound,
          lambda e: (e.address == self.ref.address),
          _DISCOVERY_TIMEOUT_SECONDS,
      )

  @navi_test_base.parameterized(
      hci.OwnAddressType.PUBLIC,
      hci.OwnAddressType.RANDOM,
      hci.OwnAddressType.RESOLVABLE_OR_RANDOM,
      hci.OwnAddressType.RESOLVABLE_OR_PUBLIC,
  )
  async def test_scan_and_connect_after_pairing(
      self, ref_address_type: hci.OwnAddressType
  ) -> None:
    """Tests scanning remote devices after pairing(IRK exchanged).

    Test steps:
      1. Pair with REF.
      2. Disconnect from REF.
      3. Start advertising on REF.
      4. Start scanning on DUT.
      5. Wait for matched scan result.

    Args:
      ref_address_type: address type of REF device used in advertisements.
    """
    if ref_address_type in (
        hci.OwnAddressType.RESOLVABLE_OR_RANDOM,
        hci.OwnAddressType.RANDOM,
    ):
      identity_address = self.ref.random_address
      identity_address_type = android_constants.AddressTypeStatus.RANDOM
    else:
      identity_address = self.ref.address
      identity_address_type = android_constants.AddressTypeStatus.PUBLIC

    self.logger.info('[DUT] Pair with REF.')
    await self.le_connect_and_pair(identity_address_type)
    ref_dut_acl = list(self.ref.device.connections.values())[0]

    self.logger.info('[REF] Disconnect.')
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      await ref_dut_acl.disconnect()
      await dut_cb.wait_for_event(bl4a_api.AclDisconnected)

    self.logger.info('[REF] Start advertising.')
    await self.ref.device.start_advertising(own_address_type=ref_address_type)

    self.logger.info('[DUT] Start scanning for REF.')
    dut_scanner = self.dut.bl4a.start_scanning(
        scan_filter=bl4a_api.ScanFilter(
            device=identity_address,
            address_type=identity_address_type,
        ),
    )
    await dut_scanner.wait_for_event(bl4a_api.ScanResult)
    self.logger.info('[DUT] Found REF, start connecting GATT.')
    await self.dut.bl4a.connect_gatt_client(
        address=identity_address,
        address_type=identity_address_type,
        transport=android_constants.Transport.LE,
    )


if __name__ == '__main__':
  test_runner.main()
