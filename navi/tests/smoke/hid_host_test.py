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

"""Tests for HID over GATT Profile(GATT) implementation on Android."""

import asyncio
import contextlib
import struct

from bumble import core
from bumble import hci
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import hid
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import input as input_utils

_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_VIDEO_SERVICE_NAME = "video"


class Delegate(hid.Device.Delegate):
  """Delegate for HID device."""

  _report_data: dict[tuple[hid.ReportType, int], bytes]

  def __init__(
      self,
      report_data: dict[tuple[hid.ReportType, int], bytes] | None = None,
  ) -> None:
    super().__init__()
    self._report_data = report_data if report_data is not None else {}

  @override
  def set_report(self, report_type: hid.ReportType, data: bytes) -> None:
    self._report_data[(report_type, int(data[0]))] = data[1:]

  @override
  def get_report(
      self, report_type: hid.ReportType, report_id: int | None = None
  ) -> bytes:
    return self._report_data[(report_type, report_id)] if report_id else b"\x00"


class HidHostTest(navi_test_base.TwoDevicesTestBase):
  ref_hid_device: hid.Device

  def _setup_hid_service(self) -> None:
    delegate = Delegate()
    self.ref_hid_device = hid.Device(self.ref.device, delegate=delegate)
    self.ref.device.sdp_service_records = {
        1: hid.make_device_sdp_record(1, hid.DEFAULT_REPORT_MAP)
    }

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.device.adb.getprop(hid.PROPERTY_HID_HOST_SUPPORTED) != "true":
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

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Verify the HID connection is established.
    """
    self._setup_hid_service()
    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      self.logger.info("[DUT] Pair with REF")
      await self.classic_connect_and_pair(connect_profiles=True)

      self.logger.info("[DUT] Wait for HID connected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )

  async def test_reconnect(self) -> None:
    """Tests reconnecting the HID connection with the background scanner.

    Test steps:
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

      self.logger.info("[DUT] Wait for acl disconnected")
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
      await self.ref_hid_device.connect(ref_dut_acl)

      self.logger.info("[DUT] Wait for connected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )

  async def test_keyboard_input(self) -> None:
    """Tests the HID keyboard input.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Press each key on the keyboard and verify the key down and up events
         on DUT.
    """
    await self.test_connect()

    input_monitor = await input_utils.InputMonitor.create(
        self.dut.device.serial
    )
    self.test_case_context.push(input_monitor)

    self.logger.info("[DUT] Wait for input ready")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["Bumble Keyboard"])

    for hid_key in range(
        constants.UsbHidKeyCode.A, constants.UsbHidKeyCode.Z + 1
    ):
      hid_key_code = constants.UsbHidKeyCode(hid_key)
      android_key_code = android_constants.KeyCode[hid_key_code.name]
      self.logger.info("[REF] Press HID key %s", hid_key_code.name)
      self.ref_hid_device.send_interrupt_data(
          bytes([0x01, 0x00, 0x00, hid_key, 0x00, 0x00, 0x00, 0x00, 0x00])
      )
      self.logger.info("[DUT] Wait for key %s down", android_key_code.name)
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await input_monitor.wait_for_event(
            ["EV_KEY", f"KEY_{hid_key_code.name}", "DOWN"]
        )

      self.logger.info("[REF] Release HID key %s", hid_key_code.name)
      self.ref_hid_device.send_interrupt_data(
          bytes([0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
      )

      self.logger.info("[DUT] Wait for key %s up", android_key_code.name)
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await input_monitor.wait_for_event(
            ["EV_KEY", f"KEY_{hid_key_code.name}", "UP"]
        )

  async def test_mouse_click(self) -> None:
    """Tests the HID mouse click.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Press primary button and wait for button press.
      3. Release primary button and wait for button down.
    """
    await self.test_connect()

    input_monitor = await input_utils.InputMonitor.create(
        self.dut.device.serial
    )
    self.test_case_context.push(input_monitor)

    self.logger.info("[DUT] Wait for input ready")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["Bumble Mouse"])

    self.logger.info("[REF] Press Primary button")
    hid_report = struct.pack("<BBhhB", 0x02, 0x01, 0, 0, 0)
    self.ref_hid_device.send_interrupt_data(hid_report)

    self.logger.info("[DUT] Wait for button press")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_KEY", "BTN_MOUSE", "DOWN"])

    self.logger.info("[REF] Release Primary button")
    hid_report = struct.pack("<BBhhB", 0x02, 0x00, 0, 0, 0)
    self.ref_hid_device.send_interrupt_data(hid_report)

    self.logger.info("[DUT] Wait for button up")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_KEY", "BTN_MOUSE", "UP"])

  async def test_mouse_movement(self) -> None:
    """Tests the HID mouse movement.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Move on X axis and wait for hover movement.
      3. Move on Y axis and wait for hover movement.
    """
    await self.test_connect()

    input_monitor = await input_utils.InputMonitor.create(
        self.dut.device.serial
    )
    self.test_case_context.push(input_monitor)

    self.logger.info("[DUT] Wait for input ready")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["Bumble Mouse"])

    self.logger.info("[REF] Move on X axis")
    hid_report = struct.pack("<BBhhB", 0x02, 0, 1, 0, 0)
    self.ref_hid_device.send_interrupt_data(hid_report)

    self.logger.info("[DUT] Wait for hover movement")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_REL", " REL_X"])

    self.logger.info("[REF] Move on Y axis")
    hid_report = struct.pack("<BBhhB", 0x02, 0, 0, 1, 0)
    self.ref_hid_device.send_interrupt_data(hid_report)

    self.logger.info("[DUT] Wait for hover movement")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_REL", " REL_Y"])

  async def test_reconnection_when_connection_policy_change(self) -> None:
    """Tests the reconnection when connection policy changes.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Change the connection policy to forbidden.
      3. Wait for the connection to be disconnected.
      4. Change the connection policy to allowed.
      5. Wait for the reconnection.
    """
    await self.test_connect()

    self.assertEqual(
        self.dut.bt.getHidHostConnectionPolicy(self.ref.address),
        android_constants.ConnectionPolicy.ALLOWED,
    )

    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST) as dut_hid_cb,
        self.dut.bl4a.register_callback(
            bl4a_api.Module.ADAPTER
        ) as dut_adapter_cb,
    ):
      self.logger.info("[DUT] Change connection policy to allowed")
      self.dut.bt.setHidHostConnectionPolicy(
          self.ref.address, android_constants.ConnectionPolicy.FORBIDDEN
      )

      self.logger.info("[DUT] Wait for connection disconnected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
      )

      self.logger.info("[DUT] Wait for acl disconnected")
      await dut_adapter_cb.wait_for_event(bl4a_api.AclDisconnected)

      self.logger.info("[DUT] Change connection policy to allowed")
      self.dut.bt.setHidHostConnectionPolicy(
          self.ref.address, android_constants.ConnectionPolicy.ALLOWED
      )

      self.logger.info("[DUT] Wait for acl connected")
      await dut_adapter_cb.wait_for_event(
          bl4a_api.AclConnected(
              address=self.ref.address,
              transport=android_constants.Transport.CLASSIC,
          )
      )

      self.logger.info("[DUT] Wait for HID connected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )

  async def test_remove_bond(self) -> None:
    """Tests the HID reconnection after removing the bond.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Remove the bond between DUT and REF.
      3. Wait for the connection to be disconnected.
      4. Verify the ACL is disconnected.
    """
    await self.test_connect()

    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST) as dut_hid_cb,
        self.dut.bl4a.register_callback(
            bl4a_api.Module.ADAPTER
        ) as dut_adapter_cb,
    ):
      self.logger.info("[DUT] Remove bond")
      self.dut.bt.removeBond(self.ref.address)

      self.logger.info("[DUT] Wait for connection disconnected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
      )

      self.logger.info("[DUT] Wait for acl disconnected")
      await dut_adapter_cb.wait_for_event(bl4a_api.AclDisconnected)

      self.logger.info("[DUT] Try to connect to REF")
      is_connected = self.dut.bt.connect(self.ref.address)
      self.assertFalse(is_connected)

  async def test_virtual_unplug(self) -> None:
    """Tests the HID virtual unplug.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Virtual unplug the HID device from the issuer.
      3. Wait for the connection to be disconnected.
      4. Verify the device is not bonded.
    """
    await self.test_connect()

    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST) as dut_hid_cb,
        self.dut.bl4a.register_callback(
            bl4a_api.Module.ADAPTER
        ) as dut_adapter_cb,
    ):
      self.logger.info("[REF] Virtual unplug")
      self.ref_hid_device.virtual_cable_unplug()

      self.logger.info("[DUT] Wait for HID disconnected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
      )

      self.logger.info("[DUT] Wait for acl disconnected")
      await dut_adapter_cb.wait_for_event(bl4a_api.AclDisconnected)

      self.logger.info("[DUT] Wait for bond removed")
      await dut_adapter_cb.wait_for_event(
          bl4a_api.BondStateChanged(
              address=self.ref.address,
              state=android_constants.BondState.NONE,
          )
      )

  async def test_set_and_get_report(self) -> None:
    """Tests the HID set report.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Set the report with report type INPUT_REPORT and report ID 1.
      3. Verify the report is set successfully.
      4. Get the report with report type INPUT_REPORT and report ID 1.
      5. Verify the report is get successfully.
    """
    await self.test_connect()

    report_id = 1
    data = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09]

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      self.logger.info(
          "[DUT] Set the report with report type %s",
          hid.ReportType.INPUT_REPORT,
      )
      self.dut.bt.setHidHostReport(
          self.ref.address,
          hid.ReportType.INPUT_REPORT,
          bytes([report_id] + data).hex(),
      )

      self.logger.info("[DUT] Verify the report is set successfully")
      await dut_hid_cb.wait_for_event(
          bl4a_api.HidHostHandshake(
              address=self.ref.address,
              status=hid.HandshakeMessage.ResultCode.SUCCESSFUL,
          )
      )

      self.logger.info("[DUT] Get the report with report ID 1")
      self.dut.bt.getHidHostReport(
          self.ref.address, hid.ReportType.INPUT_REPORT, 1, 0
      )

      self.logger.info("[DUT] Wait for the report with report ID 1")
      event = await dut_hid_cb.wait_for_event(bl4a_api.HidHostReport)

      self.logger.info("[DUT] Verify the report is correct")
      self.assertEqual(event.address, self.ref.address)
      self.assertSequenceEqual(event.report, bytes([report_id] + data))

  async def test_send_data(self) -> None:
    """Tests the HID send data.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Send the data to the HID device.
      3. Verify the data is received correctly.
    """
    await self.test_connect()

    report_queue = asyncio.Queue[tuple[hid.ReportType, bytes]]()

    def on_interrupt_pdu(report_type: hid.ReportType, data: bytes) -> None:
      report_queue.put_nowait((report_type, data))

    self.ref_hid_device.on(hid.HID.EVENT_INTERRUPT_DATA, on_interrupt_pdu)

    self.logger.info(
        "[DUT] Send the report with report type %s",
    )
    data = bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09])
    self.dut.bt.sendHidHostData(self.ref.address, data.hex())

    self.logger.info("[REF] Check report")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      report = await report_queue.get()
    self.assertEqual(report[0], hid.ReportType.OUTPUT_REPORT)
    self.assertSequenceEqual(report[1], data)

  async def test_set_idle_time(self) -> None:
    """Tests the HID set idle time.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Set the idle time to 500ms.
      3. Wait for the idle time changed.
      4. Get the idle time.
      5. Verify the idle time is correctly set.
    """
    await self.test_connect()

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      idle_time = 125
      idle_time_ms = idle_time * 4
      self.logger.info("[DUT] Set the idle time to %sms", idle_time_ms)
      self.dut.bt.setHidHostIdleTime(self.ref.address, idle_time)

      # TODO: Remove the sleep and use the callback instead.
      self.logger.info("[DUT] Wait for the idle time changed")
      await asyncio.sleep(0.5)

      self.logger.info("[DUT] Get the idle time")
      self.dut.bt.getHidHostIdleTime(self.ref.address)

      self.logger.info("[DUT] Verify the idle time is %sms", idle_time_ms)
      await dut_hid_cb.wait_for_event(
          bl4a_api.HidHostIdleTimeChanged(
              address=self.ref.address,
              idle_time=idle_time,
          )
      )

  @navi_test_base.parameterized(
      (android_constants.ConnectionPolicy.ALLOWED),
      (android_constants.ConnectionPolicy.FORBIDDEN),
  )
  async def test_bt_on_off(
      self, connection_policy: android_constants.ConnectionPolicy
  ) -> None:
    """Tests the BT on off.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. If connection policy is FORBIDDEN, set it.
      3. Turn off BT on DUT.
      4. Wait for HID to disconnect.
      5. Turn on BT on DUT.
      6. Attempt to connect to REF.
      7. Verify that HID connects if connection policy is ALLOWED, and does not
         connect if FORBIDDEN.

    Args:
      connection_policy: The connection policy to set.
    """
    await self.test_connect()

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      if connection_policy == android_constants.ConnectionPolicy.FORBIDDEN:
        self.logger.info("[DUT] Set connection policy to forbidden")
        self.dut.bt.setHidHostConnectionPolicy(
            self.ref.address, android_constants.ConnectionPolicy.FORBIDDEN
        )

      self.logger.info("[DUT] Turn off BT")
      self.dut.bt.disable()

      self.logger.info("[DUT] Wait for BT disabled.")
      self.dut.bt.waitForAdapterState(android_constants.AdapterState.OFF)

      self.logger.info("[DUT] Wait for HID disconnected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
      )

      self.logger.info("[DUT] Turn on BT")
      self.dut.bt.enable()

      self.logger.info("[DUT] Wait for BT enabled.")
      self.dut.bt.waitForAdapterState(android_constants.AdapterState.ON)

      is_connected = self.dut.bt.connect(self.ref.address)

      if connection_policy == android_constants.ConnectionPolicy.ALLOWED:
        self.logger.info("[DUT] Wait for HID connected")
        await dut_hid_cb.wait_for_event(
            bl4a_api.ProfileConnectionStateChanged(
                address=self.ref.address,
                state=android_constants.ConnectionState.CONNECTED,
            ),
        )
      else:
        self.assertFalse(is_connected)

  async def test_get_and_set_protocol_mode(self) -> None:
    """Tests the HID get and set protocol mode.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Get the protocol mode.
      3. Verify the protocol mode is correctly set.
      4. Set the protocol mode to BOOT_MODE.
      5. Verify the protocol mode is correctly set.
      6. Set the protocol mode to REPORT_MODE.
      7. Verify the protocol mode is correctly set.
    """
    await self.test_connect()

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      self.logger.info("[DUT] Get the protocol mode")
      self.dut.bt.getHidHostProtocolMode(self.ref.address)

      self.logger.info("[DUT] Verify the protocol mode is REPORT_PROTOCOL")
      await dut_hid_cb.wait_for_event(
          bl4a_api.HidHostProtocolModeChanged(
              address=self.ref.address,
              protocol_mode=android_constants.HidHostProtocolMode.REPORT,
          )
      )

      self.logger.info("[DUT] Set the protocol mode to BOOT_PROTOCOL")
      self.dut.bt.setHidHostProtocolMode(
          self.ref.address, android_constants.HidHostProtocolMode.BOOT
      )

      self.logger.info("[DUT] Verify the protocol mode is set successfully")
      await dut_hid_cb.wait_for_event(
          bl4a_api.HidHostHandshake(
              address=self.ref.address,
              status=hid.HandshakeMessage.ResultCode.SUCCESSFUL,
          )
      )

      self.logger.info("[DUT] Get the protocol mode")
      self.dut.bt.getHidHostProtocolMode(self.ref.address)

      self.logger.info("[DUT] Verify the protocol mode is BOOT_PROTOCOL")
      await dut_hid_cb.wait_for_event(
          bl4a_api.HidHostProtocolModeChanged(
              address=self.ref.address,
              protocol_mode=android_constants.HidHostProtocolMode.BOOT,
          )
      )

      self.logger.info("[DUT] Set the protocol mode to REPORT_PROTOCOL")
      self.dut.bt.setHidHostProtocolMode(
          self.ref.address,
          android_constants.HidHostProtocolMode.REPORT,
      )

      self.logger.info("[DUT] Verify the protocol mode is set successfully")
      await dut_hid_cb.wait_for_event(
          bl4a_api.HidHostHandshake(
              address=self.ref.address,
              status=hid.HandshakeMessage.ResultCode.SUCCESSFUL,
          )
      )

      self.logger.info("[DUT] Get the protocol mode")
      self.dut.bt.getHidHostProtocolMode(self.ref.address)

      self.logger.info("[DUT] Verify the protocol mode is REPORT_PROTOCOL")
      await dut_hid_cb.wait_for_event(
          bl4a_api.HidHostProtocolModeChanged(
              address=self.ref.address,
              protocol_mode=android_constants.HidHostProtocolMode.REPORT,
          )
      )

  async def test_remove_bond_when_connection_pending(self) -> None:
    """Tests the HID remove bond when connection pending.

    Test steps:
      1. Start the HID connection between DUT and REF.
      2. Set the connectable to False on REF.
      3. DUT attempts to connect to REF.
      4. Remove the bond between DUT and REF.
      5. Verify the hid host profile connection is not established.
    """
    await self.test_connect()

    self.logger.info("[DUT] Disconnect")
    self.dut.bt.disconnect(self.ref.address)

    self.logger.info("[REF] Set connectable to False")
    await self.ref.device.set_connectable(False)

    with (
        self.dut.bl4a.register_callback(
            bl4a_api.Module.HID_HOST
        ) as hid_host_cb,
        self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as adapter_cb,
    ):
      self.logger.info("[DUT] Connect")
      self.dut.bt.connect(self.ref.address)

      self.logger.info("[DUT] Wait for HID connecting")
      await hid_host_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTING,
          )
      )

      self.logger.info("[DUT] Remove bond")
      self.dut.bt.removeBond(self.ref.address)

      self.logger.info("[DUT] Wait for acl disconnected")
      await adapter_cb.wait_for_event(bl4a_api.AclDisconnected)

      self.logger.info("[DUT] Wait for HID disconnected")
      await hid_host_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
      )


if __name__ == "__main__":
  test_runner.main()
