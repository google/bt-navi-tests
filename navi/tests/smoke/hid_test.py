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
import struct

from bumble import core
from bumble import hci
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import hid
from navi.tests import navi_test_base
from navi.tests.smoke import hogp_test
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants

_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_PREPARE_INPUT_ACTIVITY_TIMEOUT_SECONDS = 0.5


class HidTest(navi_test_base.TwoDevicesTestBase):
  ref_hid_server: hid.Server[hid.DeviceProtocol]
  ref_hid_device: hid.DeviceProtocol

  def _setup_hid_service(self) -> None:
    self.ref_hid_server = hid.Server(self.ref.device, hid.DeviceProtocol)
    self.ref.device.sdp_service_records = {
        1: hid.make_device_sdp_record(1, hogp_test.HID_REPORT_MAP)
    }

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if (
        self.dut.device.adb.getprop(hogp_test.PROPERTY_HID_HOST_SUPPORTED)
        != "true"
    ):
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
      await self.classic_connect_and_pair()

      self.logger.info("[DUT] Wait for HID connected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          predicate=lambda e: (
              e.address == self.ref.address
              and e.state == android_constants.ConnectionState.CONNECTED
          ),
      )

      self.logger.info("[REF] Wait for HID connected")
      self.ref_hid_device = await self.ref_hid_server.wait_connection()

  async def test_reconnect(self) -> None:
    """Tests reconnecting the HID connection with the background scanner.

    Test Steps:
      1. Pair with REF.
      2. Terminate the connection.
      3. Connect HID from REF.
    """
    await self.test_connect()

    ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
        hci.Address(self.dut.address)
    )
    assert ref_dut_acl is not None
    with self.dut.bl4a.register_callback(
        bl4a_api.Module.ADAPTER
    ) as dut_adapter_cb:
      self.logger.info("[REF] Disconnect")
      await ref_dut_acl.disconnect()
      await dut_adapter_cb.wait_for_event(bl4a_api.AclDisconnected)

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      self.logger.info("[REF] Connect ACL")
      ref_dut_acl = await self.ref.device.connect(
          self.dut.address,
          transport=core.BT_BR_EDR_TRANSPORT,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      self.logger.info("[REF] Encrypt")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await ref_dut_acl.authenticate()
        await ref_dut_acl.encrypt()

      self.logger.info("[REF] Connect HID")
      self.ref_hid_device = await hid.DeviceProtocol.connect(ref_dut_acl)

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

    dut_input_cb = self.dut.bl4a.register_callback(bl4a_api.Module.INPUT)
    self.close_after_test.append(dut_input_cb)

    # Wait for the InputActivity to be ready.
    await asyncio.sleep(_PREPARE_INPUT_ACTIVITY_TIMEOUT_SECONDS)

    for hid_key in range(
        constants.UsbHidKeyCode.A, constants.UsbHidKeyCode.Z + 1
    ):
      hid_key_code = constants.UsbHidKeyCode(hid_key)
      android_key_code = android_constants.KeyCode[hid_key_code.name]
      self.logger.info("[REF] Press HID key %s", hid_key_code.name)
      self.ref_hid_device.send_data(
          bytes([0x01, 0x00, 0x00, hid_key, 0x00, 0x00, 0x00, 0x00, 0x00])
      )
      self.logger.info("[DUT] Wait for key %s down", android_key_code.name)
      event = await dut_input_cb.wait_for_event(bl4a_api.KeyEvent)
      self.assertEqual(event.key_code, android_key_code)
      self.assertEqual(event.action, android_constants.KeyAction.DOWN)

      self.logger.info("[REF] Release HID key %s", hid_key_code.name)

      self.logger.info("[DUT] Wait for key %s up", android_key_code.name)
      self.ref_hid_device.send_data(
          bytes([0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
      )
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

    dut_input_cb = self.dut.bl4a.register_callback(bl4a_api.Module.INPUT)
    self.close_after_test.append(dut_input_cb)

    # Wait for the InputActivity to be ready.
    await asyncio.sleep(_PREPARE_INPUT_ACTIVITY_TIMEOUT_SECONDS)

    self.logger.info("[REF] Press Primary button")
    hid_report = struct.pack("<BBhhB", 0x02, 0x01, 0, 0, 0)
    self.ref_hid_device.send_data(hid_report)

    self.logger.info("[DUT] Wait for button press")
    event = await dut_input_cb.wait_for_event(bl4a_api.MotionEvent)
    self.assertEqual(event.action, android_constants.MotionAction.BUTTON_PRESS)

    self.logger.info("[REF] Release Primary button")
    hid_report = struct.pack("<BBhhB", 0x02, 0x00, 0, 0, 0)
    self.ref_hid_device.send_data(hid_report)

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

    dut_input_cb = self.dut.bl4a.register_callback(bl4a_api.Module.INPUT)
    self.close_after_test.append(dut_input_cb)

    # Wait for the InputActivity to be ready.
    await asyncio.sleep(_PREPARE_INPUT_ACTIVITY_TIMEOUT_SECONDS)

    self.logger.info("[REF] Move on X axis")
    hid_report = struct.pack("<BBhhB", 0x02, 0, 1, 0, 0)
    self.ref_hid_device.send_data(hid_report)

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
    hid_report = struct.pack("<BBhhB", 0x02, 0, 0, 1, 0)
    self.ref_hid_device.send_data(hid_report)

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
