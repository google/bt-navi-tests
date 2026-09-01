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

import asyncio
import contextlib
import enum
import itertools
import uuid

from bumble import core
from bumble import data_types
from bumble import device
from bumble import gatt
from bumble import hci
from bumble import keys
from mobly import test_runner

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import pyee_extensions
from navi.utils import retry

# pylint: disable=cell-var-from-loop

_DEFAULT_TIMEOUT_SECONDS = 15.0
_MIN_ADVERTISING_INTERVAL_MS = 20
_DISCOVERY_TIMEOUT_SECONDS = 12.0
_TAK_KEY = bytes([0x12] * 16)
_TAK_SERVICE_UUID = "01234567-89ab-cdef-0123-456789abcdef"

_OwnAddressType = hci.OwnAddressType
_AdvertisingData = core.AdvertisingData


class _AdvertisingVariant(enum.Enum):
  LEGACY_NO_ADV_DATA = enum.auto()
  LEGACY_CCCDK_SERVICE_UUID_AND_DATA = enum.auto()
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
        self.fail(f"Invalid address type {ref_address_type}.")

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
          address_type=android_constants.AddressTypeStatus(
              ref_address_type.value
          ),
      )
      await dut_cb.wait_for_event(
          event=bl4a_api.AclConnected(
              address=ref_address, transport=android_constants.Transport.LE
          ),
      )
      # [DUT] Disconnect GATT.
      await gatt_client.disconnect()
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected(
              address=ref_address,
              transport=android_constants.Transport.LE,
          ),
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

    ref_dut_acl = await self.connect_le_from_ref(
        dut_address_type=android_constants.AddressTypeStatus.PUBLIC,
        ref_address_type=_OwnAddressType.PUBLIC,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
    )

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      # [REF] Disconnect.
      await ref_dut_acl.disconnect()
      # [DUT] Wait for LE-ACL disconnected.
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected(
              address=self.ref.address,
              transport=android_constants.Transport.LE,
          ),
      )

  @navi_test_base.parameterized(
      _AdvertisingVariant.LEGACY_NO_ADV_DATA,
      _AdvertisingVariant.LEGACY_CCCDK_SERVICE_UUID_AND_DATA,
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
    scan_filter = bl4a_api.ScanFilter(
        device=self.ref.address,
        address_type=android_constants.AddressTypeStatus.PUBLIC,
    )
    match ref_advertising_variant:
      case _AdvertisingVariant.LEGACY_NO_ADV_DATA:
        advertising_data = b""
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
      case _AdvertisingVariant.LEGACY_CCCDK_SERVICE_UUID_AND_DATA:
        advertising_data = bytes(
            core.AdvertisingData([
                data_types.CompleteListOf16BitServiceUUIDs([core.UUID("FFF5")]),
                data_types.ServiceData128BitUUID(
                    core.UUID("5810bbc0-b499-11e9-a2a3-2a2ae2dbcce4"),
                    bytes.fromhex("01") + bytes.fromhex("0002"),
                ),
            ])
        )
        advertising_properties = device.AdvertisingEventProperties(
            is_connectable=True,
            is_scannable=True,
            is_legacy=True,
        )
        scan_filter = bl4a_api.ScanFilter(
            service_uuids="0000fff5-0000-1000-8000-00805f9b34fb"
        )
      case _:
        self.fail(f"Invalid advertising variant {ref_advertising_variant}.")

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
        scan_filter=scan_filter,
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

      @watcher.on(self.ref.device, self.ref.device.EVENT_ADVERTISEMENT)
      def _(adv: device.Advertisement) -> None:
        if (
            service_uuids := adv.data.get(
                _AdvertisingData.Type.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS
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
      dut_address = hci.Address(f"{self.dut.address}/P")

      @watcher.on(self.ref.device, self.ref.device.EVENT_ADVERTISEMENT)
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

    self.logger.info("[DUT] Start advertising with service UUID.")
    advertise = await self.dut.bl4a.start_extended_advertising_set(
        bl4a_api.AdvertisingSetParameters(
            secondary_phy=phy,
            own_address_type=own_address_type,
        ),
        bl4a_api.AdvertisingData(service_uuids=[service_uuid]),
        duration=0,
    )

    # [REF] Scan for DUT.
    scan_results = asyncio.Queue[device.Advertisement]()

    def on_advertising_report(adv: device.Advertisement) -> None:
      if (
          service_uuids := adv.data.get(
              _AdvertisingData.Type.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS
          )
      ) and service_uuid in service_uuids:
        scan_results.put_nowait(adv)

    with pyee_extensions.EventWatcher() as watcher:
      watcher.on(self.ref.device, "advertisement", on_advertising_report)

      self.logger.info("[REF] Start scanning for DUT.")
      await self.ref.device.start_scanning()

      self.logger.info("[REF] Wait for advertising report from DUT.")
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        advertisement = await scan_results.get()
      advertise.stop()
      self.assertEqual(advertisement.secondary_phy, phy)

      match own_address_type:
        case android_constants.AddressTypeStatus.PUBLIC:
          self.assertEqual(
              advertisement.address, hci.Address(f"{self.dut.address}/P")
          )
        case android_constants.AddressTypeStatus.RANDOM:
          self.assertTrue(advertisement.address.is_random)
          self.assertTrue(advertisement.address.is_resolvable)
        case android_constants.AddressTypeStatus.RANDOM_NON_RESOLVABLE:
          self.assertTrue(advertisement.address.is_random)
          self.assertFalse(advertisement.address.is_resolvable)
        case _:
          self.fail(f"Invalid address type {own_address_type}.")

  async def test_periodic_advertising(self) -> None:
    """Tests periodic advertising.

    Test steps:
      1. Start advertising on DUT.
      2. Start scanning on REF.
      3. Wait for matched scan result.
      4. Create PA sync on REF.
      5. Wait for PA sync establishment.
      6. Wait for periodic advertisement from REF.
      7. Check that the periodic advertisement data contains the service UUID
      from the periodic advertising data.
    """
    if not self.dut.bt.isLePeriodicAdvertisingSupported():
      self.skipTest("DUT does not support periodic advertising.")

    # Generate a random UUID for testing.
    service_uuid = str(uuid.uuid4())
    service_uuid_2 = str(uuid.uuid4())

    self.logger.info("[DUT] Start advertising with service UUID.")
    advertising_set = await self.dut.bl4a.start_extended_advertising_set(
        bl4a_api.AdvertisingSetParameters(),
        bl4a_api.AdvertisingData(service_uuids=[service_uuid]),
        periodic_advertising_parameters=bl4a_api.PeriodicAdvertisingParameters(
            interval=100,
            include_tx_power_level=True,
        ),
        periodic_advertising_data=bl4a_api.AdvertisingData(
            service_uuids=[service_uuid_2]
        ),
        duration=0,
    )
    self.test_case_context.enter_context(advertising_set)

    # [REF] Scan for DUT.
    advertisements = asyncio.Queue[device.Advertisement]()

    @self.ref.device.on(self.ref.device.EVENT_ADVERTISEMENT)
    def _(adv: device.Advertisement) -> None:
      if (
          service_uuids := adv.data.get(
              _AdvertisingData.Type.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS
          )
      ) and service_uuid in service_uuids:
        advertisements.put_nowait(adv)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info("[REF] Start scanning")
      await self.ref.device.start_scanning()
      self.logger.info("[REF] Wait for advertising report from DUT.")
      advertisement = await advertisements.get()

    # Periodic Synchronization may fail, so retry the process.
    @retry.retry_on_exception()
    async def sync_pa() -> device.PeriodicAdvertisingSync:
      self.logger.info("[REF] Creating periodic advertising sync.")
      pa_sync = await self.ref.device.create_periodic_advertising_sync(
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
        self.logger.info("[REF] Waiting for PA sync establishment.")
        try:
          await pa_sync_result
        finally:
          if pa_sync.state == pa_sync.State.PENDING:
            self.logger.info("[REF] Cancel PA sync.")
            await pa_sync.terminate()
      return pa_sync

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      pa_sync = await sync_pa()
      periodic_advertisements = asyncio.Queue[device.PeriodicAdvertisement]()
      pa_sync.on(
          pa_sync.EVENT_PERIODIC_ADVERTISEMENT,
          periodic_advertisements.put_nowait,
      )
      self.logger.info("[REF] Wait for periodic advertisement.")
      periodic_advertisement = await periodic_advertisements.get()
      if not periodic_advertisement.data:
        self.fail("Periodic advertisement data is empty.")
      # Check that the periodic advertisement data contains the service UUID
      # from the periodic advertising data.
      self.assertEqual(
          periodic_advertisement.data.get(
              _AdvertisingData.Type.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS
          ),
          [service_uuid_2],
      )

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
          inquiry_scan_enabled=False, page_scan_enabled=False
      )
      self.dut.bt.startInquiry()

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
                      "Super Bumble".encode(),
                  ),
              ])
          ),
      )

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

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      self.logger.info("[DUT] Pair with REF.")
      await self.le_connect_and_pair(identity_address_type)

      if ref_dut_acl := self.ref.device.find_connection_by_bd_addr(
          hci.Address(self.dut.address, hci.AddressType.PUBLIC_DEVICE),
          core.BT_LE_TRANSPORT,
      ):
        self.logger.info("[REF] Disconnect.")
        with contextlib.suppress(hci.HCI_Error, hci.HCI_StatusError):
          await ref_dut_acl.disconnect()
      await dut_cb.wait_for_event(bl4a_api.AclDisconnected)

    self.logger.info("[REF] Start advertising.")
    await self.ref.device.start_advertising(own_address_type=ref_address_type)

    self.logger.info("[DUT] Start scanning for REF.")
    dut_scanner = self.dut.bl4a.start_scanning(
        scan_filter=bl4a_api.ScanFilter(
            device=identity_address,
            address_type=identity_address_type,
        ),
    )
    await dut_scanner.wait_for_event(bl4a_api.ScanResult)
    self.logger.info("[DUT] Found REF, start connecting GATT.")
    await self.dut.bl4a.connect_gatt_client(
        address=identity_address,
        address_type=identity_address_type,
        transport=android_constants.Transport.LE,
    )

  async def test_scan_with_identify_address_and_irk(self) -> None:
    """Tests that DUT can scan with identify address and IRK.

    Test steps:
      1. Generate a static address.
      2. Start advertising on REF with RPA.
      3. Start scanning on DUT with static address and REF's IRK.
      4. Check that DUT can receive scan result from REF.
    """
    target_address = hci.Address.generate_static_address().to_string()
    self.logger.info("[REF] Start advertising.")
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      await self.ref.device.start_advertising(
          advertising_type=device.AdvertisingType.UNDIRECTED_CONNECTABLE_SCANNABLE,
          own_address_type=hci.OwnAddressType.RESOLVABLE_OR_RANDOM,
      )
    self.logger.info("[DUT] Start scanning for REF with IRK.")
    with self.dut.bl4a.start_scanning(
        scan_settings=bl4a_api.ScanSettings(
            scan_mode=android_constants.BleScanMode.LOW_LATENCY,
            callback_type=android_constants.BleScanCallbackType.ALL_MATCHES,
            match_mode=android_constants.BleScanMatchMode.STICKY,
        ),
        scan_filter=bl4a_api.ScanFilter(
            device=target_address,
            address_type=android_constants.AddressTypeStatus.RANDOM,
            irk=self.ref.device.irk,
        ),
    ) as scan_cb:
      # [DUT] Wait for advertising report(scan result) from REF.
      event = await scan_cb.wait_for_event(bl4a_api.ScanResult)
      self.assertEqual(event.address, target_address)

  async def test_le_connection_priority(self) -> None:
    """Tests LE connection priority.

    Test steps:
      1. Pair then Disconnect with REF.
      2. Start advertising on REF.
      3. Start scanning on DUT.
      4. Wait for matched scan result.
      5. Connect to REF.
      6. Request connection priority on DUT.
      7. Check that the connection parameters is updated on DUT.
    """
    self.logger.info("[REF] Start advertising")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM,
        advertising_type=device.AdvertisingType.UNDIRECTED_CONNECTABLE_SCANNABLE,
    )
    self.logger.info("[DUT] Connect GATT client to REF")
    gatt_client = await self.dut.bl4a.connect_gatt_client(
        address=self.ref.random_address,
        transport=android_constants.Transport.LE,
        address_type=android_constants.AddressTypeStatus.RANDOM,
    )
    self.test_case_context.push(gatt_client)
    self.logger.info("[DUT] GATT client connected")

    ref_connection = next(iter(self.ref.device.connections.values()))
    connection_parameters = [
        (android_constants.ConnectionPriority.DCK, 30.0, 30.0),
        (android_constants.ConnectionPriority.HIGH, 11.25, 15.0),
        (android_constants.ConnectionPriority.BALANCED, 30.0, 50.0),
        (android_constants.ConnectionPriority.LOW_POWER, 100.0, 150.0),
    ]
    if (
        connection_parameters[0][1]
        < ref_connection.parameters.connection_interval
        < connection_parameters[0][2]
    ):
      # If connection parameters is already in the expected range, reverse the
      # order to make sure the connection parameters is always updated.
      connection_parameters = connection_parameters[::-1]

    condition = asyncio.Condition()

    @ref_connection.on(ref_connection.EVENT_CONNECTION_PARAMETERS_UPDATE)
    async def _() -> None:
      async with condition:
        condition.notify_all()

    for priority, min_interval, max_interval in connection_parameters:
      self.logger.info("[DUT] Request connection priority.")
      await gatt_client.request_connection_priority(priority)
      self.logger.info(
          "[REF] Wait for connection interval update to [%s, %s].",
          min_interval,
          max_interval,
      )
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS), condition:
        await condition.wait_for(
            lambda: min_interval
            <= ref_connection.parameters.connection_interval
            <= max_interval,
        )
      self.logger.info(
          "[REF] Connection interval is updated to %s",
          ref_connection.parameters.connection_interval,
      )

  async def test_request_subrate(self) -> None:
    """Tests requesting LE subrate.

    Test steps:
      1. Enable subrating on REF.
      2. Start advertising on REF.
      3. Connect GATT client to REF.
      4. Request subrate mode on DUT.
      5. Check that the subrate mode is updated on REF.
    """
    # TODO: Re-enable this when subrate manager is ready.
    if not self.dut.device.is_emulator:
      self.skipTest("Not stable on real device.")

    # TODO: Check if DUT supports LE subrating.
    if not self.ref.device.supports_le_features(
        hci.LeFeatureMask.CONNECTION_SUBRATING
    ):
      self.skipTest("REF does not support LE subrating.")

    self.logger.info("[REF] Start advertising")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM,
        advertising_type=device.AdvertisingType.UNDIRECTED_CONNECTABLE_SCANNABLE,
    )
    self.logger.info("[DUT] Connect GATT client to REF")
    gatt_client = await self.dut.bl4a.connect_gatt_client(
        address=self.ref.random_address,
        transport=android_constants.Transport.LE,
        address_type=android_constants.AddressTypeStatus.RANDOM,
    )
    self.test_case_context.push(gatt_client)
    self.logger.info("[DUT] GATT client connected")

    ref_subrate_changed = asyncio.get_running_loop().create_future()
    ref_connection = next(iter(self.ref.device.connections.values()))

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      dut_features = await ref_connection.get_remote_le_features()

    if not (
        dut_features & hci.LeFeatureMask.CONNECTION_SUBRATING
        and dut_features & hci.LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
    ):
      self.skipTest("DUT does not support LE subrating.")

    ref_connection.once(
        ref_connection.EVENT_CONNECTION_PARAMETERS_UPDATE,
        lambda: ref_subrate_changed.set_result(None),
    )
    self.logger.info("[DUT] Request subrate mode.")
    await gatt_client.request_subrate_mode(android_constants.LeSubrateMode.HIGH)

    self.logger.info("[REF] Wait for subrate mode change.")
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      await ref_subrate_changed

  @navi_test_base.parameterized(
      (android_constants.ConnectionPriority.HIGH),
      (android_constants.ConnectionPriority.BALANCED),
      (android_constants.ConnectionPriority.LOW_POWER),
  )
  async def test_subrate_mode_changed_by_subrate_mode_request(
      self,
      mode_to_set: android_constants.ConnectionPriority,
  ) -> None:
    """Test subrate mode changed by subrate mode request.

    Test steps:
      1. Start advertising on REF.
      2. Connect GATT(and LE-ACL) to REF from DUT.
      3. Set connection priority to the given mode.
      4. Set subrate mode to low and verify the subrate mode is changed to low.
      5. Set subrate mode to balanced and verify the subrate mode is changed to
      balanced.
      6. Set subrate mode to high and verify the subrate mode is changed to
      high.
      7. Set subrate mode to off and verify the subrate mode is changed to off.

    Args:
      mode_to_set: The connection priority to set.
    """

    service_uuid = str(uuid.uuid4())
    characteristic_uuid = str(uuid.uuid4())

    self.ref.device.add_service(
        gatt.Service(
            uuid=service_uuid,
            characteristics=[
                gatt.Characteristic(
                    uuid=characteristic_uuid,
                    properties=gatt.Characteristic.Properties.WRITE,
                    permissions=gatt.Characteristic.Permissions.WRITEABLE,
                    value=gatt.CharacteristicValue(),
                )
            ],
        )
    )

    if self.dut.bt.getSdkVersion() < 37:
      self.skipTest("DUT does not support this feature in this SDK version.")

    if not self.ref.device.supports_le_features(
        hci.LeFeatureMask.CONNECTION_SUBRATING
    ):
      self.skipTest("REF does not support LE subrating.")

    self.logger.info("[REF] Start advertising.")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM
    )
    await self.ref.device.send_command(
        hci.HCI_LE_Set_Host_Feature_Command(
            bit_number=hci.LeFeature.CONNECTION_SUBRATING_HOST_SUPPORT,
            bit_value=1,
        ),
        check_result=True,
    )
    self.logger.info("[DUT] Connect to REF.")
    gatt_client = await self.dut.bl4a.connect_gatt_client(
        str(self.ref.random_address),
        android_constants.Transport.LE,
        android_constants.AddressTypeStatus.RANDOM,
    )

    ref_connection = next(iter(self.ref.device.connections.values()))
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      dut_features = await ref_connection.get_remote_le_features()
    if not (
        dut_features & hci.LeFeatureMask.CONNECTION_SUBRATING
        and dut_features & hci.LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
    ):
      self.skipTest("DUT does not support LE subrating.")

    self.logger.info(
        "[DUT] Request connection priority in the beginning: %s", mode_to_set
    )

    await gatt_client.request_connection_priority(mode_to_set)

    for mode in [
        android_constants.LeSubrateMode.LOW,
        android_constants.LeSubrateMode.BALANCED,
        android_constants.LeSubrateMode.HIGH,
    ]:
      self.logger.info("[DUT] Request subrate mode to %s.", mode.name)
      subrate_mode = await gatt_client.request_subrate_mode(mode)
      self.assertEqual(
          subrate_mode,
          mode,
          f"Subrate mode is not changed to {mode.name}.",
      )
      self.logger.info("[DUT] Rollback subrate mode to off.")
      subrate_mode = await gatt_client.request_subrate_mode(
          android_constants.LeSubrateMode.OFF
      )
      self.assertEqual(
          subrate_mode,
          android_constants.LeSubrateMode.OFF,
          "Subrate mode is not rolled back.",
      )

    gatt_client.close()

  async def test_subrate_mode_changed_with_multiple_clients(self) -> None:
    """Test subrate mode changed with multiple clients.

    Test steps:
      1. Start advertising on REF.
      2. Client1: Connect GATT(and LE-ACL) to REF from DUT.
      3. Client1: Set connection priority to balanced.
      4. Client1: Set subrate mode to balanced and verify the subrate mode is
      changed to balanced.
      5. Client2: Connect GATT(and LE-ACL) to REF from DUT.
      6. Client2: Set subrate mode to high and verify the subrate mode is
      changed to high.
      7. Client3: Connect GATT(and LE-ACL) to REF from DUT.
      8. Client3: Set subrate mode to low and verify the subrate mode is still
      high.
      9. Client2: Disconnect from REF.
      10. Client3: Set subrate mode to low and verify the subrate mode is
      changed to balanced.
    """

    service_uuid = str(uuid.uuid4())
    characteristic_uuid = str(uuid.uuid4())

    write_future = asyncio.get_running_loop().create_future()

    def on_write(connection: device.Connection, value: bytes) -> None:
      del connection  # Unused.
      write_future.set_result(value)

    self.ref.device.add_service(
        gatt.Service(
            uuid=service_uuid,
            characteristics=[
                gatt.Characteristic(
                    uuid=characteristic_uuid,
                    properties=gatt.Characteristic.Properties.WRITE,
                    permissions=gatt.Characteristic.Permissions.WRITEABLE,
                    value=gatt.CharacteristicValue(write=on_write),
                )
            ],
        )
    )

    if self.dut.bt.getSdkVersion() < 37:
      self.skipTest("DUT does not support this feature in this SDK version.")

    if not self.ref.device.supports_le_features(
        hci.LeFeatureMask.CONNECTION_SUBRATING
    ):
      self.skipTest("REF does not support LE subrating.")

    self.logger.info("[REF] Start advertising.")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM
    )
    await self.ref.device.send_command(
        hci.HCI_LE_Set_Host_Feature_Command(
            bit_number=hci.LeFeature.CONNECTION_SUBRATING_HOST_SUPPORT,
            bit_value=1,
        ),
        check_result=True,
    )

    gatt_clients = []
    for i in range(1, 4):
      self.logger.info("[DUT] Connect to REF with client%s.", i)
      gatt_client = await self.dut.bl4a.connect_gatt_client(
          str(self.ref.random_address),
          android_constants.Transport.LE,
          android_constants.AddressTypeStatus.RANDOM,
      )
      gatt_clients.append(gatt_client)

    self.assertLen(gatt_clients, 3)

    self.logger.info(
        "[DUT] Request connection priority to balanced in the beginning."
    )

    ref_connection = next(iter(self.ref.device.connections.values()))
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      dut_features = await ref_connection.get_remote_le_features()
    if not (
        dut_features & hci.LeFeatureMask.CONNECTION_SUBRATING
        and dut_features & hci.LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
    ):
      self.skipTest("DUT does not support LE subrating.")

    await gatt_clients[0].request_connection_priority(
        android_constants.ConnectionPriority.BALANCED
    )

    self.logger.info("[DUT] Client1: Request subrate mode to balanced.")
    subrate_mode = await gatt_clients[0].request_subrate_mode(
        android_constants.LeSubrateMode.BALANCED
    )

    self.assertEqual(
        subrate_mode,
        android_constants.LeSubrateMode.BALANCED,
        "Subrate mode is not changed to balanced.",
    )

    self.logger.info("[DUT] Client2: Request subrate mode to high.")
    subrate_mode2 = await gatt_clients[1].request_subrate_mode(
        android_constants.LeSubrateMode.HIGH
    )

    self.assertEqual(
        subrate_mode2,
        android_constants.LeSubrateMode.HIGH,
        "Subrate mode is not changed to high.",
    )

    self.logger.info("[DUT] Client3: Request subrate mode to low.")
    subrate_mode = await gatt_clients[2].request_subrate_mode(
        android_constants.LeSubrateMode.LOW
    )

    self.logger.info(
        "[DUT] Verify subrate mode is still high because there are multiple"
        " subrate mode requests [BALANCED, HIGH, LOW] in the connection,"
        " system will choose the highest priority one."
    )

    self.assertEqual(
        subrate_mode,
        android_constants.LeSubrateMode.HIGH,
        "Subrate mode is not changed to high.",
    )

    gatt_clients[1].close()

    self.logger.info(
        "[DUT] Verify subrate mode is changed to balanced because there are"
        " multiple subrate mode requests [BALANCED, LOW] in the connection"
        " and client2 is disconnected."
        " system will choose the highest priority one."
    )

    subrate_mode = await gatt_clients[2].request_subrate_mode(
        android_constants.LeSubrateMode.LOW
    )

    self.assertEqual(
        subrate_mode,
        android_constants.LeSubrateMode.BALANCED,
        "Subrate mode is not changed to balanced.",
    )

    gatt_clients[0].close()
    gatt_clients[2].close()
    gatt_clients.clear()

  @navi_test_base.parameterized(
      (android_constants.LeSubrateMode.LOW),
      (android_constants.LeSubrateMode.BALANCED),
      (android_constants.LeSubrateMode.HIGH),
  )
  async def test_subrate_mode_changed_when_connection_priority_changed(
      self,
      subrate_mode_to_set: android_constants.LeSubrateMode,
  ) -> None:
    """Test that the subrate mode persists when connection priority changes.

    Test steps:
      1. Start advertising on REF.
      2. Connect GATT(and LE-ACL) to REF from DUT.
      3. Set connection priority to high.
      4. Set subrate mode to the given mode and verify the subrate mode is
      changed to the given mode.
      5. Set connection priority to balanced and verify received the subrate
      mode is still the given mode.
      6. Set connection priority to low power and verify received the subrate
      mode is still the given mode.

    Args:
      subrate_mode_to_set: The subrate mode to set and verify.
    """

    service_uuid = str(uuid.uuid4())
    characteristic_uuid = str(uuid.uuid4())

    self.ref.device.add_service(
        gatt.Service(
            uuid=service_uuid,
            characteristics=[
                gatt.Characteristic(
                    uuid=characteristic_uuid,
                    properties=gatt.Characteristic.Properties.WRITE,
                    permissions=gatt.Characteristic.Permissions.WRITEABLE,
                    value=gatt.CharacteristicValue(),
                )
            ],
        )
    )

    if self.dut.bt.getSdkVersion() < 37:
      self.skipTest("DUT does not support this feature in this SDK version.")

    if not self.ref.device.supports_le_features(
        hci.LeFeatureMask.CONNECTION_SUBRATING
    ):
      self.skipTest("REF does not support LE subrating.")

    self.logger.info("[REF] Start advertising.")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM
    )
    await self.ref.device.send_command(
        hci.HCI_LE_Set_Host_Feature_Command(
            bit_number=hci.LeFeature.CONNECTION_SUBRATING_HOST_SUPPORT,
            bit_value=1,
        ),
        check_result=True,
    )
    self.logger.info("[DUT] Connect to REF.")
    gatt_client = await self.dut.bl4a.connect_gatt_client(
        str(self.ref.random_address),
        android_constants.Transport.LE,
        android_constants.AddressTypeStatus.RANDOM,
    )

    self.logger.info(
        "[DUT] Request connection priority to high in the beginning."
    )

    await gatt_client.request_connection_priority(
        android_constants.ConnectionPriority.HIGH
    )

    ref_connection = next(iter(self.ref.device.connections.values()))
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      dut_features = await ref_connection.get_remote_le_features()
    if not (
        dut_features & hci.LeFeatureMask.CONNECTION_SUBRATING
        and dut_features & hci.LeFeatureMask.CONNECTION_SUBRATING_HOST_SUPPORT
    ):
      self.skipTest("DUT does not support LE subrating.")

    self.logger.info("[DUT] Request subrate mode to low.")
    subrate_mode = await gatt_client.request_subrate_mode(subrate_mode_to_set)

    self.assertEqual(
        subrate_mode,
        subrate_mode_to_set,
        f"Subrate mode is not changed to {subrate_mode_to_set}",
    )

    self.logger.info(
        "[DUT] Request connection priority to balanced in the beginning."
    )
    await gatt_client.request_connection_priority(
        android_constants.ConnectionPriority.BALANCED
    )

    await gatt_client.wait_for_event(
        event=bl4a_api.GattSubrateChanged,
        predicate=lambda e: e.subrate_mode == subrate_mode_to_set,
    )

    self.logger.info(
        "[DUT] Request connection priority to Low power in the beginning."
    )
    await gatt_client.request_connection_priority(
        android_constants.ConnectionPriority.LOW_POWER
    )

    await gatt_client.wait_for_event(
        event=bl4a_api.GattSubrateChanged,
        predicate=lambda e: e.subrate_mode == subrate_mode_to_set,
    )
    gatt_client.close()

  async def test_scan_on_found_and_on_lost(self) -> None:
    """Test scanning with callback type FIRST_MATCH/MATCH_LOST.

    Test steps:
      1. Set REF to start advertising.
      2. Start scanning on DUT with callback type FIRST_MATCH+MATCH_LOST.
      3. Check if DUT received scan result from REF(FIRST_MATCH).
      4. Set REF to stop advertising.
      5. Check if DUT received scan result from REF(MATCH_LOST).
    """
    if self.dut.device.is_emulator:
      self.skipTest("Rootcanal doesn't support APCF yet.")

    ref_service_uuid = str(uuid.uuid4())

    self.logger.info("[REF] Start advertising")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM,
        advertising_type=device.AdvertisingType.UNDIRECTED_CONNECTABLE_SCANNABLE,
        advertising_interval_min=_MIN_ADVERTISING_INTERVAL_MS,
        advertising_interval_max=_MIN_ADVERTISING_INTERVAL_MS,
        advertising_data=bytes(
            _AdvertisingData([
                data_types.Flags(
                    core.AdvertisingData.Flags.LE_GENERAL_DISCOVERABLE_MODE
                ),
                data_types.CompleteListOf128BitServiceUUIDs(
                    [core.UUID(ref_service_uuid)]
                ),
            ])
        ),
    )

    self.logger.info(
        "[DUT] Start scanning with callback FIRST_MATCH+MATCH_LOST"
    )
    with self.dut.bl4a.start_scanning(
        scan_settings=bl4a_api.ScanSettings(
            scan_mode=android_constants.BleScanMode.LOW_LATENCY,
            callback_type=(
                android_constants.BleScanCallbackType.FIRST_MATCH
                | android_constants.BleScanCallbackType.MATCH_LOST
            ),
            legacy=False,
        ),
        scan_filter=bl4a_api.ScanFilter(service_uuids=ref_service_uuid),
    ) as scan_cb:
      self.logger.info("[DUT] Wait for scan result (FIRST_MATCH)")
      first_match_event = await scan_cb.wait_for_event(
          bl4a_api.ScanResult,
          timeout=_DEFAULT_TIMEOUT_SECONDS,
      )
      self.assertEqual(
          first_match_event.address,
          self.ref.random_address,
          "FIRST_MATCH scan result is not correct",
      )

      self.logger.info("[REF] stopping advertising")
      await self.ref.device.stop_advertising()

      self.logger.info("[DUT] Wait for scan result (MATCH_LOST)")
      match_lost_event = await scan_cb.wait_for_event(bl4a_api.ScanResult)
      self.assertEqual(
          match_lost_event.address,
          self.ref.random_address,
          "MATCH_LOST scan result is not correct",
      )

  def _require_tak_support(self) -> None:
    """Verifies that DUT supports TAK (requires SDK 37.1 / 26Q4)."""
    # TAK requires SDK 37.1 (26Q4 /  CINNAMON_BUN_1 = 3700001)
    if self.dut.bt.getFullSdkVersion() < 3700001:
      self.skipTest("DUT does not support this feature in this SDK version.")

  async def _establish_le_acl_connection(
      self, is_ref_central: bool
  ) -> device.Connection:
    """Establishes an LE ACL connection between DUT and REF.

    Args:
      is_ref_central: If True, REF is Central and DUT is Peripheral. If False,
        DUT is Central and REF is Peripheral.

    Returns:
      The Bumble Connection instance on REF.
    """
    if is_ref_central:
      return await self.connect_le_from_ref(
          dut_address_type=android_constants.AddressTypeStatus.PUBLIC,
          ref_address_type=hci.OwnAddressType.RANDOM,
          timeout=_DEFAULT_TIMEOUT_SECONDS,
      )
    else:
      with self.dut.bl4a.register_callback(
          bl4a_api.Module.ADAPTER
      ) as adapter_cb:
        self.logger.info("[REF] Start advertising.")
        await self.ref.device.start_advertising(
            own_address_type=hci.OwnAddressType.RANDOM
        )

        self.logger.info("[DUT] Connect to REF as Central.")
        gatt_client = await self.dut.bl4a.connect_gatt_client(
            address=self.ref.random_address,
            address_type=android_constants.AddressTypeStatus.RANDOM,
            transport=android_constants.Transport.LE,
        )
        self.test_case_context.push(gatt_client)

        await adapter_cb.wait_for_event(
            event=bl4a_api.AclConnected(
                address=self.ref.random_address,
                transport=android_constants.Transport.LE,
            )
        )

        le_connections = [
            c
            for c in self.ref.device.connections.values()
            if c.transport == core.PhysicalTransport.LE
        ]
        if not le_connections:
          self.fail("Failed to find ACL connection between DUT and REF.")
        ref_connection = le_connections[-1]

        await self.ref.device.stop_advertising()
        return ref_connection

  async def _start_tak_session_and_wait(
      self,
      key: bytes,
      service_uuid: str,
      is_ref_central: bool,
  ) -> bl4a_api.TakSession:
    """Starts a TAK session on DUT and waits for the initial state transition.

    Args:
      key: The 16-byte TAK key.
      service_uuid: The TAK service UUID string.
      is_ref_central: If True, DUT is Peripheral (expects TAK_STATE_WAITING). If
        False, DUT is Central (expects TAK_STATE_ENCRYPTING).

    Returns:
      The active bl4a_api.TakSession instance.
    """
    role_name = "Peripheral" if is_ref_central else "Central"
    self.logger.info("Calling startTakSession on DUT (%s)", role_name)
    tak_session = self.test_case_context.enter_context(
        self.dut.bl4a.start_tak_session(
            self.ref.random_address, key, service_uuid
        )
    )
    expected_state = (
        android_constants.TakState.WAITING
        if is_ref_central
        else android_constants.TakState.ENCRYPTING
    )
    await tak_session.wait_for_event(
        event=bl4a_api.TakStateChanged(
            device=self.ref.random_address,
            state=expected_state,
            uuid=service_uuid,
            status=android_constants.BluetoothStatusCode.SUCCESS,
        )
    )
    return tak_session

  @navi_test_base.named_parameterized(
      ("outgoing", False),
      ("incoming", True),
  )
  async def test_tak_setup(self, is_ref_central: bool) -> None:
    """Verify TAK encrypted session setup.

    Args:
      is_ref_central: If True, REF is Central (incoming to DUT). If False, DUT
        is Central (outgoing from DUT).
    """
    self._require_tak_support()

    ref_connection = await self._establish_le_acl_connection(is_ref_central)

    # Unconditionally inject TAK as LTK to keystore so we can use
    # ref_connection.encrypt()
    pairing_keys = keys.PairingKeys()
    pairing_keys.ltk = keys.PairingKeys.Key(
        value=_TAK_KEY,
        rand=b"\x00" * 8,
        ediv=0,
    )

    if self.ref.device.keystore is None:
      self.ref.device.keystore = keys.MemoryKeyStore()

    await self.ref.device.update_keys(
        str(ref_connection.peer_address),
        pairing_keys,
    )

    tak_session = await self._start_tak_session_and_wait(
        _TAK_KEY, _TAK_SERVICE_UUID, is_ref_central
    )
    if is_ref_central:
      # Let Bumble handle the LL command and encryption events via its
      # keystore
      async with self.assert_not_timeout(
          _DEFAULT_TIMEOUT_SECONDS, msg="Wait for REF encryption_change"
      ):
        await ref_connection.encrypt()

    await tak_session.wait_for_event(
        event=bl4a_api.TakStateChanged(
            device=self.ref.random_address,
            state=android_constants.TakState.ENCRYPTED,
            uuid=_TAK_SERVICE_UUID,
            status=android_constants.BluetoothStatusCode.SUCCESS,
        )
    )
    self.logger.info("DUT successfully reached ENCRYPTED state.")

  async def test_tak_timeout_incoming_no_central_request(self) -> None:
    """Verify that if the local Peripheral device receives no encryption request from Central, the session times out."""
    self._require_tak_support()

    await self._establish_le_acl_connection(is_ref_central=True)

    tak_session = await self._start_tak_session_and_wait(
        _TAK_KEY, _TAK_SERVICE_UUID, is_ref_central=True
    )
    # Remote device does NOT initiate encryption.
    # Local Peripheral waits for timeout (2-3 seconds) and should receive
    # ERROR_TAK_ENCRYPTION_FAILED.
    await tak_session.wait_for_event(
        event=bl4a_api.TakStateChanged(
            device=self.ref.random_address,
            state=android_constants.TakState.NONE,
            uuid=_TAK_SERVICE_UUID,
            status=android_constants.BluetoothStatusCode.ERROR_TAK_ENCRYPTION_FAILED,
        )
    )
    self.logger.info(
        "DUT successfully timed out and received ERROR_TAK_ENCRYPTION_FAILED."
    )

  @navi_test_base.named_parameterized(
      ("outgoing", False),
      ("incoming", True),
  )
  async def test_tak_key_mismatch(self, is_ref_central: bool) -> None:
    """Verify that a mismatch between local and remote TAK keys results in encryption failure.

    Args:
      is_ref_central: If True, REF is Central (incoming to DUT). If False, DUT
        is Central (outgoing from DUT).
    """
    self._require_tak_support()

    if is_ref_central and self.dut.device.is_emulator:
      self.skipTest(
          "Rootcanal doesn't support LE peripheral key mismatch verification"
          " yet."
      )

    ref_connection = await self._establish_le_acl_connection(is_ref_central)

    # Mismatched TAK keys: DUT uses Key A (0x11), REF uses Key B (0x22)
    dut_tak_key = bytes([0x11] * 16)
    ref_tak_key = bytes([0x22] * 16)

    # Configure REF keystore with ref_tak_key
    pairing_keys = keys.PairingKeys()
    pairing_keys.ltk = keys.PairingKeys.Key(
        value=ref_tak_key,
        rand=b"\x00" * 8,
        ediv=0,
    )

    if self.ref.device.keystore is None:
      self.ref.device.keystore = keys.MemoryKeyStore()

    await self.ref.device.update_keys(
        str(ref_connection.peer_address),
        pairing_keys,
    )

    tak_session = await self._start_tak_session_and_wait(
        dut_tak_key, _TAK_SERVICE_UUID, is_ref_central
    )
    if is_ref_central:
      # REF (Central) initiates encryption with mismatched key
      self.logger.info("[REF] Initiating encryption with mismatched key")
      with contextlib.suppress(core.ProtocolError, hci.HCI_Error):
        await ref_connection.encrypt()

    # DUT callback receives ERROR_TAK_ENCRYPTION_FAILED
    await tak_session.wait_for_event(
        event=bl4a_api.TakStateChanged(
            device=self.ref.random_address,
            state=android_constants.TakState.NONE,
            uuid=_TAK_SERVICE_UUID,
            status=android_constants.BluetoothStatusCode.ERROR_TAK_ENCRYPTION_FAILED,
        )
    )
    self.logger.info(
        "DUT successfully received ERROR_TAK_ENCRYPTION_FAILED for key"
        " mismatch."
    )


if __name__ == "__main__":
  test_runner.main()
