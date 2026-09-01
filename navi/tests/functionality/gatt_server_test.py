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

"""Tests for GATT Server."""

from __future__ import annotations

import asyncio
import secrets
import uuid

from bumble import att
from bumble import core
from bumble import device
from bumble import gatt
from bumble import gatt_client
from bumble import hci
from bumble import l2cap
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import bluetooth_constants
from navi.utils import retry

_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0

_Property = android_constants.GattCharacteristicProperty
_Permission = android_constants.GattCharacteristicPermission
_CCCD_UUID = (
    bluetooth_constants.BluetoothAssignedUuid.CLIENT_CHARACTERISTIC_CONFIGURATION_DESCRIPTOR
)

_TEST_SERVICE_UUID = "9e72cf4a-0100-47c2-835b-efcecf84931b"
_READ_CHAR_UUID = "9e72cf4a-0200-47c2-835b-efcecf84931b"
_WRITE_CHAR_UUID = "9e72cf4a-0300-47c2-835b-efcecf84931b"
_SUBSCRIBE_CHAR_UUID = "9e72cf4a-0400-47c2-835b-efcecf84931b"

_GATT_SERVICE = bl4a_api.GattService(
    uuid=_TEST_SERVICE_UUID,
    characteristics=(
        bl4a_api.GattCharacteristic(
            uuid=_READ_CHAR_UUID,
            properties=_Property.READ,
            permissions=_Permission.READ,
        ),
        bl4a_api.GattCharacteristic(
            uuid=_WRITE_CHAR_UUID,
            properties=_Property.WRITE | _Property.WRITE_NO_RESPONSE,
            permissions=_Permission.WRITE,
        ),
        bl4a_api.GattCharacteristic(
            uuid=_SUBSCRIBE_CHAR_UUID,
            properties=_Property.READ | _Property.NOTIFY | _Property.INDICATE,
            permissions=_Permission.READ,
            descriptors=(
                bl4a_api.GattDescriptor(
                    uuid=_CCCD_UUID,
                    permissions=_Permission.READ | _Permission.WRITE,
                ),
            ),
        ),
    ),
)


class GattServerVentiTest(navi_test_base.TwoDevicesTestBase):
  """Tests for GATT Server role."""

  dut_name: str

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.getprop(android_constants.Property.GATT_ENABLED) != "true":
      raise signals.TestAbortClass("GATT is not enabled on DUT.")

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()

    # Use a unique name to avoid conflicts.
    self.dut_name = f"gatt_server_test_{uuid.uuid4().hex[:8]}"
    self.dut.bt.setName(self.dut_name)
    self.logger.info("dut_name: %s", self.dut.bt.getName())

  async def _setup_gatt_server(
      self, is_private: bool = False
  ) -> bl4a_api.GattServer:
    """Sets up a private GATT server on DUT."""
    dut_gatt_server = self.dut.bl4a.create_gatt_server()
    self.test_case_context.enter_context(dut_gatt_server)
    self.logger.info(
        "[DUT] Start advertising with Non-resolvable private address."
    )
    advertiser = await self.dut.bl4a.start_extended_advertising_set(
        bl4a_api.AdvertisingSetParameters(
            connectable=True,
            own_address_type=(
                android_constants.AddressTypeStatus.RANDOM_NON_RESOLVABLE
                if is_private
                else android_constants.AddressTypeStatus.RANDOM
            ),
        ),
        gatt_server=dut_gatt_server if is_private else None,
        advertising_data=bl4a_api.AdvertisingData(include_device_name=True),
        scan_response=bl4a_api.AdvertisingData(),
        periodic_advertising_parameters=None,
        periodic_advertising_data=None,
        duration=0,
        max_extended_advertising_events=0,
    )
    self.test_case_context.enter_context(advertiser)
    return dut_gatt_server

  @retry.retry_on_exception()
  async def _make_le_connection(self) -> device.Connection:
    """Connects to DUT over LE and returns the connection."""
    ref_dut_acl = await self.ref.device.connect(
        self.dut_name,
        transport=core.BT_LE_TRANSPORT,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
        own_address_type=hci.OwnAddressType.RANDOM,
    )
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await ref_dut_acl.get_remote_le_features()
    return ref_dut_acl

  async def test_private_server_add_service(self) -> None:
    """Tests opening a GATT server on DUT, adding a service discovered by REF.

    Test steps:
      1. Open a GATT server on DUT.
      2. Add a GATT service to the server instance.
      3. Discover services from REF.
      4. Verify added service is discovered.
    """
    dut_gatt_server = await self._setup_gatt_server(is_private=True)

    self.logger.info("[DUT] Add a service.")
    await dut_gatt_server.add_service(
        _GATT_SERVICE
    )

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self._make_le_connection()

    async with device.Peer(ref_dut_acl) as peer:
      self.logger.info("[REF] Check services.")
      services = await peer.discover_services([core.UUID(_TEST_SERVICE_UUID)])
      self.assertLen(services, 1)
      characteristics = await peer.discover_characteristics(
          [core.UUID(_READ_CHAR_UUID)], services[0]
      )
      self.assertLen(characteristics, 1)
      self.assertEqual(
          characteristics[0].properties, gatt.Characteristic.Properties.READ
      )

  async def test_private_server_handle_characteristic_read_request(
      self,
  ) -> None:
    """Tests handling a characteristic read request.

    Test steps:
      1. Open a GATT server on DUT.
      2. Add a GATT service including a readable characteristic to the server
      instance.
      3. Read characteristic from REF.
      4. Handle the read request and send response from DUT.
      5. Check read result from REF.
    """
    dut_gatt_server = await self._setup_gatt_server(is_private=True)

    self.logger.info("[DUT] Add a service.")
    await dut_gatt_server.add_service(
        _GATT_SERVICE
    )

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self._make_le_connection()

    async with device.Peer(ref_dut_acl) as peer:
      services = await peer.discover_services([core.UUID(_TEST_SERVICE_UUID)])
      self.assertLen(services, 1)
      characteristics = await peer.discover_characteristics(
          [core.UUID(_READ_CHAR_UUID)], services[0]
      )
      self.assertLen(characteristics, 1)
      characteristic = characteristics[0]

      self.logger.info("[REF] Read characteristic.")
      read_task = asyncio.create_task(characteristic.read_value())

      read_request = await dut_gatt_server.wait_for_event(
          event=bl4a_api.GattCharacteristicReadRequest,
          predicate=lambda request: (
              request.characteristic_uuid == _READ_CHAR_UUID
          ),
      )
      expected_data = secrets.token_bytes(16)
      dut_gatt_server.send_response(
          address=read_request.address,
          request_id=read_request.request_id,
          status=android_constants.GattStatus.SUCCESS,
          value=expected_data,
      )
      self.assertEqual(await read_task, expected_data)

  @navi_test_base.named_parameterized(
      with_response=True,
      without_response=False,
  )
  async def test_private_server_handle_characteristic_write_request(
      self, with_response: bool
  ) -> None:
    """Tests handling a characteristic write request.

    Test steps:
      1. Open a GATT server on DUT.
      2. Add a GATT service including a writable characteristic to the server
      instance.
      3. Write characteristic from REF.
      4. Handle the write request and send response from DUT.
      5. Check write result from REF.

    Args:
      with_response: Whether to test write with response or without response. If
        True, test write with response; otherwise, test write without response.
    """

    dut_gatt_server = await self._setup_gatt_server(is_private=True)

    self.logger.info("[DUT] Add a service.")
    await dut_gatt_server.add_service(_GATT_SERVICE)

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self._make_le_connection()

    async with device.Peer(ref_dut_acl) as peer:
      services = await peer.discover_services([core.UUID(_TEST_SERVICE_UUID)])
      self.assertLen(services, 1)
      characteristics = await peer.discover_characteristics(
          [core.UUID(_WRITE_CHAR_UUID)], services[0]
      )
      self.assertLen(characteristics, 1)
      characteristic = characteristics[0]

      self.logger.info("[REF] Write characteristic.")
      expected_data = secrets.token_bytes(16)
      write_task = asyncio.create_task(
          characteristic.write_value(expected_data, with_response=with_response)
      )

      write_request = await dut_gatt_server.wait_for_event(
          event=bl4a_api.GattCharacteristicWriteRequest,
          predicate=lambda request: (
              request.characteristic_uuid == _WRITE_CHAR_UUID
          ),
      )
      self.assertEqual(write_request.value, expected_data)
      self.assertEqual(write_request.response_needed, with_response)

      dut_gatt_server.send_response(
          address=write_request.address,
          request_id=write_request.request_id,
          status=android_constants.GattStatus.SUCCESS,
          value=b"",
      )
      await write_task

  async def test_private_server_service_discovery_by_uuid(self) -> None:
    """Tests GATT service discovery by UUID, testing the FindByTypeValue ATT request.

    Test steps:
      1. Open a GATT server on DUT.
      2. Add multiple different GATT services to the server instance.
      3. Discover a specific service by UUID from REF.
      4. Verify only the targeted service is discovered.
    """

    other_service_uuid = "9e72cf4a-0100-47c2-835b-efcecf84931c"
    dut_gatt_server = await self._setup_gatt_server(is_private=True)

    self.logger.info("[DUT] Add the target service.")
    await dut_gatt_server.add_service(
        _GATT_SERVICE
    )

    self.logger.info("[DUT] Add a different service.")
    await dut_gatt_server.add_service(
        bl4a_api.GattService(
            uuid=other_service_uuid,
            characteristics=(
                bl4a_api.GattCharacteristic(
                    uuid=_READ_CHAR_UUID,
                    properties=_Property.READ,
                    permissions=_Permission.READ,
                ),
            )
        ),
    )

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self._make_le_connection()

    async with device.Peer(ref_dut_acl) as peer:
      self.logger.info("[REF] Discover specific service by UUID.")
      # This Bumble API internally sends an ATT Find By Type Value Request
      services = await peer.discover_service(core.UUID(_TEST_SERVICE_UUID))

      # Verify we only found the target service, and not the other service
      self.assertLen(services, 1)
      self.assertEqual(
          services[0].uuid, core.UUID(_TEST_SERVICE_UUID)
      )

  async def test_private_server_handle_characteristic_long_read_request(
      self,
  ) -> None:
    """Tests handling a characteristic read blob request for long data.

    Test steps:
      1. Open a GATT server on DUT.
      2. Add a GATT service including a readable characteristic to the server
      instance.
      3. Read characteristic from REF.
      4. Handle the read request, respond with part of data, wait for next
      request (blob).
      5. Handle the read blob request, send the rest.
      6. Check read result from REF.
    """
    dut_gatt_server = await self._setup_gatt_server(is_private=True)

    self.logger.info("[DUT] Add a service.")
    await dut_gatt_server.add_service(
        _GATT_SERVICE
    )

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self._make_le_connection()

    async with device.Peer(ref_dut_acl) as peer:
      services = await peer.discover_services([core.UUID(_TEST_SERVICE_UUID)])
      self.assertLen(services, 1)
      characteristics = await peer.discover_characteristics(
          [core.UUID(_READ_CHAR_UUID)], services[0]
      )
      self.assertLen(characteristics, 1)
      characteristic = characteristics[0]

      self.logger.info("[REF] Read characteristic.")
      # Normal MTU defaults to 23, so ATT_MTU-1 is 22. We want to be much larger
      # so we exceed the MTU and trigger the read_blob.
      expected_data = secrets.token_bytes(64)
      read_task = asyncio.create_task(characteristic.read_value())

      # For 64 bytes (with MTU=23), Bumble actually needs 3 total read requests!
      # Round 1: 0-21 (22 bytes)
      # Round 2: 22-43 (22 bytes)
      # Round 3: 44-63 (20 bytes)

      self.logger.info("[DUT] Handle read and blob read requests.")
      previous_request_id = -1

      for _ in range(3):
        read_request = await dut_gatt_server.wait_for_event(
            bl4a_api.GattCharacteristicReadRequest
        )
        self.assertGreater(
            read_request.request_id,
            previous_request_id,
            "Request ID should be increasing.",
        )

        dut_gatt_server.send_response(
            address=read_request.address,
            request_id=read_request.request_id,
            status=android_constants.GattStatus.SUCCESS,
            value=expected_data[read_request.offset:],
            offset=read_request.offset,
        )
        previous_request_id = read_request.request_id

      self.logger.info("[REF] Validate full read.")
      self.assertEqual(await read_task, expected_data)

  async def test_private_server_handle_characteristic_multiple_variable_read_request(
      self,
  ) -> None:
    """Tests handling a characteristic multiple variable read request.

    Test steps:
      1. Open a GATT server on DUT.
      2. Add a GATT service including 2 readable characteristics to the server
         instance.
      3. Send multiple variable read request from REF using
         ATT_Read_Multiple_Variable_Request.
      4. Handle the 2 read requests sequentially and send responses from DUT.
      5. Check read result from REF.
    """
    service_uuid = "9e72cf4a-0100-47c2-835b-efcecf84931d"
    read_char_uuid2 = "9e72cf4a-0200-47c2-835b-efcecf84931d"

    dut_gatt_server = await self._setup_gatt_server(is_private=True)

    self.logger.info("[DUT] Add a service.")
    await dut_gatt_server.add_service(
        bl4a_api.GattService(
            uuid=service_uuid,
            characteristics=[
                bl4a_api.GattCharacteristic(
                    uuid=_READ_CHAR_UUID,
                    properties=_Property.READ,
                    permissions=_Permission.READ,
                ),
                bl4a_api.GattCharacteristic(
                    uuid=read_char_uuid2,
                    properties=_Property.READ,
                    permissions=_Permission.READ,
                ),
            ],
        ),
    )

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self._make_le_connection()

    async with device.Peer(ref_dut_acl) as peer:
      services = await peer.discover_services([core.UUID(service_uuid)])
      self.assertLen(services, 1)

      characteristics = await peer.discover_characteristics(
          [core.UUID(_READ_CHAR_UUID), core.UUID(read_char_uuid2)], services[0]
      )
      self.assertLen(characteristics, 2)
      char1 = next(
          c for c in characteristics if c.uuid == core.UUID(_READ_CHAR_UUID)
      )
      char2 = next(
          c for c in characteristics if c.uuid == core.UUID(read_char_uuid2)
      )

      self.logger.info("[REF] Send read multiple variable request.")
      request = att.ATT_Read_Multiple_Variable_Request(
          set_of_handles=[char1.handle, char2.handle]
      )
      read_task = asyncio.create_task(peer.gatt_client.send_request(request))

      self.logger.info("[DUT] Handle first read request.")
      read_request1 = await dut_gatt_server.wait_for_event(
          event=bl4a_api.GattCharacteristicReadRequest,
          predicate=lambda req: req.characteristic_uuid == _READ_CHAR_UUID,
      )
      expected_data1 = secrets.token_bytes(8)
      dut_gatt_server.send_response(
          address=read_request1.address,
          request_id=read_request1.request_id,
          status=android_constants.GattStatus.SUCCESS,
          value=expected_data1,
      )

      self.logger.info("[DUT] Handle second read request.")
      read_request2 = await dut_gatt_server.wait_for_event(
          event=bl4a_api.GattCharacteristicReadRequest,
          predicate=lambda req: req.characteristic_uuid == read_char_uuid2,
      )
      expected_data2 = secrets.token_bytes(8)
      dut_gatt_server.send_response(
          address=read_request2.address,
          request_id=read_request2.request_id,
          status=android_constants.GattStatus.SUCCESS,
          value=expected_data2,
      )

      self.logger.info("[REF] Validate full read.")
      response = await asyncio.wait_for(
          read_task, _DEFAULT_STEP_TIMEOUT_SECONDS
      )

      self.assertLen(response.length_value_tuple_list, 2)
      self.assertEqual(response.length_value_tuple_list[0][1], expected_data1)
      self.assertEqual(response.length_value_tuple_list[1][1], expected_data2)

  async def test_eatt_connection_without_encryption(self) -> None:
    """Tests EATT connection without encryption should fail.

    Test steps:
      1. Start advertising on DUT.
      2. Connect to DUT over LE.
      3. Try to connect to EATT.
      4. Verify that EATT connection fails.
    """

    self.logger.info("[DUT] Start advertising.")
    advertiser = await self.dut.bl4a.start_extended_advertising_set(
        bl4a_api.AdvertisingSetParameters(
            connectable=True,
            own_address_type=(android_constants.AddressTypeStatus.RANDOM),
        ),
        advertising_data=bl4a_api.AdvertisingData(include_device_name=True),
    )
    self.test_case_context.enter_context(advertiser)

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self._make_le_connection()

    self.logger.info("[REF] Try to connect to EATT.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      with self.assertRaises(l2cap.L2capError):
        await gatt_client.Client.connect_eatt(ref_dut_acl)


if __name__ == "__main__":
  test_runner.main()
