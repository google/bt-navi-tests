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
from unittest import mock

from bumble import att
from bumble import core
from bumble import gatt
from bumble import hci
from bumble import pairing
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import hid
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import input as input_utils

_VIDEO_SERVICE_NAME = "video"
_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0


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
                bytes([0x01, hid.ReportType.INPUT_REPORT.value]),
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
                bytes([0x01, hid.ReportType.OUTPUT_REPORT.value]),
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
                bytes([0x02, hid.ReportType.INPUT_REPORT.value]),
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
                bytes([hid.ProtocolMode.REPORT_PROTOCOL.value]),
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
                hid.DEFAULT_REPORT_MAP,
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

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()

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
      await self.le_connect_and_pair(
          hci.OwnAddressType.RANDOM, connect_profiles=True
      )
      self.logger.info("[DUT] Wait for HID connected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.random_address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )

  async def test_reconnect(self) -> None:
    """Tests reconnecting the HID connection with the background scanner.

    Test steps:
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
          own_address_type=hci.OwnAddressType.RANDOM,
      )
      self.logger.info("[DUT] Wait for connected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.random_address,
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
    # Leverage the test_connect() to establish the connection.
    await self.test_connect()
    report_characteristic = self.ref_keyboard_input_report_characteristic

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
      report_characteristic.value = bytes(
          [0x00, 0x00, hid_key, 0x00, 0x00, 0x00, 0x00, 0x00]
      )
      await self.ref.device.notify_subscribers(report_characteristic)
      self.logger.info("[DUT] Wait for key %s down", android_key_code.name)
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await input_monitor.wait_for_event(
            ["EV_KEY", f"KEY_{hid_key_code.name}", "DOWN"]
        )

      self.logger.info("[REF] Release HID key %s", hid_key_code.name)
      report_characteristic.value = bytes(8)

      self.logger.info("[DUT] Wait for key %s up", android_key_code.name)
      await self.ref.device.notify_subscribers(report_characteristic)
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await input_monitor.wait_for_event(
            ["EV_KEY", f"KEY_{hid_key_code.name}", "UP"]
        )

  async def test_mouse_click(self) -> None:
    """Tests the HID mouse click.

    Test steps:
      1. Leverage the test_connect() to establish the connection.
      2. Press primary button and wait for button press.
      3. Release primary button and wait for button down.
    """
    # Leverage the test_connect() to establish the connection.
    await self.test_connect()
    report_characteristic = self.ref_mouse_input_report_characteristic

    input_monitor = await input_utils.InputMonitor.create(
        self.dut.device.serial
    )
    self.test_case_context.push(input_monitor)

    self.logger.info("[DUT] Wait for input ready")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["Bumble Mouse"])

    self.logger.info("[REF] Press Primary button")
    report_characteristic.value = struct.pack("<BhhB", 0x01, 0, 0, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for button press")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_KEY", "BTN_MOUSE", "DOWN"])

    self.logger.info("[REF] Release Primary button")
    report_characteristic.value = struct.pack("<BhhB", 0x00, 0, 0, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for button up")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_KEY", "BTN_MOUSE", "UP"])

  async def test_mouse_movement(self) -> None:
    """Tests the HID mouse movement.

    Test steps:
      1. Leverage the test_connect() to establish the connection.
      2. Move on X axis and wait for hover movement.
      3. Move on Y axis and wait for hover movement.
    """
    # Leverage the test_connect() to establish the connection.
    await self.test_connect()
    report_characteristic = self.ref_mouse_input_report_characteristic

    input_monitor = await input_utils.InputMonitor.create(
        self.dut.device.serial
    )
    self.test_case_context.push(input_monitor)

    self.logger.info("[DUT] Wait for input ready")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["Bumble Mouse"])

    self.logger.info("[REF] Move on X axis")
    report_characteristic.value = struct.pack("<BhhB", 0, 1, 0, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for hover movement")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_REL", " REL_X"])

    self.logger.info("[REF] Move on Y axis")
    report_characteristic.value = struct.pack("<BhhB", 0x00, 0, 1, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for hover movement")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_REL", " REL_Y"])

  async def test_set_preferred_transport(self) -> None:
    """Tests setting the preferred transport for HID Host.

    Test steps:
      1. Establish the HID connection over Classic.
      2. Set the preferred transport to LE.
      3. Verify the transport switch to LE.
    """
    self._setup_hid_service()

    ref_classic_hid_device = hid.Device(self.ref.device, delegate=None)
    self.ref.device.sdp_service_records = {
        1: hid.make_device_sdp_record(1, hid.DEFAULT_REPORT_MAP)
    }
    condition = asyncio.Condition()
    mouse_characteristic_subscribers: list[att.Bearer | None] = [None]

    @ref_classic_hid_device.on(hid.Device.EVENT_CONNECTION)
    @ref_classic_hid_device.on(hid.Device.EVENT_DISCONNECTION)
    async def on_ref_hid_connection_state_changed() -> None:
      async with condition:
        condition.notify_all()

    @self.ref_mouse_input_report_characteristic.on(
        gatt.Characteristic.EVENT_SUBSCRIPTION
    )
    async def on_ref_hid_control_channel_state_changed(
        bearer: att.Bearer, notify_enabled: bool, indicate_enabled: bool
    ) -> None:
      del notify_enabled, indicate_enabled
      async with condition:
        mouse_characteristic_subscribers[0] = bearer
        condition.notify_all()

    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST) as dut_hid_cb,
        self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as adapter_cb,
    ):
      self.logger.info("[DUT] Pair and connect LE")
      key_distribution = (
          pairing.PairingDelegate.KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY
          | pairing.PairingDelegate.KeyDistribution.DISTRIBUTE_IDENTITY_KEY
          | pairing.PairingDelegate.KeyDistribution.DISTRIBUTE_LINK_KEY
      )
      await self.le_connect_and_pair(
          hci.OwnAddressType.PUBLIC,
          connect_profiles=False,
          delegate=pairing.PairingDelegate(
              io_capability=pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_KEYBOARD_INPUT,
              local_initiator_key_distribution=key_distribution,
              local_responder_key_distribution=key_distribution,
          ),
      )
      self.logger.info("[DUT] Wait for HID service discovered")
      discovered_uuids = set[core.UUID]()
      while not discovered_uuids.issuperset({
          core.BT_HUMAN_INTERFACE_DEVICE_SERVICE,
          gatt.GATT_HUMAN_INTERFACE_DEVICE_SERVICE,
      }):
        uuid_changed_event = await adapter_cb.wait_for_event(
            bl4a_api.UuidChanged(address=self.ref.address, uuids=mock.ANY)
        )
        discovered_uuids.update(map(core.UUID, uuid_changed_event.uuids or []))
        self.logger.info("[DUT] uuids: %r", discovered_uuids)

      self.logger.info("[DUT] Connect to REF")
      self.dut.bt.connect(self.ref.address)

      self.logger.info("[DUT] Wait for HID connected over Classic")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )

      initial_transport = self.dut.bt.getHidHostPreferredTransport(
          self.ref.address
      )
      self.logger.info(
          "[DUT] Initial preferred transport: %s", initial_transport
      )
      self.assertEqual(
          initial_transport,
          android_constants.Transport.CLASSIC,
          msg="Initial preferred transport should be CLASSIC",
      )
      async with (
          self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS),
          condition,
      ):
        self.logger.info("[REF] Wait for HID connected over Classic")
        await condition.wait_for(
            lambda: ref_classic_hid_device.control_channel is not None
        )

      self.logger.info("[DUT] Set preferred transport to LE")
      self.assertTrue(
          self.dut.bt.setHidHostPreferredTransport(
              self.ref.address, android_constants.Transport.LE
          ),
          msg="Failed to set preferred transport to LE",
      )

      updated_transport = self.dut.bt.getHidHostPreferredTransport(
          self.ref.address
      )
      self.logger.info(
          "[DUT] Updated preferred transport: %s", updated_transport
      )
      self.assertEqual(
          updated_transport,
          android_constants.Transport.LE,
          msg="Updated preferred transport should be LE",
      )

      self.logger.info("[DUT] Wait for transport switch (DISCONNECTED Classic)")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
      )
      async with (
          self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS),
          condition,
      ):
        self.logger.info("[REF] Wait for HID disconnected over Classic")
        await condition.wait_for(
            lambda: ref_classic_hid_device.control_channel is None
        )

      self.logger.info("[DUT] Wait for transport switch (CONNECTED LE)")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )
      async with (
          self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS),
          condition,
      ):
        self.logger.info("[REF] Wait for HID connected over LE")
        await condition.wait_for(
            lambda: mouse_characteristic_subscribers[0] is not None
        )

  async def test_get_report(self) -> None:
    """Tests HOGP get report.

    Test steps:
      1. Establish the HID connection.
      2. Get the report with report type INPUT_REPORT and report ID 1
      (Keyboard).
      3. Verify the report is retrieved successfully.
    """
    await self.test_connect()

    report_id = 1

    dut_hid_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    self.test_case_context.push(dut_hid_cb)

    self.logger.info("[DUT] Get HOGP report for Keyboard (ID 1)")
    self.dut.bt.getHidHostReport(
        self.ref.random_address,
        hid.ReportType.INPUT_REPORT,
        report_id,
        0,
    )

    self.logger.info("[DUT] Wait for HidHostReport event")
    event = await dut_hid_cb.wait_for_event(bl4a_api.HidHostReport)

    self.logger.info("[DUT] Verify report data")
    self.assertEqual(event.address, self.ref.random_address)
    self.assertSequenceEqual(event.report, [report_id] + [0] * 8)

  async def test_set_report(self) -> None:
    """Tests HOGP set report.

    Test steps:
      1. Establish the HID connection.
      2. Set the report with report type INPUT_REPORT and report ID 1.
      3. Verify the handshake status is successful.
    """
    await self.test_connect()

    report_id = 1
    data = [0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09]
    report_hex = bytes([report_id] + data).hex()

    dut_hid_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    self.test_case_context.push(dut_hid_cb)

    self.logger.info("[DUT] Set HOGP report")
    self.dut.bt.setHidHostReport(
        self.ref.random_address,
        hid.ReportType.INPUT_REPORT,
        report_hex,
    )

    self.logger.info("[DUT] Wait for handshake")
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostHandshake(
            address=self.ref.random_address,
            status=hid.HandshakeMessage.ResultCode.SUCCESSFUL,
        )
    )

    # Verify on Bumble side that the characteristic value was updated
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Verify characteristic value")
      condition = asyncio.Condition()

      @self.ref_keyboard_input_report_characteristic.on(
          self.ref_keyboard_input_report_characteristic.EVENT_WRITE
      )
      async def on_write(*args, **kwargs) -> None:
        del args, kwargs
        async with condition:
          condition.notify_all()

      async with condition:
        await condition.wait_for(
            lambda: self.ref_keyboard_input_report_characteristic.value
            == bytes(data)
        )

  async def test_get_protocol_mode(self) -> None:
    """Tests HOGP get protocol mode.

    Test steps:
      1. Establish the HID connection.
      2. Get the protocol mode.
      3. Verify the protocol mode is REPORT_PROTOCOL.
    """
    await self.test_connect()

    dut_hid_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    self.test_case_context.push(dut_hid_cb)

    self.logger.info("[DUT] Get HOGP protocol mode")
    self.dut.bt.getHidHostProtocolMode(self.ref.random_address)

    self.logger.info("[DUT] Wait for protocol mode event")
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostProtocolModeChanged(
            address=self.ref.random_address,
            protocol_mode=android_constants.HidHostProtocolMode.REPORT,
        )
    )

  async def test_set_protocol_mode(self) -> None:
    """Tests HOGP set protocol mode.

    Test steps:
      1. Establish the HID connection.
      2. Set the protocol mode to BOOT_PROTOCOL.
      3. Verify the handshake is successful.
      4. Get the protocol mode and verify it is BOOT_PROTOCOL.
      5. Set it back to REPORT_PROTOCOL.
      6. Verify the handshake is successful.
      7. Get the protocol mode and verify it is REPORT_PROTOCOL.
    """
    await self.test_connect()

    dut_hid_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    self.test_case_context.push(dut_hid_cb)

    self.logger.info("[DUT] Set HOGP protocol mode to BOOT_PROTOCOL")
    self.dut.bt.setHidHostProtocolMode(
        self.ref.random_address,
        android_constants.HidHostProtocolMode.BOOT,
    )

    self.logger.info("[DUT] Wait for handshake")
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostHandshake(
            address=self.ref.random_address,
            status=hid.HandshakeMessage.ResultCode.SUCCESSFUL,
        )
    )

    self.logger.info("[DUT] Get protocol mode")
    self.dut.bt.getHidHostProtocolMode(self.ref.random_address)
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostProtocolModeChanged(
            address=self.ref.random_address,
            protocol_mode=android_constants.HidHostProtocolMode.BOOT,
        )
    )

    self.logger.info("[DUT] Set HOGP protocol mode to REPORT_PROTOCOL")
    self.dut.bt.setHidHostProtocolMode(
        self.ref.random_address,
        android_constants.HidHostProtocolMode.REPORT,
    )

    self.logger.info("[DUT] Wait for handshake")
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostHandshake(
            address=self.ref.random_address,
            status=hid.HandshakeMessage.ResultCode.SUCCESSFUL,
        )
    )

    self.logger.info("[DUT] Get protocol mode")
    self.dut.bt.getHidHostProtocolMode(self.ref.random_address)
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostProtocolModeChanged(
            address=self.ref.random_address,
            protocol_mode=android_constants.HidHostProtocolMode.REPORT,
        )
    )


if __name__ == "__main__":
  test_runner.main()
