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
import secrets

from bumble import gatt
from bumble import gatt_client
from bumble import hci
import bumble.core
import bumble.device
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import bluetooth_constants
from navi.utils import constants

_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0

_Property = android_constants.GattCharacteristicProperty
_Permission = android_constants.GattCharacteristicPermission
_CCCD_UUID = (
    bluetooth_constants.BluetoothAssignedUuid.CLIENT_CHARACTERISTIC_CONFIGURATION_DESCRIPTOR
)

_TEST_SERVICE_UUID = "9e72cf4a-0100-47c2-835b-efcecf84931a"
_READ_CHAR_UUID = "9e72cf4a-0200-47c2-835b-efcecf84931a"
_WRITE_CHAR_UUID = "9e72cf4a-0300-47c2-835b-efcecf84931a"
_SUBSCRIBE_CHAR_UUID = "9e72cf4a-0400-47c2-835b-efcecf84931a"

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


class GattServerTest(navi_test_base.TwoDevicesTestBase):
  """Tests of GATT server implementation on Pixel."""

  dut_gatt_server: bl4a_api.GattServer

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.getprop(android_constants.Property.GATT_ENABLED) != "true":
      raise signals.TestAbortClass("GATT is not enabled on DUT.")

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    self.logger.info("[DUT] Open server.")
    self.dut_gatt_server = self.dut.bl4a.create_gatt_server()

  @override
  async def async_teardown_test(self) -> None:
    await super().async_teardown_test()
    self.dut_gatt_server.close()

  async def test_add_service(self) -> None:
    """Tests opening a GATT server on DUT, adding a service discovered by REF.

    Test steps:
      1. Open a GATT server on DUT.
      2. Add a GATT service to the server instance.
      3. Discover services from REF.
      4. Verify added service is discovered.
    """
    self.logger.info("[DUT] Add a service.")
    await self.dut_gatt_server.add_service(_GATT_SERVICE)

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self.connect_le_from_ref(
        dut_address_type=android_constants.AddressTypeStatus.PUBLIC,
        ref_address_type=hci.OwnAddressType.RANDOM,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    async with bumble.device.Peer(ref_dut_acl) as peer:
      self.logger.info("[REF] Check services.")
      services = peer.get_services_by_uuid(bumble.core.UUID(_TEST_SERVICE_UUID))
      self.assertLen(services, 1)

      with self.subTest("Read Characteristic"):
        read_chars = services[0].get_characteristics_by_uuid(
            bumble.core.UUID(_READ_CHAR_UUID)
        )
        self.assertLen(read_chars, 1)
        self.assertEqual(
            read_chars[0].properties, gatt.Characteristic.Properties.READ
        )

      with self.subTest("Write Characteristic"):
        write_chars = services[0].get_characteristics_by_uuid(
            bumble.core.UUID(_WRITE_CHAR_UUID)
        )
        self.assertLen(write_chars, 1)
        self.assertEqual(
            write_chars[0].properties,
            gatt.Characteristic.Properties.WRITE
            | gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
        )

      with self.subTest("Subscribe Characteristic"):
        sub_chars = services[0].get_characteristics_by_uuid(
            bumble.core.UUID(_SUBSCRIBE_CHAR_UUID)
        )
        self.assertLen(sub_chars, 1)
        self.assertEqual(
            sub_chars[0].properties,
            gatt.Characteristic.Properties.READ
            | gatt.Characteristic.Properties.NOTIFY
            | gatt.Characteristic.Properties.INDICATE,
        )

  async def test_handle_characteristic_read_request(self) -> None:
    """Tests handling a characteristic read request.

    Test steps:
      1. Open a GATT server on DUT.
      2. Add a GATT service including a readable characteristic to the server
      instance.
      3. Read characteristic from REF.
      4. Handle the read request and send response from DUT.
      5. Check read result from REF.
    """
    self.logger.info("[DUT] Add a service.")
    await self.dut_gatt_server.add_service(_GATT_SERVICE)

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self.connect_le_from_ref(
        dut_address_type=android_constants.AddressTypeStatus.PUBLIC,
        ref_address_type=hci.OwnAddressType.RANDOM,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    async with bumble.device.Peer(ref_dut_acl) as peer:
      characteristic = peer.get_characteristics_by_uuid(
          bumble.core.UUID(_READ_CHAR_UUID)
      )[0]

      self.logger.info("[REF] Read characteristic.")
      read_task = asyncio.create_task(characteristic.read_value())

      read_request = await self.dut_gatt_server.wait_for_event(
          event=bl4a_api.GattCharacteristicReadRequest,
          predicate=lambda request: (
              request.characteristic_uuid == _READ_CHAR_UUID
          ),
      )
      expected_data = secrets.token_bytes(16)
      self.dut_gatt_server.send_response(
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
  async def test_handle_characteristic_write_request(
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

    self.logger.info("[DUT] Add a service.")
    await self.dut_gatt_server.add_service(_GATT_SERVICE)

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self.connect_le_from_ref(
        dut_address_type=android_constants.AddressTypeStatus.PUBLIC,
        ref_address_type=hci.OwnAddressType.RANDOM,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    async with bumble.device.Peer(ref_dut_acl) as peer:
      characteristic = peer.get_characteristics_by_uuid(
          bumble.core.UUID(_WRITE_CHAR_UUID)
      )[0]

      self.logger.info("[REF] Write characteristic.")
      expected_data = secrets.token_bytes(16)
      write_task = asyncio.create_task(
          characteristic.write_value(expected_data, with_response=with_response)
      )

      write_request = await self.dut_gatt_server.wait_for_event(
          event=bl4a_api.GattCharacteristicWriteRequest,
          predicate=lambda request: (
              request.characteristic_uuid == _WRITE_CHAR_UUID
          ),
      )
      self.assertEqual(write_request.value, expected_data)
      self.assertEqual(write_request.response_needed, with_response)

      self.dut_gatt_server.send_response(
          address=write_request.address,
          request_id=write_request.request_id,
          status=android_constants.GattStatus.SUCCESS,
          value=b"",
      )
      await write_task

  @navi_test_base.named_parameterized(
      notify=True,
      indicate=False,
  )
  async def test_handle_subscription(self, is_notify: bool) -> None:
    """Tests sending GATT notification / indication to REF.

    Test steps:
      1. Add a GATT service including a characteristic to the server instance.
      2. Subscribe GATT characteristic from REF.
      3. Handle the subscribe request (CCCD write) from DUT.
      4. Send notification from DUT.
      5. Check notification from REF.

    Args:
      is_notify: Whether to test notification or indication. If True, send
        notification; otherwise, send indication.
    """

    self.logger.info("[DUT] Add a service.")
    await self.dut_gatt_server.add_service(_GATT_SERVICE)
    dut_characteristic = bl4a_api.find_characteristic_by_uuid(
        _SUBSCRIBE_CHAR_UUID, self.dut_gatt_server.services
    )
    if not dut_characteristic.handle:
      self.fail("Cannot find characteristic.")

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self.connect_le_from_ref(
        dut_address_type=android_constants.AddressTypeStatus.PUBLIC,
        ref_address_type=hci.OwnAddressType.RANDOM,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    async with bumble.device.Peer(ref_dut_acl) as peer:
      ref_characteristic = peer.get_characteristics_by_uuid(
          bumble.core.UUID(_SUBSCRIBE_CHAR_UUID)
      )[0]

      self.logger.info("[REF] Subscribe characteristic.")
      notification_queue = asyncio.Queue[bytes]()
      expected_data = secrets.token_bytes(16)
      subscribe_task = asyncio.create_task(
          ref_characteristic.subscribe(
              notification_queue.put_nowait, prefer_notify=is_notify
          )
      )

      self.logger.info("[DUT] Wait for CCCD write.")
      subscribe_request = await self.dut_gatt_server.wait_for_event(
          event=bl4a_api.GattDescriptorWriteRequest,
          predicate=lambda request: (
              request.characteristic_handle == dut_characteristic.handle
              and request.descriptor_uuid == _CCCD_UUID
          ),
      )

      self.logger.info("[DUT] Respond to CCCD write.")
      self.dut_gatt_server.send_response(
          address=subscribe_request.address,
          request_id=subscribe_request.request_id,
          status=android_constants.GattStatus.SUCCESS,
          value=b"",
      )

      self.logger.info("[REF] Wait subscription complete.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await subscribe_task

      self.logger.info("[DUT] Send notification.")
      self.dut_gatt_server.send_notification(
          address=self.ref.random_address,
          characteristic_handle=dut_characteristic.handle,
          confirm=not is_notify,
          value=expected_data,
      )

      self.logger.info("[REF] Wait for notification.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.assertEqual(await notification_queue.get(), expected_data)

  async def test_eatt(self) -> None:
    # EATT requires authentication and encryption.
    self.logger.info("[REF] Connect to DUT.")
    await self.le_connect_and_pair(
        hci.OwnAddressType.RANDOM, direction=constants.Direction.INCOMING
    )
    if not (
        ref_dut_acl := self.ref.device.find_connection_by_bd_addr(
            hci.Address(self.dut.address),
            transport=bumble.core.PhysicalTransport.LE,
        )
    ):
      self.fail("Failed to find ACL connection between DUT and REF.")

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Connect EATT.")
      client = await gatt_client.Client.connect_eatt(ref_dut_acl)

      self.logger.info("[DUT] Discover services.")
      services = await client.discover_services()
      self.assertNotEmpty(services)


if __name__ == "__main__":
  test_runner.main()
