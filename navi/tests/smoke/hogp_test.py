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

"""Tests for HID over GATT Profile(GATT) implementation on Android."""

import asyncio
import enum
import struct

from bumble import device as bumble_device
from bumble import gatt
from bumble import hci
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants


class _HidReportProtocol(enum.IntEnum):
  BOOT = 0x00
  REPORT = 0x01


class _HidReportType(enum.IntEnum):
  INPUT = 0x01
  OUTPUT = 0x02
  FEATURE = 0x03


HID_REPORT_MAP = bytes([
    # fmt: off
    # pylint: disable=line-too-long
    0x05, 0x01,        # Usage Page (Generic Desktop Ctrls)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    0x05, 0x07,        #   Usage Page (Kbrd/Keypad)
    0x19, 0xE0,        #   Usage Minimum (0xE0)
    0x29, 0xE7,        #   Usage Maximum (0xE7)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x08,        #   Report Count (8)
    0x81, 0x02,        #   Input (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position)
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x08,        #   Report Size (8)
    0x81, 0x01,        #   Input (Const,Array,Abs,No Wrap,Linear,Preferred State,No Null Position)
    0x95, 0x06,        #   Report Count (6)
    0x75, 0x08,        #   Report Size (8)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x94,        #   Logical Maximum (-108)
    0x05, 0x07,        #   Usage Page (Kbrd/Keypad)
    0x19, 0x00,        #   Usage Minimum (0x00)
    0x29, 0x94,        #   Usage Maximum (0x94)
    0x81, 0x00,        #   Input (Data,Array,Abs,No Wrap,Linear,Preferred State,No Null Position)
    0x95, 0x05,        #   Report Count (5)
    0x75, 0x01,        #   Report Size (1)
    0x05, 0x08,        #   Usage Page (LEDs)
    0x19, 0x01,        #   Usage Minimum (Num Lock)
    0x29, 0x05,        #   Usage Maximum (Kana)
    0x91, 0x02,        #   Output (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position,Non-volatile)
    0x95, 0x01,        #   Report Count (1)
    0x75, 0x03,        #   Report Size (3)
    0x91, 0x01,        #   Output (Const,Array,Abs,No Wrap,Linear,Preferred State,No Null Position,Non-volatile)
    0xC0,              # End Collection
    0x05, 0x01,        # Usage Page (Generic Desktop Ctrls)
    0x09, 0x02,        # Usage (Mouse)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x02,        #   Report ID (2)
    0x09, 0x01,        #   Usage (Pointer)
    0xA1, 0x00,        #   Collection (Physical)
    0x05, 0x09,        #     Usage Page (Button)
    0x19, 0x01,        #     Usage Minimum (0x01)
    0x29, 0x05,        #     Usage Maximum (0x05)
    0x15, 0x00,        #     Logical Minimum (0)
    0x25, 0x01,        #     Logical Maximum (1)
    0x95, 0x05,        #     Report Count (5)
    0x75, 0x01,        #     Report Size (1)
    0x81, 0x02,        #     Input (Data,Var,Abs,No Wrap,Linear,Preferred State,No Null Position)
    0x95, 0x01,        #     Report Count (1)
    0x75, 0x03,        #     Report Size (3)
    0x81, 0x01,        #     Input (Const,Array,Abs,No Wrap,Linear,Preferred State,No Null Position)
    0x05, 0x01,        #     Usage Page (Generic Desktop Ctrls)
    0x09, 0x30,        #     Usage (X)
    0x09, 0x31,        #     Usage (Y)
    0x16, 0x00, 0x80,  #     Logical Minimum (-32768)
    0x26, 0xFF, 0x7F,  #     Logical Maximum (32767)
    0x75, 0x10,        #     Report Size (16)
    0x95, 0x02,        #     Report Count (2)
    0x81, 0x06,        #     Input (Data,Var,Rel,No Wrap,Linear,Preferred State,No Null Position)
    0x09, 0x38,        #     Usage (Wheel)
    0x15, 0x81,        #     Logical Minimum (-127)
    0x25, 0x7F,        #     Logical Maximum (127)
    0x75, 0x08,        #     Report Size (8)
    0x95, 0x01,        #     Report Count (1)
    0x81, 0x06,        #     Input (Data,Var,Rel,No Wrap,Linear,Preferred State,No Null Position)
    0xC0,              #   End Collection
    0xC0,  # End Collection
])
PROPERTY_HID_HOST_SUPPORTED = "bluetooth.profile.hid.host.enabled"


class HogpTest(navi_test_base.TwoDevicesTestBase):
  ref_hogp_service: gatt.Service
  ref_keyboard_input_report_characteristic: gatt.Characteristic
  ref_keyboard_output_report_characteristic: gatt.Characteristic
  ref_mouse_input_report_characteristic: gatt.Characteristic

  def _setup_hid_service(self) -> None:
    self.ref_keyboard_input_report_characteristic = gatt.Characteristic(
        gatt.GATT_REPORT_CHARACTERISTIC,
        gatt.Characteristic.Properties.READ
        | gatt.Characteristic.Properties.WRITE
        | gatt.Characteristic.Properties.NOTIFY,
        gatt.Characteristic.READABLE | gatt.Characteristic.WRITEABLE,
        bytes(8),
        [
            gatt.Descriptor(
                gatt.GATT_REPORT_REFERENCE_DESCRIPTOR,
                gatt.Descriptor.READABLE,
                bytes([0x01, _HidReportType.INPUT.value]),
            )
        ],
    )

    self.ref_keyboard_output_report_characteristic = gatt.Characteristic(
        gatt.GATT_REPORT_CHARACTERISTIC,
        gatt.Characteristic.Properties.READ
        | gatt.Characteristic.Properties.WRITE
        | gatt.Characteristic.WRITE_WITHOUT_RESPONSE,
        gatt.Characteristic.READABLE | gatt.Characteristic.WRITEABLE,
        bytes([0]),
        [
            gatt.Descriptor(
                gatt.GATT_REPORT_REFERENCE_DESCRIPTOR,
                gatt.Descriptor.READABLE,
                bytes([0x01, _HidReportType.OUTPUT.value]),
            )
        ],
    )
    self.ref_mouse_input_report_characteristic = gatt.Characteristic(
        gatt.GATT_REPORT_CHARACTERISTIC,
        gatt.Characteristic.Properties.READ
        | gatt.Characteristic.Properties.WRITE
        | gatt.Characteristic.Properties.NOTIFY,
        gatt.Characteristic.READABLE | gatt.Characteristic.WRITEABLE,
        bytes(6),
        [
            gatt.Descriptor(
                gatt.GATT_REPORT_REFERENCE_DESCRIPTOR,
                gatt.Descriptor.READABLE,
                bytes([0x02, _HidReportType.INPUT.value]),
            )
        ],
    )
    self.ref_hogp_service = gatt.Service(
        gatt.GATT_HUMAN_INTERFACE_DEVICE_SERVICE,
        [
            gatt.Characteristic(
                gatt.GATT_PROTOCOL_MODE_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ,
                gatt.Characteristic.READABLE,
                bytes([_HidReportProtocol.REPORT.value]),
            ),
            gatt.Characteristic(
                gatt.GATT_HID_INFORMATION_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ,
                gatt.Characteristic.READABLE,
                # bcdHID=1.1, bCountryCode=0x00,
                # Flags=RemoteWake|NormallyConnectable
                bytes([0x11, 0x01, 0x00, 0x03]),
            ),
            gatt.Characteristic(
                gatt.GATT_HID_CONTROL_POINT_CHARACTERISTIC,
                gatt.Characteristic.WRITE_WITHOUT_RESPONSE,
                gatt.Characteristic.WRITEABLE,
            ),
            gatt.Characteristic(
                gatt.GATT_REPORT_MAP_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ,
                gatt.Characteristic.READABLE,
                HID_REPORT_MAP,
            ),
            self.ref_keyboard_input_report_characteristic,
            self.ref_keyboard_output_report_characteristic,
            self.ref_mouse_input_report_characteristic,
        ],
    )
    self.ref.device.add_service(self.ref_hogp_service)

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.device.adb.getprop(PROPERTY_HID_HOST_SUPPORTED) != "true":
      raise signals.TestAbortClass("HID host is not supported on DUT")

    # Stay awake during the test.
    self.dut.shell("svc power stayon true")
    # Dismiss the keyguard.
    self.dut.shell("wm dismiss-keyguard")

  @override
  async def async_teardown_class(self) -> None:
    await super().async_teardown_class()
    # Stop staying awake during the test.
    self.dut.shell("svc power stayon false")

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()

  async def test_connect(self) -> None:
    """Tests establishing the HID connection from DUT to REF.

    Test Steps:
      1. Establish the HID connection between DUT and REF.
      2. Verify the HID connection is established.
    """
    self._setup_hid_service()
    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      self.logger.info("[DUT] Pair with REF")
      await self.le_connect_and_pair(hci.OwnAddressType.PUBLIC)
      self.logger.info("[DUT] Wait for HID connected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          predicate=lambda e: (
              e.address == self.ref.address
              and e.state == android_constants.ConnectionState.CONNECTED
          ),
      )

  async def test_reconnect(self) -> None:
    """Tests reconnecting the HID connection with the background scanner.

    Test Steps:
      1. Pair with REF.
      2. Terminate the connection.
      3. Start advertising on REF.
      4. Verify the HID connection is re-established by the background scanner.
    """
    await self.test_connect()

    ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
        hci.Address(self.dut.address)
    )
    assert ref_dut_acl is not None
    self.logger.info("[REF] Disconnect")
    await ref_dut_acl.disconnect()

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      self.logger.info("[REF] Restart advertising")
      await self.ref.device.start_advertising(
          own_address_type=hci.OwnAddressType.PUBLIC,
          target=hci.Address(f"{self.dut.address}/P"),
          advertising_type=bumble_device.AdvertisingType.DIRECTED_CONNECTABLE_LOW_DUTY,
      )
      self.logger.info("[DUT] Wait for connected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          predicate=lambda e: (
              e.address == self.ref.address
              and e.state == android_constants.ConnectionState.CONNECTED
          ),
      )

  async def test_keyboard_input(self) -> None:
    """Tests the HID keyboard input.

    Test Steps:
      1. Establish the HID connection between DUT and REF.
      2. Press each key on the keyboard and verify the key down and up events
         on DUT.
    """
    # Leverage the test_connect() to establish the connection.
    await self.test_connect()
    report_characteristic = self.ref_keyboard_input_report_characteristic

    dut_input_cb = self.dut.bl4a.register_callback(bl4a_api.Module.INPUT)
    self.close_after_test.append(dut_input_cb)

    # Wait for the InputActivity to be ready.
    await asyncio.sleep(0.5)

    for hid_key in range(
        constants.UsbHidKeyCode.A, constants.UsbHidKeyCode.Z + 1
    ):
      hid_key_code = constants.UsbHidKeyCode(hid_key)
      android_key_code = android_constants.KeyCode[hid_key_code.name]
      self.logger.info("[REF] Press HID key %s", hid_key_code.name)
      report_characteristic.value = bytes(
          [0x00, 0x00, hid_key, 0x00, 0x00, 0x00, 0x00, 0x00]
      )
      await self.ref.device.notify_subscribers(report_characteristic)
      self.logger.info("[DUT] Wait for key %s down", android_key_code.name)
      event = await dut_input_cb.wait_for_event(bl4a_api.KeyEvent)
      self.assertEqual(event.key_code, android_key_code)
      self.assertEqual(event.action, android_constants.KeyAction.DOWN)

      self.logger.info("[REF] Release HID key %s", hid_key_code.name)
      report_characteristic.value = bytes(8)

      self.logger.info("[DUT] Wait for key %s up", android_key_code.name)
      await self.ref.device.notify_subscribers(report_characteristic)
      event = await dut_input_cb.wait_for_event(bl4a_api.KeyEvent)
      self.assertEqual(event.key_code, android_key_code)
      self.assertEqual(event.action, android_constants.KeyAction.UP)

  async def test_mouse_click(self) -> None:
    """Tests the HID mouse click.

    Test Steps:
      1. Leverage the test_connect() to establish the connection.
      2. Press primary button and wait for button press.
      3. Release primary button and wait for button down.
    """
    # Leverage the test_connect() to establish the connection.
    await self.test_connect()
    report_characteristic = self.ref_mouse_input_report_characteristic

    dut_input_cb = self.dut.bl4a.register_callback(bl4a_api.Module.INPUT)
    self.close_after_test.append(dut_input_cb)

    # Wait for the InputActivity to be ready.
    await asyncio.sleep(0.5)

    self.logger.info("[REF] Press Primary button")
    report_characteristic.value = struct.pack("<BhhB", 0x01, 0, 0, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for button press")
    event = await dut_input_cb.wait_for_event(bl4a_api.MotionEvent)
    self.assertEqual(event.action, android_constants.MotionAction.BUTTON_PRESS)

    self.logger.info("[REF] Release Primary button")
    report_characteristic.value = struct.pack("<BhhB", 0x00, 0, 0, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for button down")
    event = await dut_input_cb.wait_for_event(bl4a_api.MotionEvent)
    self.assertEqual(
        event.action, android_constants.MotionAction.BUTTON_RELEASE
    )

  async def test_mouse_movement(self) -> None:
    """Tests the HID mouse movement.

    Test Steps:
      1. Leverage the test_connect() to establish the connection.
      2. Move on X axis and wait for hover movement.
      3. Move on Y axis and wait for hover movement.
    """
    # Leverage the test_connect() to establish the connection.
    await self.test_connect()
    report_characteristic = self.ref_mouse_input_report_characteristic

    dut_input_cb = self.dut.bl4a.register_callback(bl4a_api.Module.INPUT)
    self.close_after_test.append(dut_input_cb)

    # Wait for the InputActivity to be ready.
    await asyncio.sleep(0.5)

    self.logger.info("[REF] Move on X axis")
    report_characteristic.value = struct.pack("<BhhB", 0, 1, 0, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for hover movement")
    await dut_input_cb.wait_for_event(
        bl4a_api.MotionEvent,
        lambda e: e.action
        in (
            android_constants.MotionAction.HOVER_MOVE,
            android_constants.MotionAction.HOVER_ENTER,
            android_constants.MotionAction.HOVER_EXIT,
        ),
    )
    # Clear all events.
    dut_input_cb.get_all_events(bl4a_api.MotionEvent)

    self.logger.info("[REF] Move on Y axis")
    report_characteristic.value = struct.pack("<BhhB", 0x00, 0, 1, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for hover movement")
    await dut_input_cb.wait_for_event(
        bl4a_api.MotionEvent,
        lambda e: e.action
        in (
            android_constants.MotionAction.HOVER_MOVE,
            android_constants.MotionAction.HOVER_ENTER,
            android_constants.MotionAction.HOVER_EXIT,
        ),
    )


if __name__ == "__main__":
  test_runner.main()
