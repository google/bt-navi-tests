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

"""Tests for HID Device over GATT Profile(GATT) implementation on Android."""

import asyncio
import datetime

from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import hid as hid_ext
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import retry as retry_lib


_DEFAULT_STEP_TIMEOUT = datetime.timedelta(seconds=10)
_DEFAULT_STEP_TIMEOUT_SECONDS = _DEFAULT_STEP_TIMEOUT.total_seconds()


class HidDeviceTest(navi_test_base.TwoDevicesTestBase):
  ref_hid_host: hid_ext.Host

  def _setup_hid_service(self) -> None:
    self.ref_hid_host = hid_ext.Host(self.ref.device)

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if (
        self.dut.getprop(android_constants.Property.HID_DEVICE_ENABLED)
        != "true"
    ):
      raise signals.TestAbortClass("HID device is not supported on DUT")

  async def _wait_for_hid_device_connected(
      self, dut_hid_cb: bl4a_api.CallbackHandler
  ) -> None:
    self.logger.info("[DUT] Wait for HID Device connected")
    await dut_hid_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
    )

  async def _wait_for_hid_device_disconnected(
      self,
      *,
      dut_hid_cb: bl4a_api.CallbackHandler,
      dut_adapter_cb: bl4a_api.CallbackHandler,
  ) -> None:
    self.logger.info("[DUT] Wait for HID Device disconnected")
    await dut_hid_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.DISCONNECTED,
        ),
    )

    self.logger.info("[DUT] Wait for acl disconnected")
    await dut_adapter_cb.wait_for_event(bl4a_api.AclDisconnected)

  @retry_lib.retry_on_exception(initial_delay_sec=1, num_retries=3)
  def _register_hid_device_app(self) -> bl4a_api.CallbackHandler:
    """Registers the HID Device App with retry since proxy may not be ready."""
    self.logger.info("[DUT] Register HID Device App")
    dut_hid_cb = self.dut.bl4a.register_hid_device_app()
    self.test_case_context.push(dut_hid_cb)

    return dut_hid_cb

  async def _setup_connection(self) -> bl4a_api.CallbackHandler:
    self._setup_hid_service()

    self.logger.info("[DUT] Register HID Device App")
    dut_hid_cb = self._register_hid_device_app()
    self.test_case_context.push(dut_hid_cb)

    self.logger.info("[DUT] Pair with REF")
    await self.classic_connect_and_pair(direction=constants.Direction.INCOMING)

    self.logger.info("[DUT] Connect to HID Device")
    self.dut.bt.hidDeviceConnect(self.ref.address)

    return dut_hid_cb

  def _verify_hid_device_connected(self) -> None:
    """Verifies that the HID Device is connected."""
    self.logger.info("[DUT] Verify the HID Device is connected to REF")
    self.assertIn(self.ref.address, self.dut.bt.getHidDeviceConnectedDevices())

    self.logger.info("[DUT] Verify the HID Device ConnectionState is CONNECTED")
    self.assertEqual(
        self.dut.bt.getHidDeviceConnectionState(self.ref.address),
        android_constants.ConnectionState.CONNECTED,
    )

    self.logger.info(
        "[DUT] Verify the HID Device connection state is connected"
    )
    self.assertIn(
        self.ref.address,
        self.dut.bt.getHidDeviceDevicesMatchingConnectionStates(
            [android_constants.ConnectionState.CONNECTED]
        ),
    )

  def _verify_hid_device_disconnected(self) -> None:
    """Verifies that the HID Device is disconnected."""
    self.logger.info("[DUT] Verify the HID Device is disconnected from REF")
    self.assertNotIn(
        self.ref.address, self.dut.bt.getHidDeviceConnectedDevices()
    )

    self.logger.info(
        "[DUT] Verify the HID Device connection state is disconnected"
    )
    self.assertEqual(
        self.dut.bt.getHidDeviceConnectionState(self.ref.address),
        android_constants.ConnectionState.DISCONNECTED,
    )

    self.logger.info(
        "[DUT] Verify the disconnected HID Device is in"
        " getHidDeviceDevicesMatchingConnectionStates"
    )
    self.assertIn(
        self.ref.address,
        self.dut.bt.getHidDeviceDevicesMatchingConnectionStates(
            [android_constants.ConnectionState.DISCONNECTED]
        ),
    )

  async def test_connect(self) -> None:
    """Tests establishing the HID connection from DUT to REF.

    Test steps:
      1. Register HID Device App on DUT.
      2. Establish the HID connection between DUT and REF.
      3. Verify the HID connection is established.
    """
    dut_hid_cb = await self._setup_connection()
    await self._wait_for_hid_device_connected(dut_hid_cb)
    self._verify_hid_device_connected()

  async def test_disconnect(self) -> None:
    """Tests reconnecting the HID connection.

    Test steps:
      1. Register HID Device App on DUT.
      2. Establish the HID connection between DUT and REF.
      3. Terminate the connection.
      4. Verify the HID connection is terminated.
    """
    dut_hid_cb = await self._setup_connection()
    await self._wait_for_hid_device_connected(dut_hid_cb)
    self._verify_hid_device_connected()

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.ADAPTER
    ) as dut_adapter_cb:
      self.logger.info("[REF] Disconnect")
      self.dut.bt.hidDeviceDisconnect(self.ref.address)

      await self._wait_for_hid_device_disconnected(
          dut_hid_cb=dut_hid_cb,
          dut_adapter_cb=dut_adapter_cb,
      )
      self._verify_hid_device_disconnected()

  async def test_unable_to_connect_without_sdp_settings(self) -> None:
    """Tests unable to connect to the HID Device without SDP settings.

    Test steps:
      1. Connect to HID Device without SDP settings.
    """
    self._setup_hid_service()

    self.logger.info("[DUT] Connect to HID Device without SDP settings")
    self.assertFalse(self.dut.bt.hidDeviceConnect(self.ref.address))

  async def test_send_report(self) -> None:
    """Tests sending report data from the HID Device.

    Test steps:
      1. Register HID Device App on DUT.
      2. Establish the HID connection between DUT and REF.
      3. Send report data from the HID Device.
      4. Verify the report data is received by the HID host.
    """
    dut_hid_cb = await self._setup_connection()
    await self._wait_for_hid_device_connected(dut_hid_cb)
    self._verify_hid_device_connected()

    report_type_queue = asyncio.Queue[hid_ext.ReportType]()
    report_data_queue = asyncio.Queue[bytes]()

    def on_interrupt_pdu(report_type: hid_ext.ReportType, data: bytes) -> None:
      report_type_queue.put_nowait(report_type)
      report_data_queue.put_nowait(data)

    self.ref_hid_host.on(hid_ext.HID.EVENT_INTERRUPT_DATA, on_interrupt_pdu)

    self.logger.info("[DUT] Send report")
    data = [0x00, 0x01, 0x02, 0x03]
    self.dut.bt.hidDeviceSendReport(self.ref.address, 0x01, data)

    self.logger.info("[REF] Check report")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      report_type = await report_type_queue.get()
      report_data = await report_data_queue.get()
    self.assertEqual(report_type, hid_ext.ReportType.INPUT_REPORT)
    self.assertSequenceEqual(report_data, data)

  async def test_reply_report(self) -> None:
    """Tests replying report data to the HID Device.

    Test steps:
      1. Register HID Device App on DUT.
      2. Establish the HID connection between DUT and REF.
      3. Send get_report from the HID Host.
      4. Reply report data to the ref.
      5. Verify the reply report is received.
    """
    dut_hid_cb = await self._setup_connection()
    await self._wait_for_hid_device_connected(dut_hid_cb)
    self._verify_hid_device_connected()

    report_id = 1
    data = [0x00, 0x01, 0x02, 0x03]

    self.logger.info("[REF] Get report")
    get_report_task = asyncio.create_task(
        self.ref_hid_host.get_report(
            hid_ext.ReportType.INPUT_REPORT, report_id, 0
        )
    )
    self.logger.info("[DUT] Wait for Get report request")
    await dut_hid_cb.wait_for_event(bl4a_api.HidDeviceGetReportRequest)

    self.logger.info("[DUT] Reply report")
    self.dut.bt.hidDeviceReplyReport(
        self.ref.address,
        hid_ext.ReportType.INPUT_REPORT,
        report_id,
        data,
    )

    self.logger.info("[REF] Check report")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      report = await get_report_task
      self.assertEqual(report, bytes(data))

  async def test_report_error(self) -> None:
    """Tests reporting error data to the HID Device.

    Test steps:
      1. Register HID Device App on DUT.
      2. Establish the HID connection between DUT and REF.
      3. Report error data to the HID Device.
      4. Verify the error is received by the HID Host.
    """
    dut_hid_cb = await self._setup_connection()
    await self._wait_for_hid_device_connected(dut_hid_cb)
    self._verify_hid_device_connected()

    report_id = 1

    self.logger.info("[REF] Get report")
    get_report_task = asyncio.create_task(
        self.ref_hid_host.get_report(
            hid_ext.ReportType.INPUT_REPORT, report_id, 0
        )
    )
    self.logger.info("[DUT] Wait for Get report request")
    await dut_hid_cb.wait_for_event(bl4a_api.HidDeviceGetReportRequest)

    self.logger.info("[DUT] Report error")
    self.dut.bt.hidDeviceReportError(
        self.ref.address, hid_ext.HandshakeMessage.ResultCode.NOT_READY
    )

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      with self.assertRaises(hid_ext.HidProtocolError) as control_message:
        await get_report_task
    self.assertEqual(
        control_message.exception.result_code,
        hid_ext.HandshakeMessage.ResultCode.NOT_READY,
    )

  async def test_get_user_app_name(self) -> None:
    """Tests getting the user app name of the HID Device.

    Test steps:
      1. Register HID Device App on DUT.
      2. Establish the HID connection between DUT and REF.
      3. Verify the user app name is correct.
    """
    dut_hid_cb = await self._setup_connection()
    await self._wait_for_hid_device_connected(dut_hid_cb)
    self._verify_hid_device_connected()

    self.logger.info("[DUT] Get user app name")
    self.assertEqual(
        self.dut.bt.getHidDeviceUserAppName(),
        android_constants.PACKAGE_NAME_BLUETOOTH_SNIPPET,
    )

  async def test_bad_descriptor_hid_device(self) -> None:
    """Tests registering HID Device App with a bad descriptor.

    Note: Bad descriptor is allowed in Android because it doesn't perform
    semantic validation.

    Test steps:
      1. Register HID Device App with bad descriptor.
      2. Verify that the registration is successful.
    """
    self.logger.info("[DUT] Register HID Device App with bad descriptor")
    with self.dut.bl4a.register_hid_device_app(
        descriptors=[0xFE, 0xFE, 0xFE]
    ) as bad_dut_hid_cb:
      self.logger.info("[DUT] Wait for app registration status")
      await bad_dut_hid_cb.wait_for_event(
          bl4a_api.HidDeviceAppStatusChanged(
              address=None,
              registered=True,
          )
      )


if __name__ == "__main__":
  test_runner.main()
