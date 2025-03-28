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
import secrets
import uuid

from bumble import device
from bumble import gatt
from bumble import hci
from mobly import asserts
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api


class GattClientTest(navi_test_base.TwoDevicesTestBase):

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.getprop(android_constants.Property.GATT_ENABLED) != "true":
      raise signals.TestAbortClass("GATT is not enabled on DUT.")

  async def test_discover_services(self) -> None:
    """Test connect GATT as client."""
    service_uuid = str(uuid.uuid4())
    self.ref.device.add_service(
        gatt.Service(uuid=service_uuid, characteristics=[])
    )

    self.logger.info("[REF] Start advertising.")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM
    )
    self.logger.info("[DUT] Connect to REF.")
    gatt_client = await self.dut.bl4a.connect_gatt_client(
        str(self.ref.random_address),
        android_constants.Transport.LE,
        android_constants.AddressTypeStatus.RANDOM,
    )

    self.logger.info("[DUT] Discover services.")
    services = await gatt_client.discover_services()

    self.logger.info("[DUT] Check services.")
    asserts.assert_true(
        any(service.uuid == service_uuid for service in services),
        "Cannot find service UUID?",
    )

  async def test_write_characteristic(self) -> None:
    """Test write value to characteristics.

    Test steps:
      1. Add a GATT server with a writable characteristic on REF.
      2. Start advertising on REF.
      3. Connect GATT(and LE-ACL) to REF from DUT.
      4. Discover GATT services from DUT.
      5. Write characteristic value on REF from DUT.
      6. Check written value.
    """
    service_uuid = str(uuid.uuid4())
    characteristic_uuid = str(uuid.uuid4())

    write_future = asyncio.get_running_loop().create_future()

    def on_write(_: device.Connection, value: bytes) -> None:
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

    self.logger.info("[REF] Start advertising.")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM
    )
    self.logger.info("[DUT] Connect to REF.")
    gatt_client = await self.dut.bl4a.connect_gatt_client(
        str(self.ref.random_address),
        android_constants.Transport.LE,
        android_constants.AddressTypeStatus.RANDOM,
    )
    self.logger.info("[DUT] Discover services.")
    services = await gatt_client.discover_services()
    characteristic = bl4a_api.find_characteristic_by_uuid(
        characteristic_uuid, services
    )

    self.logger.info("[DUT] Write characteristic.")
    expected_value = secrets.token_bytes(16)
    await gatt_client.write_characteristic(
        characteristic.handle,
        expected_value,
        android_constants.GattWriteType.DEFAULT,
    )
    self.logger.info("[REF] Check write value.")
    asserts.assert_equal(expected_value, await write_future)

  async def test_characteristic_notification(self) -> None:
    """Test read value from characteristics.

    Test steps:
      1. Add a GATT server with a readable characteristic on REF.
      2. Start advertising on REF.
      3. Connect GATT(and LE-ACL) to REF from DUT.
      4. Discover GATT services from DUT.
      5. Read characteristic value on REF from DUT.
      6. Check read value.
    """
    service_uuid = str(uuid.uuid4())
    characteristic_uuid = str(uuid.uuid4())
    expected_value = secrets.token_bytes(256)

    self.ref.device.add_service(
        gatt.Service(
            uuid=service_uuid,
            characteristics=[
                gatt.Characteristic(
                    uuid=characteristic_uuid,
                    properties=gatt.Characteristic.Properties.READ,
                    permissions=gatt.Characteristic.Permissions.READABLE,
                    value=expected_value,
                )
            ],
        )
    )

    self.logger.info("[REF] Start advertising.")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM
    )
    self.logger.info("[DUT] Connect to REF.")
    gatt_client = await self.dut.bl4a.connect_gatt_client(
        str(self.ref.random_address),
        android_constants.Transport.LE,
        android_constants.AddressTypeStatus.RANDOM,
    )
    self.logger.info("[DUT] Discover services.")
    services = await gatt_client.discover_services()
    characteristic = bl4a_api.find_characteristic_by_uuid(
        characteristic_uuid, services
    )

    self.logger.info("[DUT] Read characteristic.")
    actual_value = await gatt_client.read_characteristic(characteristic.handle)
    self.logger.info("Check read value.")
    asserts.assert_equal(expected_value, actual_value)

  async def test_subscribe_characteristic(self) -> None:
    """Test subscribe value from characteristics.

    Test steps:
      1. Add a GATT server with a notifyable characteristic on REF.
      2. Start advertising on REF.
      3. Connect GATT(and LE-ACL) to REF from DUT.
      4. Discover GATT services from DUT.
      5. Subscribe characteristic value on REF from DUT.
      6. Notify subscribers from REF.
      7. Check read value.
    """
    service_uuid = str(uuid.uuid4())
    characteristic_uuid = str(uuid.uuid4())
    expected_value = secrets.token_bytes(256)

    ref_characteristic = gatt.Characteristic(
        uuid=characteristic_uuid,
        properties=(
            gatt.Characteristic.Properties.READ
            | gatt.Characteristic.Properties.NOTIFY
        ),
        permissions=gatt.Characteristic.Permissions.READABLE,
        value=expected_value,
    )
    self.ref.device.add_service(
        gatt.Service(
            uuid=service_uuid,
            characteristics=[ref_characteristic],
        )
    )

    self.logger.info("[REF] Start advertising.")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM
    )
    self.logger.info("[DUT] Connect to REF.")
    gatt_client = await self.dut.bl4a.connect_gatt_client(
        str(self.ref.random_address),
        android_constants.Transport.LE,
        android_constants.AddressTypeStatus.RANDOM,
    )
    self.logger.info("[DUT] Discover services.")
    services = await gatt_client.discover_services()
    characteristic = bl4a_api.find_characteristic_by_uuid(
        characteristic_uuid, services
    )

    self.logger.info("[DUT] Subscribe characteristic.")
    await gatt_client.subscribe_characteristic_notifications(
        characteristic.handle
    )

    self.logger.info("[REF] Notify subscribers.")
    expected_value = secrets.token_bytes(16)
    await self.ref.device.notify_subscribers(ref_characteristic, expected_value)

    self.logger.info("Check notified value.")
    notification = await gatt_client.wait_for_event(
        bl4a_api.GattCharacteristicChanged,
        lambda e: (e.handle == characteristic.handle),
        datetime.timedelta(seconds=10),
    )
    asserts.assert_equal(expected_value, notification.value)


if __name__ == "__main__":
  test_runner.main()
