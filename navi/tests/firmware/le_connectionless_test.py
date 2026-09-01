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

"""Tests for LE Connectionless functionality."""

import asyncio
import secrets

from bumble import core
from bumble import data_types
from bumble import device as device_lib
from bumble import hci
from mobly import test_runner
from typing_extensions import override

from navi.bumble_ext import apcf
from navi.tests import navi_test_base
from navi.tests.firmware import test_base
from navi.utils import constants
from navi.utils import retry

_DEFAULT_TIMEOUT_SECONDS = 15.0


class LeConnectionlessTest(test_base.DualDeviceTestBase):

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    hci.HCI_Vendor_Event.add_vendor_factory(
        apcf.LeAdvertisementTrackingSubevent.try_from_bytes
    )

  @override
  async def async_teardown_class(self) -> None:
    hci.HCI_Vendor_Event.remove_vendor_factory(
        apcf.LeAdvertisementTrackingSubevent.try_from_bytes
    )
    await super().async_teardown_class()

  @retry.retry_on_exception()
  async def _create_pa_sync(
      self,
      sink_device: device_lib.Device,
      advertising_set: device_lib.AdvertisingSet,
  ) -> device_lib.PeriodicAdvertisingSync:
    """Creates a LE Periodic Advertising Sync on the sink device.

    Args:
      sink_device: The sink device to create the PA Sync on.
      advertising_set: The advertising set to sync.

    Returns:
      The created LE Periodic Advertising Sync.
    """

    advertisements = asyncio.Queue[device_lib.Advertisement]()
    sink_device.on(
        device_lib.Device.EVENT_ADVERTISEMENT, advertisements.put_nowait
    )
    self.logger.info("[Sink] Starting scanning.")
    await sink_device.start_scanning()

    try:
      self.logger.info("[Sink] Waiting for advertisement.")
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        while advertisement := await advertisements.get():
          if advertisement.address == advertising_set.random_address:
            self.logger.info("[Sink] Found advertisement.")
            break

      self.logger.info("[Sink] Creating periodic advertising sync.")
      pa_sync = await sink_device.create_periodic_advertising_sync(
          advertiser_address=advertisement.address, sid=advertisement.sid
      )

      if pa_sync.state == pa_sync.State.ESTABLISHED:
        return pa_sync

      pa_sync_result = asyncio.get_running_loop().create_future()
      pa_sync.once(
          pa_sync.EVENT_ESTABLISHMENT, lambda: pa_sync_result.set_result(None)
      )
      pa_sync.once(
          pa_sync.EVENT_ESTABLISHMENT_ERROR,
          lambda: pa_sync_result.set_exception(hci.HCI_Error(pa_sync.status)),
      )
      self.logger.info("[Sink] Waiting for PA sync establishment.")
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        try:
          await pa_sync_result
        finally:
          if pa_sync.state == pa_sync.State.PENDING:
            self.logger.info("[Sink] Cancel PA sync.")
            await pa_sync.terminate()

      return pa_sync
    finally:
      self.logger.info("[Sink] Stopping scanning.")
      await sink_device.stop_scanning()

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_le_scan_legacy_adv(
      self, direction: constants.Direction
  ) -> None:
    """Tests LE scanning of legacy advertisements and scan response."""
    if direction == constants.Direction.INCOMING:
      advertiser, scanner = self.ref.device, self.dut.device
    else:
      advertiser, scanner = self.dut.device, self.ref.device

    token = secrets.token_hex(4)
    adv_name = f"L-Adv-{token}"
    sr_name = f"L-SR-{token}"

    # Setup Advertiser Data and Scan Response Data
    advertising_data = bytes(
        core.AdvertisingData([data_types.CompleteLocalName(adv_name)])
    )
    scan_response_data = bytes(
        core.AdvertisingData([data_types.ShortenedLocalName(sr_name)])
    )

    # Setup Advertiser
    advertising_set = await advertiser.create_advertising_set(
        advertising_parameters=device_lib.AdvertisingParameters(
            advertising_event_properties=device_lib.AdvertisingEventProperties(
                is_connectable=True,
                is_scannable=True,
                is_legacy=True,
            ),
            own_address_type=hci.OwnAddressType.RANDOM,
        ),
        advertising_data=advertising_data,
        scan_response_data=scan_response_data,
        auto_restart=True,
        auto_start=True,
    )
    self.logger.info(
        "Started legacy advertising on %s", advertiser.random_address
    )

    # Setup Scanner Queue
    advertisements = asyncio.Queue[device_lib.Advertisement]()
    scanner.on(device_lib.Device.EVENT_ADVERTISEMENT, advertisements.put_nowait)

    # Phase 1: Passive Scan to validate primary advertising data payload
    self.logger.info(
        "Starting Passive Scanning to validate legacy advertising data"
    )
    await scanner.start_scanning(active=False)
    try:
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        while True:
          advertisement = await advertisements.get()
          if advertisement.address == advertising_set.random_address:
            self.logger.info(
                "Found legacy advertisement in passive scan: %s", advertisement
            )
            self.assertEqual(
                advertisement.data.get(
                    core.AdvertisingData.COMPLETE_LOCAL_NAME
                ),
                adv_name,
                msg=(
                    "Legacy advertisement local name does not match expected"
                    f" '{adv_name}'"
                ),
            )
            self.assertTrue(
                advertisement.is_legacy,
                msg=(
                    "Expected legacy advertisement packet, but is_legacy is"
                    " False"
                ),
            )
            self.assertTrue(
                advertisement.is_connectable,
                msg=(
                    "Expected legacy advertisement to be connectable, but"
                    " is_connectable is False"
                ),
            )
            self.assertTrue(
                advertisement.is_scannable,
                msg=(
                    "Expected legacy advertisement to be scannable, but"
                    " is_scannable is False"
                ),
            )
            self.assertFalse(
                advertisement.is_scan_response,
                msg=(
                    "Expected legacy primary advertisement, but"
                    " is_scan_response is True"
                ),
            )
            break
    finally:
      # Stop passive scanning
      self.logger.info("Stopping passive scanning")
      await scanner.stop_scanning()

    # Clear the queue for active scanning
    while not advertisements.empty():
      advertisements.get_nowait()

    # Phase 2: Active Scan to validate scan response data payload
    self.logger.info("Starting Active Scanning to validate scan response data")
    await scanner.start_scanning(active=True)
    try:
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        while True:
          advertisement = await advertisements.get()
          if (
              advertisement.address == advertising_set.random_address
              and advertisement.is_scan_response
          ):
            self.logger.info(
                "Found legacy scan response in active scan: %s", advertisement
            )
            self.assertEqual(
                advertisement.data.get(
                    core.AdvertisingData.SHORTENED_LOCAL_NAME
                ),
                sr_name,
                msg=(
                    "Legacy scan response local name does not match expected"
                    f" '{sr_name}'"
                ),
            )
            break
    finally:
      self.logger.info("Stopping active scanning")
      await scanner.stop_scanning()

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_le_scan_extended_adv(
      self, direction: constants.Direction
  ) -> None:
    """Tests LE scanning of extended advertisements and payload validation."""
    if direction == constants.Direction.INCOMING:
      advertiser, scanner = self.ref.device, self.dut.device
    else:
      advertiser, scanner = self.dut.device, self.ref.device

    # Generate dynamic name to avoid collision in multi-device environments
    token = secrets.token_hex(4)
    adv_name = f"E-Adv-{token}"

    # Setup Advertiser Data
    advertising_data = bytes(
        core.AdvertisingData([data_types.CompleteLocalName(adv_name)])
    )

    # Setup Advertiser
    advertising_set = await advertiser.create_advertising_set(
        advertising_parameters=device_lib.AdvertisingParameters(
            advertising_event_properties=device_lib.AdvertisingEventProperties(
                is_connectable=True,
                is_scannable=False,  # Extended connectable cannot be scannable
                is_legacy=False,
            ),
            own_address_type=hci.OwnAddressType.RANDOM,
        ),
        advertising_data=advertising_data,
        auto_restart=True,
        auto_start=True,
    )
    self.logger.info(
        "Started extended advertising on %s", advertiser.random_address
    )

    # Setup Scanner
    advertisements = asyncio.Queue[device_lib.Advertisement]()
    scanner.on(device_lib.Device.EVENT_ADVERTISEMENT, advertisements.put_nowait)

    self.logger.info("Starting scanning")
    await scanner.start_scanning()
    try:
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        while True:
          advertisement = await advertisements.get()
          if advertisement.address == advertising_set.random_address:
            self.logger.info(
                "Found expected extended advertisement: %s", advertisement
            )
            self.assertFalse(
                advertisement.is_legacy,
                msg=(
                    "Expected extended advertisement packet, but is_legacy is"
                    " True"
                ),
            )
            self.assertTrue(
                advertisement.is_connectable,
                msg=(
                    "Expected extended advertisement to be connectable, but"
                    " is_connectable is False"
                ),
            )
            self.assertEqual(
                advertisement.data.get(
                    core.AdvertisingData.COMPLETE_LOCAL_NAME
                ),
                adv_name,
                msg=(
                    "Extended advertisement local name does not match expected"
                    f" '{adv_name}'"
                ),
            )
            break
    finally:
      self.logger.info("Stopping scanning")
      await scanner.stop_scanning()

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_le_scan_multi_adv(
      self, direction: constants.Direction
  ) -> None:
    """Tests LE scanning of multiple advertisements and payload validation."""
    if direction == constants.Direction.INCOMING:
      advertiser, scanner = self.ref.device, self.dut.device
    else:
      advertiser, scanner = self.dut.device, self.ref.device

    token = secrets.token_hex(4)
    num_adv_sets = 3
    advertising_sets = []
    expected_address_by_name = {}

    # Setup Advertiser: Start 3 advertising sets with unique random addresses
    # and data
    for i in range(num_adv_sets):
      random_address = hci.Address.generate_static_address()
      name = f"M-Adv-{token}-{i+1}"
      expected_address_by_name[name] = random_address

      adv_data = bytes(
          core.AdvertisingData([
              data_types.CompleteLocalName(name),
          ])
      )
      adv_set = await advertiser.create_advertising_set(
          random_address=random_address,
          advertising_parameters=device_lib.AdvertisingParameters(
              advertising_event_properties=device_lib.AdvertisingEventProperties(
                  is_connectable=True,
                  is_scannable=False,
                  is_legacy=False,
              ),
              own_address_type=hci.OwnAddressType.RANDOM,
          ),
          advertising_data=adv_data,
          auto_restart=True,
          auto_start=True,
      )
      advertising_sets.append(adv_set)
      self.logger.info(
          "Started advertising set %d on %s", i, adv_set.random_address
      )

    # Setup Scanner
    advertisements = asyncio.Queue[device_lib.Advertisement]()
    scanner.on(device_lib.Device.EVENT_ADVERTISEMENT, advertisements.put_nowait)

    self.logger.info("Starting scanning")
    await scanner.start_scanning()
    try:
      expected_addresses = set(expected_address_by_name.values())
      found_address_by_name: dict[str, hci.Address] = {}

      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
        while len(found_address_by_name) < num_adv_sets:
          advertisement = await advertisements.get()
          if advertisement.address in expected_addresses:
            found_name = advertisement.data.get(
                core.AdvertisingData.Type.COMPLETE_LOCAL_NAME
            )
            self.logger.info(
                "Found advertisement from: %s, name: %s",
                advertisement.address,
                found_name,
            )
            if found_name is not None:
              found_address_by_name[found_name] = advertisement.address
    finally:
      self.logger.info("Stopping scanning")
      await scanner.stop_scanning()

    self.assertCountEqual(
        found_address_by_name.keys(),
        expected_address_by_name.keys(),
        msg="Discovered advertising sets do not match the expected ones",
    )

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_periodic_advertising_and_sync(
      self, direction: constants.Direction
  ) -> None:
    """Tests LE Periodic Advertising and sync payload validation."""
    if direction == constants.Direction.INCOMING:
      advertiser, scanner = self.ref.device, self.dut.device
    else:
      advertiser, scanner = self.dut.device, self.ref.device

    if self.is_emulator:
      self.skipTest("Rootcanal doesn't properly support PA.")

    if not advertiser.supports_le_features(
        hci.LeFeatureMask.LE_PERIODIC_ADVERTISING
    ):
      self.skipTest("PA is not supported by advertiser")

    if not scanner.supports_le_features(
        hci.LeFeatureMask.LE_PERIODIC_ADVERTISING
    ):
      self.skipTest("PA is not supported by scanner")

    token = secrets.token_hex(4)
    periodic_name = f"P-Adv-{token}"

    # Setup Periodic Advertising Data
    periodic_advertising_data = bytes(
        core.AdvertisingData([
            data_types.CompleteLocalName(periodic_name),
        ])
    )

    # Setup Periodic Advertising on Advertiser
    advertising_set = await advertiser.create_advertising_set(
        advertising_parameters=device_lib.AdvertisingParameters(
            advertising_event_properties=device_lib.AdvertisingEventProperties(
                is_connectable=False,
                is_scannable=False,
                is_legacy=False,
            ),
            own_address_type=hci.OwnAddressType.RANDOM,
            primary_advertising_interval_min=100,
            primary_advertising_interval_max=200,
        ),
        periodic_advertising_parameters=device_lib.PeriodicAdvertisingParameters(
            periodic_advertising_interval_min=80,
            periodic_advertising_interval_max=160,
        ),
        periodic_advertising_data=periodic_advertising_data,
        auto_restart=True,
        auto_start=True,
    )

    # Start Periodic Advertising
    self.logger.info("Starting periodic advertising")
    await advertising_set.start_periodic()

    # Setup Scanner
    advertisements = asyncio.Queue[device_lib.Advertisement]()
    scanner.on(device_lib.Device.EVENT_ADVERTISEMENT, advertisements.put_nowait)

    pa_sync = await self._create_pa_sync(scanner, advertising_set)

    # Now listen for periodic advertisements reports
    periodic_reports = asyncio.Queue[device_lib.PeriodicAdvertisement]()
    pa_sync.on(
        pa_sync.EVENT_PERIODIC_ADVERTISEMENT, periodic_reports.put_nowait
    )

    self.logger.info("Waiting for periodic advertisement reports")
    reports = []
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      # Receive at least 3 periodic reports
      for _ in range(3):
        report = await periodic_reports.get()
        self.logger.info("Received periodic report: %s", report)
        reports.append(report)

    self.assertLen(reports, 3)
    for report in reports:
      self.assertIsNotNone(report.data)

    names = [
        (
            report.data.get(core.AdvertisingData.COMPLETE_LOCAL_NAME)
            if report.data is not None
            else None
        )
        for report in reports
    ]
    self.assertEqual(names, [periodic_name] * 3)

  @navi_test_base.named_parameterized(
      service_data_legacy=dict(
          filter_type=apcf.ApcfFeatureSelection.SERVICE_DATA, is_legacy=True
      ),
      service_data_extended=dict(
          filter_type=apcf.ApcfFeatureSelection.SERVICE_DATA, is_legacy=False
      ),
      service_uuid_legacy=dict(
          filter_type=apcf.ApcfFeatureSelection.SERVICE_UUID, is_legacy=True
      ),
      service_uuid_extended=dict(
          filter_type=apcf.ApcfFeatureSelection.SERVICE_UUID, is_legacy=False
      ),
      name_legacy=dict(
          filter_type=apcf.ApcfFeatureSelection.LOCAL_NAME, is_legacy=True
      ),
      name_extended=dict(
          filter_type=apcf.ApcfFeatureSelection.LOCAL_NAME, is_legacy=False
      ),
      address_legacy=dict(
          filter_type=apcf.ApcfFeatureSelection.BROADCAST_ADDRESS,
          is_legacy=True,
      ),
      address_extended=dict(
          filter_type=apcf.ApcfFeatureSelection.BROADCAST_ADDRESS,
          is_legacy=False,
      ),
  )
  async def test_le_apcf_filtering(
      self, filter_type: apcf.ApcfFeatureSelection, is_legacy: bool
  ) -> None:
    """Tests LE APCF filtering."""
    if self.is_emulator:
      self.skipTest("Rootcanal doesn't support APCF filtering.")

    # We use DUT as scanner and REF as advertiser
    scanner = self.dut.device
    advertiser = self.ref.device

    if is_legacy:
      advertising_event_properties = device_lib.AdvertisingEventProperties(
          is_connectable=True,
          is_scannable=True,
          is_legacy=True,
      )
    else:
      advertising_event_properties = device_lib.AdvertisingEventProperties(
          is_connectable=True,
          is_scannable=False,  # Extended connectable cannot be scannable
          is_legacy=False,
      )

    # 1. Check if APCF is supported by scanner
    try:
      self.logger.info("[Scanner] Check if APCF is supported")
      await scanner.send_sync_command(apcf.HciApcfReadExtendedFeaturesCommand())
    except hci.HCI_Error as e:
      if e.error_code == hci.HCI_ErrorCode.UNKNOWN_HCI_COMMAND_ERROR:
        self.skipTest("Scanner does not support APCF")
      raise

    # Generate dynamic names to avoid collision
    token = secrets.token_hex(4)

    filter_command: apcf.HciApcfCommand
    # Define match and mismatch parameters based on filter_type
    if filter_type == apcf.ApcfFeatureSelection.LOCAL_NAME:
      match_name = f"APCF-Match-{token}"
      mismatch_name = f"APCF-Mismatch-{token}"

      adv_data_match = bytes(
          core.AdvertisingData([data_types.CompleteLocalName(match_name)])
      )
      adv_data_mismatch = bytes(
          core.AdvertisingData([data_types.CompleteLocalName(mismatch_name)])
      )

      filter_command = apcf.HciApcfLocalNameCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=1,
          local_name=match_name.encode("utf-8"),
      )

      adv_address_match = None
      adv_address_mismatch = None

    elif filter_type == apcf.ApcfFeatureSelection.BROADCAST_ADDRESS:
      match_address = hci.Address.generate_static_address()
      mismatch_address = hci.Address.generate_static_address()

      adv_name = f"APCF-Addr-{token}"
      adv_data_match = bytes(
          core.AdvertisingData([data_types.CompleteLocalName(adv_name)])
      )
      adv_data_mismatch = adv_data_match

      filter_command = apcf.HciApcfBroadcasterAddressCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=1,
          broadcaster_address=bytes(match_address),
          application_address_type=0x02,  # Ignore address type
      )

      adv_address_match = match_address
      adv_address_mismatch = mismatch_address

    elif filter_type == apcf.ApcfFeatureSelection.SERVICE_UUID:
      match_uuid = core.UUID("180D")  # Heart Rate
      mismatch_uuid = core.UUID("180F")  # Battery Service

      adv_name = f"APCF-UUID-{token}"
      adv_data_match = bytes(
          core.AdvertisingData([
              data_types.CompleteLocalName(adv_name),
              data_types.IncompleteListOf16BitServiceUUIDs([match_uuid]),
          ])
      )
      adv_data_mismatch = bytes(
          core.AdvertisingData([
              data_types.CompleteLocalName(adv_name),
              data_types.IncompleteListOf16BitServiceUUIDs([mismatch_uuid]),
          ])
      )

      uuid_bytes = match_uuid.to_bytes()
      mask_bytes = b"\xFF\xFF"
      filter_command = apcf.HciApcfServiceUuidCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=1,
          uuid_and_mask=uuid_bytes + mask_bytes,
      )

      adv_address_match = None
      adv_address_mismatch = None

    elif filter_type == apcf.ApcfFeatureSelection.SERVICE_DATA:
      sd_uuid = core.UUID("180D")
      match_data = b"\x01\x02"
      mismatch_data = b"\x03\x04"

      adv_name = f"APCF-SD-{token}"
      adv_data_match = bytes(
          core.AdvertisingData([
              data_types.CompleteLocalName(adv_name),
              data_types.ServiceData16BitUUID(sd_uuid, match_data),
          ])
      )
      adv_data_mismatch = bytes(
          core.AdvertisingData([
              data_types.CompleteLocalName(adv_name),
              data_types.ServiceData16BitUUID(sd_uuid, mismatch_data),
          ])
      )

      match_bytes = sd_uuid.to_bytes() + match_data
      mask_bytes = b"\xFF\xFF\xFF\xFF"

      filter_command = apcf.HciApcfServiceDataCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=1,
          service_data_and_mask=match_bytes + mask_bytes,
      )

      adv_address_match = None
      adv_address_mismatch = None
    else:
      raise ValueError(f"Unknown filter_type: {filter_type}")

    # Setup Scanner Queue
    advertisements = asyncio.Queue[device_lib.Advertisement]()
    scanner.on(device_lib.Device.EVENT_ADVERTISEMENT, advertisements.put_nowait)

    # 2. Start advertising with mismatch data
    self.logger.info("Starting advertising with mismatch data")
    advertising_set = await advertiser.create_advertising_set(
        random_address=adv_address_mismatch,
        advertising_parameters=device_lib.AdvertisingParameters(
            advertising_event_properties=advertising_event_properties,
            own_address_type=hci.OwnAddressType.RANDOM,
        ),
        advertising_data=adv_data_mismatch,
        auto_restart=True,
        auto_start=True,
    )

    # 3. Verify scanner can see the advertisement (without filter)
    self.logger.info("Starting scanning (no filter)")
    await scanner.start_scanning()
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      while True:
        advertisement = await advertisements.get()
        expected_address = (
            adv_address_mismatch or advertising_set.random_address
        )
        if advertisement.address == expected_address:
          self.logger.info("Found advertisement: %s", advertisement)
          break

    self.logger.info("Stopping scanning")
    await scanner.stop_scanning()
    await advertising_set.stop()
    await advertising_set.remove()

    # Clear queue
    while not advertisements.empty():
      advertisements.get_nowait()

    # 4. Enable APCF on scanner
    self.logger.info("[Scanner] Enable APCF")
    await scanner.send_sync_command(
        apcf.HciApcfEnableCommand(apcf_enable=1),
    )

    # 5. Set Filtering Parameters
    self.logger.info(
        "[Scanner] Set APCF filtering parameters for %r", filter_type
    )
    await scanner.send_sync_command(
        apcf.HciApcfSetFilteringParametersCommand(
            apcf_action=apcf.ApcfAction.ADD,
            apcf_filter_index=1,
            apcf_feature_selection=filter_type,
            apcf_list_logic_type=filter_type,
            apcf_filter_logic_type=apcf.ApcfFilterLogicType.AND,
            rssi_high_thresh=-127,  # Low threshold to avoid filtering by RSSI
            delivery_mode=0x00,  # immediate
        ),
    )

    # 6. Set Filter Value
    self.logger.info("[Scanner] Set APCF filter value")
    await scanner.send_sync_command(filter_command)

    # 7. Start advertising with mismatch data again
    self.logger.info("Starting advertising with mismatch data again")
    advertising_set = await advertiser.create_advertising_set(
        random_address=adv_address_mismatch,
        advertising_parameters=device_lib.AdvertisingParameters(
            advertising_event_properties=advertising_event_properties,
            own_address_type=hci.OwnAddressType.RANDOM,
        ),
        advertising_data=adv_data_mismatch,
        auto_restart=True,
        auto_start=True,
    )

    # 8. Start scanning, verify scanner does NOT see mismatch data
    self.logger.info("Starting scanning (with filter)")
    await scanner.start_scanning()
    async with self.assert_timeout(_DEFAULT_TIMEOUT_SECONDS):
      while True:
        advertisement = await advertisements.get()
        expected_address = (
            adv_address_mismatch or advertising_set.random_address
        )
        if advertisement.address == expected_address:
          self.fail(
              "Should not receive advertisement from"
              f" {advertisement.address} due to APCF filter"
          )

    # 9. Change advertising data to match
    self.logger.info("Changing advertising to match data")
    await advertising_set.stop()
    await advertising_set.remove()

    # Clear queue just in case
    while not advertisements.empty():
      advertisements.get_nowait()

    advertising_set = await advertiser.create_advertising_set(
        random_address=adv_address_match,
        advertising_parameters=device_lib.AdvertisingParameters(
            advertising_event_properties=advertising_event_properties,
            own_address_type=hci.OwnAddressType.RANDOM,
        ),
        advertising_data=adv_data_match,
        auto_restart=True,
        auto_start=True,
    )

    # 10. Verify scanner sees match data
    self.logger.info("Waiting for match advertisement")
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      while True:
        advertisement = await advertisements.get()
        expected_address = adv_address_match or advertising_set.random_address
        if advertisement.address == expected_address:
          self.logger.info("Found matching advertisement: %s", advertisement)
          break

  @navi_test_base.named_parameterized(
      service_data_legacy=dict(
          filter_type=apcf.ApcfFeatureSelection.SERVICE_DATA, is_legacy=True
      ),
      service_data_extended=dict(
          filter_type=apcf.ApcfFeatureSelection.SERVICE_DATA, is_legacy=False
      ),
      service_uuid_legacy=dict(
          filter_type=apcf.ApcfFeatureSelection.SERVICE_UUID, is_legacy=True
      ),
      service_uuid_extended=dict(
          filter_type=apcf.ApcfFeatureSelection.SERVICE_UUID, is_legacy=False
      ),
      name_legacy=dict(
          filter_type=apcf.ApcfFeatureSelection.LOCAL_NAME, is_legacy=True
      ),
      name_extended=dict(
          filter_type=apcf.ApcfFeatureSelection.LOCAL_NAME, is_legacy=False
      ),
      address_legacy=dict(
          filter_type=apcf.ApcfFeatureSelection.BROADCAST_ADDRESS,
          is_legacy=True,
      ),
      address_extended=dict(
          filter_type=apcf.ApcfFeatureSelection.BROADCAST_ADDRESS,
          is_legacy=False,
      ),
  )
  async def test_le_apcf_add_filter_while_scanning(
      self, filter_type: apcf.ApcfFeatureSelection, is_legacy: bool
  ) -> None:
    """Tests adding APCF filter while scanning is active."""
    if self.is_emulator:
      self.skipTest("Rootcanal doesn't support APCF filtering.")

    # We use DUT as scanner and REF as advertiser
    scanner = self.dut.device
    advertiser = self.ref.device

    if is_legacy:
      advertising_event_properties = device_lib.AdvertisingEventProperties(
          is_connectable=True,
          is_scannable=True,
          is_legacy=True,
      )
    else:
      advertising_event_properties = device_lib.AdvertisingEventProperties(
          is_connectable=True,
          is_scannable=False,  # Extended connectable cannot be scannable
          is_legacy=False,
      )

    # 1. Check if APCF is supported by scanner
    try:
      self.logger.info("[Scanner] Check if APCF is supported")
      await scanner.send_sync_command(apcf.HciApcfReadExtendedFeaturesCommand())
    except hci.HCI_Error as e:
      if e.error_code == hci.HCI_ErrorCode.UNKNOWN_HCI_COMMAND_ERROR:
        self.skipTest("Scanner does not support APCF")
      raise

    # Generate dynamic names to avoid collision
    token = secrets.token_hex(4)

    filter_1_command: apcf.HciApcfCommand
    filter_2_command: apcf.HciApcfCommand
    # Define match and mismatch parameters based on filter_type
    if filter_type == apcf.ApcfFeatureSelection.LOCAL_NAME:
      match_name = f"APCF-Match-{token}"
      mismatch_name = f"APCF-Mismatch-{token}"
      placeholder_name = f"APCF-Placeholder-{token}"

      adv_data_match = bytes(
          core.AdvertisingData([data_types.CompleteLocalName(match_name)])
      )
      adv_data_mismatch = bytes(
          core.AdvertisingData([data_types.CompleteLocalName(mismatch_name)])
      )

      filter_1_command = apcf.HciApcfLocalNameCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=1,
          local_name=placeholder_name.encode("utf-8"),
      )
      filter_2_command = apcf.HciApcfLocalNameCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=2,
          local_name=match_name.encode("utf-8"),
      )

      adv_address_match = None
      adv_address_mismatch = None

    elif filter_type == apcf.ApcfFeatureSelection.BROADCAST_ADDRESS:
      match_address = hci.Address.generate_static_address()
      mismatch_address = hci.Address.generate_static_address()
      placeholder_address = hci.Address.generate_static_address()

      adv_name = f"APCF-Addr-{token}"
      adv_data_match = bytes(
          core.AdvertisingData([data_types.CompleteLocalName(adv_name)])
      )
      adv_data_mismatch = adv_data_match

      filter_1_command = apcf.HciApcfBroadcasterAddressCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=1,
          broadcaster_address=bytes(placeholder_address),
          application_address_type=0x02,  # Ignore address type
      )
      filter_2_command = apcf.HciApcfBroadcasterAddressCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=2,
          broadcaster_address=bytes(match_address),
          application_address_type=0x02,  # Ignore address type
      )

      adv_address_match = match_address
      adv_address_mismatch = mismatch_address

    elif filter_type == apcf.ApcfFeatureSelection.SERVICE_UUID:
      match_uuid = core.UUID("180D")  # Heart Rate
      mismatch_uuid = core.UUID("180F")  # Battery Service
      placeholder_uuid = core.UUID("180A")  # Device Information

      adv_name = f"APCF-UUID-{token}"
      adv_data_match = bytes(
          core.AdvertisingData([
              data_types.CompleteLocalName(adv_name),
              data_types.IncompleteListOf16BitServiceUUIDs([match_uuid]),
          ])
      )
      adv_data_mismatch = bytes(
          core.AdvertisingData([
              data_types.CompleteLocalName(adv_name),
              data_types.IncompleteListOf16BitServiceUUIDs([mismatch_uuid]),
          ])
      )

      uuid_bytes_placeholder = placeholder_uuid.to_bytes()
      uuid_bytes_match = match_uuid.to_bytes()
      mask_bytes = b"\xFF\xFF"
      filter_1_command = apcf.HciApcfServiceUuidCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=1,
          uuid_and_mask=uuid_bytes_placeholder + mask_bytes,
      )
      filter_2_command = apcf.HciApcfServiceUuidCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=2,
          uuid_and_mask=uuid_bytes_match + mask_bytes,
      )

      adv_address_match = None
      adv_address_mismatch = None

    elif filter_type == apcf.ApcfFeatureSelection.SERVICE_DATA:
      sd_uuid = core.UUID("180D")
      match_data = b"\x01\x02"
      mismatch_data = b"\x03\x04"
      placeholder_data = b"\x05\x06"

      adv_name = f"APCF-SD-{token}"
      adv_data_match = bytes(
          core.AdvertisingData([
              data_types.CompleteLocalName(adv_name),
              data_types.ServiceData16BitUUID(sd_uuid, match_data),
          ])
      )
      adv_data_mismatch = bytes(
          core.AdvertisingData([
              data_types.CompleteLocalName(adv_name),
              data_types.ServiceData16BitUUID(sd_uuid, mismatch_data),
          ])
      )

      placeholder_bytes = sd_uuid.to_bytes() + placeholder_data
      match_bytes = sd_uuid.to_bytes() + match_data
      mask_bytes = b"\xFF\xFF\xFF\xFF"

      filter_1_command = apcf.HciApcfServiceDataCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=1,
          service_data_and_mask=placeholder_bytes + mask_bytes,
      )
      filter_2_command = apcf.HciApcfServiceDataCommand(
          apcf_action=apcf.ApcfAction.ADD,
          apcf_filter_index=2,
          service_data_and_mask=match_bytes + mask_bytes,
      )

      adv_address_match = None
      adv_address_mismatch = None
    else:
      raise ValueError(f"Unknown filter_type: {filter_type}")

    # Setup Scanner Queue
    advertisements = asyncio.Queue[device_lib.Advertisement]()
    scanner.on(device_lib.Device.EVENT_ADVERTISEMENT, advertisements.put_nowait)

    # 2. Start advertising with mismatch data
    self.logger.info("Starting advertising with mismatch data")
    advertising_set = await advertiser.create_advertising_set(
        random_address=adv_address_mismatch,
        advertising_parameters=device_lib.AdvertisingParameters(
            advertising_event_properties=advertising_event_properties,
            own_address_type=hci.OwnAddressType.RANDOM,
        ),
        advertising_data=adv_data_mismatch,
        auto_restart=True,
        auto_start=True,
    )

    # 3. Verify scanner can see the advertisement (without filter)
    self.logger.info("Starting scanning (no filter)")
    await scanner.start_scanning()
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      while True:
        advertisement = await advertisements.get()
        expected_address = (
            adv_address_mismatch or advertising_set.random_address
        )
        if advertisement.address == expected_address:
          self.logger.info("Found advertisement: %s", advertisement)
          break

    self.logger.info("Stopping scanning")
    await scanner.stop_scanning()
    await advertising_set.stop()
    await advertising_set.remove()

    # Clear queue
    while not advertisements.empty():
      advertisements.get_nowait()

    # 4. Enable APCF on scanner
    self.logger.info("[Scanner] Enable APCF")
    await scanner.send_sync_command(
        apcf.HciApcfEnableCommand(apcf_enable=1),
    )

    # 5. Set Filtering Parameters
    self.logger.info(
        "[Scanner] Set APCF filtering parameters for filter 1 (%r)",
        filter_type,
    )
    await scanner.send_sync_command(
        apcf.HciApcfSetFilteringParametersCommand(
            apcf_action=apcf.ApcfAction.ADD,
            apcf_filter_index=1,
            apcf_feature_selection=filter_type,
            apcf_list_logic_type=filter_type,
            apcf_filter_logic_type=apcf.ApcfFilterLogicType.AND,
            rssi_high_thresh=-127,  # Low threshold to avoid filtering by RSSI
            delivery_mode=0x00,  # immediate
        ),
    )

    # 6. Set Filter Value
    self.logger.info("[Scanner] Set APCF filter value for filter 1")
    await scanner.send_sync_command(filter_1_command)

    # 7. Start advertising with mismatch data again
    self.logger.info("Starting advertising with mismatch data again")
    advertising_set = await advertiser.create_advertising_set(
        random_address=adv_address_mismatch,
        advertising_parameters=device_lib.AdvertisingParameters(
            advertising_event_properties=advertising_event_properties,
            own_address_type=hci.OwnAddressType.RANDOM,
        ),
        advertising_data=adv_data_mismatch,
        auto_restart=True,
        auto_start=True,
    )

    # 8. Start scanning, verify scanner does NOT see mismatch data
    self.logger.info("Starting scanning (with filter)")
    await scanner.start_scanning()
    async with self.assert_timeout(_DEFAULT_TIMEOUT_SECONDS):
      while True:
        advertisement = await advertisements.get()
        expected_address = (
            adv_address_mismatch or advertising_set.random_address
        )
        if advertisement.address == expected_address:
          self.fail(
              "Should not receive advertisement from"
              f" {advertisement.address} due to APCF filter"
          )

    # 9. Add another filter with correct data to match
    self.logger.info(
        "[Scanner] Set APCF filtering parameters for filter 2 while scanning"
        " (%r)",
        filter_type,
    )
    await scanner.send_sync_command(
        apcf.HciApcfSetFilteringParametersCommand(
            apcf_action=apcf.ApcfAction.ADD,
            apcf_filter_index=2,
            apcf_feature_selection=filter_type,
            apcf_list_logic_type=filter_type,
            apcf_filter_logic_type=apcf.ApcfFilterLogicType.AND,
            rssi_high_thresh=-127,  # Low threshold to avoid filtering by RSSI
            delivery_mode=0x00,  # immediate
        ),
    )

    self.logger.info("[Scanner] Set APCF filter value for filter 2")
    await scanner.send_sync_command(filter_2_command)

    self.logger.info("Changing advertising to match data")
    await advertising_set.stop()
    await advertising_set.remove()

    # Clear queue just in case
    while not advertisements.empty():
      advertisements.get_nowait()

    advertising_set = await advertiser.create_advertising_set(
        random_address=adv_address_match,
        advertising_parameters=device_lib.AdvertisingParameters(
            advertising_event_properties=advertising_event_properties,
            own_address_type=hci.OwnAddressType.RANDOM,
        ),
        advertising_data=adv_data_match,
        auto_restart=True,
        auto_start=True,
    )

    # 10. Verify scanner sees match data
    self.logger.info("Waiting for match advertisement")
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      while True:
        advertisement = await advertisements.get()
        expected_address = adv_address_match or advertising_set.random_address
        if advertisement.address == expected_address:
          self.logger.info("Found matching advertisement: %s", advertisement)
          break

    self.logger.info("Stopping scanning")
    await scanner.stop_scanning()
    await advertising_set.stop()
    await advertising_set.remove()

  @navi_test_base.named_parameterized(
      legacy=dict(is_legacy=True),
      extended=dict(is_legacy=False),
  )
  async def test_le_apcf_onfound_onlost(self, is_legacy: bool) -> None:
    """Tests LE APCF onfound and onlost filtering."""
    if self.is_emulator:
      self.skipTest("Rootcanal doesn't support APCF filtering.")

    # We use DUT as scanner and REF as advertiser
    scanner = self.dut.device
    advertiser = self.ref.device

    # 1. Check if APCF is supported by scanner
    try:
      self.logger.info("[Scanner] Check if APCF is supported")
      await scanner.send_sync_command(apcf.HciApcfReadExtendedFeaturesCommand())
    except hci.HCI_Error as e:
      if e.error_code == hci.HCI_ErrorCode.UNKNOWN_HCI_COMMAND_ERROR:
        self.skipTest("Scanner does not support APCF")
      raise

    # Generate dynamic names to avoid collision
    token = secrets.token_hex(4)
    match_name = f"APCF-Match-{token}"

    # Setup tracking events queue
    tracking_events = asyncio.Queue[apcf.LeAdvertisementTrackingSubevent]()

    def on_tracking_event(event: apcf.LeAdvertisementTrackingSubevent) -> None:
      self.logger.info("Received tracking event: %s", event)
      tracking_events.put_nowait(event)

    setattr(
        scanner.host,
        f"on_{apcf.LeAdvertisementTrackingSubevent.name.lower()}",
        on_tracking_event,
    )

    # 2. Enable APCF on scanner
    self.logger.info("[Scanner] Enable APCF")
    await scanner.send_sync_command(
        apcf.HciApcfEnableCommand(apcf_enable=1),
    )

    # 3. Set Filtering Parameters (ONFOUND mode)
    self.logger.info("[Scanner] Set APCF filtering parameters (ONFOUND)")
    await scanner.send_sync_command(
        apcf.HciApcfSetFilteringParametersCommand(
            apcf_action=apcf.ApcfAction.ADD,
            apcf_filter_index=1,
            apcf_feature_selection=apcf.ApcfFeatureSelection.LOCAL_NAME,
            apcf_list_logic_type=apcf.ApcfFeatureSelection.LOCAL_NAME,
            apcf_filter_logic_type=apcf.ApcfFilterLogicType.AND,
            rssi_high_thresh=-127,
            delivery_mode=0x01,  # ONFOUND
            onfound_timeout=1000,  # 1s
            onfound_timeout_cnt=1,
            rssi_low_thresh=-127,
            onlost_timeout=2000,  # 2s
            num_of_tracking_entries=10,
        ),
    )

    # 4. Set Local Name to match
    self.logger.info("[Scanner] Set APCF local name: %s", match_name)
    await scanner.send_sync_command(
        apcf.HciApcfLocalNameCommand(
            apcf_action=apcf.ApcfAction.ADD,
            apcf_filter_index=1,
            local_name=match_name.encode("utf-8"),
        ),
    )

    # 5. Start scanning on scanner
    self.logger.info("[Scanner] Starting scanning")
    await scanner.start_scanning()

    # 6. Start advertising on advertiser with MATCH name
    self.logger.info(
        "[Advertiser] Starting advertising with name: %s", match_name
    )
    advertising_data_match = bytes(
        core.AdvertisingData([data_types.CompleteLocalName(match_name)])
    )
    if is_legacy:
      advertising_event_properties = device_lib.AdvertisingEventProperties(
          is_connectable=True,
          is_scannable=True,
          is_legacy=True,
      )
    else:
      advertising_event_properties = device_lib.AdvertisingEventProperties(
          is_connectable=True,
          is_scannable=False,  # Extended connectable cannot be scannable
          is_legacy=False,
      )

    advertising_set = await advertiser.create_advertising_set(
        advertising_parameters=device_lib.AdvertisingParameters(
            advertising_event_properties=advertising_event_properties,
            own_address_type=hci.OwnAddressType.RANDOM,
        ),
        advertising_data=advertising_data_match,
        auto_restart=True,
        auto_start=True,
    )

    # 7. Verify ONFOUND (found) event
    self.logger.info("Waiting for ONFOUND tracking event")
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      while True:
        event = await tracking_events.get()
        if event.apcf_filter_index == 1:
          self.assertEqual(event.advertiser_state, 0x00)  # Found
          self.assertEqual(
              event.advertiser_address, advertising_set.random_address
          )
          self.logger.info("Pass: Received ONFOUND event")
          break

    # 8. Stop advertising on advertiser
    self.logger.info("[Advertiser] Stopping advertising")
    await advertising_set.stop()
    await advertising_set.remove()

    # 9. Verify ONLOST (lost) event
    self.logger.info("Waiting for ONLOST tracking event")
    # We wait for lost timeout (2s) + some buffer
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      while True:
        event = await tracking_events.get()
        if event.apcf_filter_index == 1:
          self.assertEqual(event.advertiser_state, 0x01)  # Lost
          self.assertEqual(
              event.advertiser_address, advertising_set.random_address
          )
          self.logger.info("Pass: Received ONLOST event")
          break

    # 10. Cleanup scanner
    self.logger.info("[Scanner] Stopping scanning")
    await scanner.stop_scanning()

  async def test_connect_while_scanning(self) -> None:
    """Verifies connection initiation takes priority over scan reporting."""
    self.logger.info("[DUT] Starting scanning")
    await self.dut.device.start_scanning(active=True)

    self.logger.info("[DUT] Starting connection")
    await self.create_connection(
        central=self.dut.device,
        peripheral=self.ref.device,
        link_type=core.PhysicalTransport.LE,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
    )


if __name__ == "__main__":
  test_runner.main()
