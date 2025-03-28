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
import secrets
import uuid

from bumble import gatt
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
from navi.utils import retry

_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0

_Property = android_constants.GattCharacteristicProperty
_Permission = android_constants.GattCharacteristicPermission
_CCCD_UUID = (
    bluetooth_constants.BluetoothAssignedUuid.CLIENT_CHARACTERISTIC_CONFIGURATION_DESCRIPTOR
)


class GattServerTest(navi_test_base.TwoDevicesTestBase):
  """Tests of GATT server implementation on Pixel."""

  dut_gatt_server: bl4a_api.GattServer
  dut_advertiser: bl4a_api.LegacyAdvertiser

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

    self.logger.info("[DUT] Start advertising.")
    self.dut_advertiser = await self.dut.bl4a.start_legacy_advertiser(
        bl4a_api.LegacyAdvertiseSettings(
            own_address_type=android_constants.AddressTypeStatus.PUBLIC,
            advertise_mode=android_constants.LegacyAdvertiseMode.LOW_LATENCY,
        ),
    )

  @override
  async def async_teardown_test(self) -> None:
    await super().async_teardown_test()
    self.dut_gatt_server.close()
    self.dut_advertiser.stop()

  @retry.retry_on_exception()
  async def _make_le_connection(self) -> bumble.device.Connection:
    """Connects to DUT over LE and returns the connection."""
    ref_dut_acl = await self.ref.device.connect(
        f"{self.dut.address}/P",
        transport=bumble.core.BT_LE_TRANSPORT,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
        own_address_type=hci.OwnAddressType.RANDOM,
    )
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await ref_dut_acl.get_remote_le_features()
    return ref_dut_acl

  async def test_add_service(self) -> None:
    """Tests opening a GATT server on DUT, adding a service discovered by REF.

    Test Steps:
      1. Open a GATT server on DUT.
      2. Add a GATT service to the server instance.
      3. Discover services from REF.
      4. Verify added service is discovered.
    """
    service_uuid = str(uuid.uuid4())
    characteristic_uuid = str(uuid.uuid4())

    self.logger.info("[DUT] Add a service.")
    await self.dut_gatt_server.add_service(
        bl4a_api.GattService(
            uuid=service_uuid,
            characteristics=[
                bl4a_api.GattCharacteristic(
                    uuid=characteristic_uuid,
                    properties=_Property.READ,
                    permissions=_Permission.READ,
                )
            ],
        ),
    )

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self._make_le_connection()

    async with bumble.device.Peer(ref_dut_acl) as peer:
      self.logger.info("[REF] Check services.")
      services = peer.get_services_by_uuid(bumble.core.UUID(service_uuid))
      self.assertLen(services, 1)
      characteristics = services[0].get_characteristics_by_uuid(
          bumble.core.UUID(characteristic_uuid)
      )
      self.assertLen(characteristics, 1)
      self.assertEqual(
          characteristics[0].properties, gatt.Characteristic.Properties.READ
      )

  async def test_handle_characteristic_read_request(self) -> None:
    """Tests handling a characteristic read request.

    Test Steps:
      1. Open a GATT server on DUT.
      2. Add a GATT service including a readable characteristic to the server
      instance.
      3. Read characteristic from REF.
      4. Handle the read request and send response from DUT.
      5. Check read result from REF.
    """
    # UUID must be random here, otherwise there might be interference when
    # multiple tests run in the same box.
    service_uuid = str(uuid.uuid4())
    characteristic_uuid = str(uuid.uuid4())

    self.logger.info("[DUT] Add a service.")
    await self.dut_gatt_server.add_service(
        bl4a_api.GattService(
            uuid=service_uuid,
            characteristics=[
                bl4a_api.GattCharacteristic(
                    uuid=characteristic_uuid,
                    properties=_Property.READ,
                    permissions=_Permission.READ,
                )
            ],
        ),
    )

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self._make_le_connection()

    async with bumble.device.Peer(ref_dut_acl) as peer:
      characteristic = peer.get_characteristics_by_uuid(
          bumble.core.UUID(characteristic_uuid)
      )[0]

      self.logger.info("[REF] Read characteristic.")
      read_task = asyncio.create_task(characteristic.read_value())

      read_request = await self.dut_gatt_server.wait_for_event(
          callback_type=bl4a_api.GattCharacteristicReadRequest,
          predicate=lambda request: (
              request.characteristic_uuid == characteristic_uuid
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

  async def test_handle_characteristic_write_request(self) -> None:
    """Tests handling a characteristic write request.

    Test Steps:
      1. Open a GATT server on DUT.
      2. Add a GATT service including a writable characteristic to the server
      instance.
      3. Write characteristic from REF.
      4. Handle the write request and send response from DUT.
      5. Check write result from REF.
    """
    # UUID must be random here, otherwise there might be interference when
    # multiple tests run in the same box.
    service_uuid = str(uuid.uuid4())
    characteristic_uuid = str(uuid.uuid4())

    self.logger.info("[DUT] Add a service.")
    await self.dut_gatt_server.add_service(
        bl4a_api.GattService(
            uuid=service_uuid,
            characteristics=[
                bl4a_api.GattCharacteristic(
                    uuid=characteristic_uuid,
                    properties=_Property.WRITE,
                    permissions=_Permission.WRITE,
                )
            ],
        ),
    )

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self._make_le_connection()

    async with bumble.device.Peer(ref_dut_acl) as peer:
      characteristic = peer.get_characteristics_by_uuid(
          bumble.core.UUID(characteristic_uuid)
      )[0]

      self.logger.info("[REF] Write characteristic.")
      expected_data = secrets.token_bytes(16)
      write_task = asyncio.create_task(
          characteristic.write_value(expected_data, with_response=True)
      )

      write_request = await self.dut_gatt_server.wait_for_event(
          callback_type=bl4a_api.GattCharacteristicWriteRequest,
          predicate=lambda request: (
              request.characteristic_uuid == characteristic_uuid
          ),
      )
      self.assertEqual(write_request.value, expected_data)

      self.dut_gatt_server.send_response(
          address=write_request.address,
          request_id=write_request.request_id,
          status=android_constants.GattStatus.SUCCESS,
          value=b"",
      )
      await write_task

  async def test_notify(self) -> None:
    """Tests sending GATT notification.

    Test Steps:
      1. Add a GATT service including a characteristic to the server instance.
      2. Subscribe GATT characteristic from REF.
      3. Handle the subscribe request (CCCD write) from DUT.
      4. Send notification from DUT.
      5. Check notification from REF.
    """
    # UUID must be random here, otherwise there might be interference when
    # multiple tests run in the same box.
    service_uuid = str(uuid.uuid4())
    characteristic_uuid = str(uuid.uuid4())

    self.logger.info("[DUT] Add a service.")
    await self.dut_gatt_server.add_service(
        bl4a_api.GattService(
            uuid=service_uuid,
            characteristics=[
                bl4a_api.GattCharacteristic(
                    uuid=characteristic_uuid,
                    properties=_Property.READ | _Property.NOTIFY,
                    permissions=_Permission.READ,
                    descriptors=[
                        bl4a_api.GattDescriptor(
                            uuid=_CCCD_UUID,
                            permissions=_Permission.READ | _Permission.WRITE,
                        )
                    ],
                )
            ],
        ),
    )
    dut_characteristic = bl4a_api.find_characteristic_by_uuid(
        characteristic_uuid, self.dut_gatt_server.services
    )
    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self._make_le_connection()

    async with bumble.device.Peer(ref_dut_acl) as peer:
      ref_characteristic = peer.get_characteristics_by_uuid(
          bumble.core.UUID(characteristic_uuid)
      )[0]

      self.logger.info("[REF] Subscribe characteristic.")
      notification_queue = asyncio.Queue[bytes]()
      expected_data = secrets.token_bytes(16)
      subscribe_task = asyncio.create_task(
          ref_characteristic.subscribe(notification_queue.put_nowait)
      )

      self.logger.info("[DUT] Wait for CCCD write.")
      subscribe_request = await self.dut_gatt_server.wait_for_event(
          callback_type=bl4a_api.GattDescriptorWriteRequest,
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
          confirm=False,
          value=expected_data,
      )

      self.logger.info("[REF] Wait for notification.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.assertEqual(await notification_queue.get(), expected_data)


if __name__ == "__main__":
  test_runner.main()
