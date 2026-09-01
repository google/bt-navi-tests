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

"""Tests for Object Push Profile (OPP) implementation on Android."""

import asyncio
import contextlib
import datetime
import pathlib
import re
import sys
import tempfile
from typing import TypeAlias
import uuid

from bumble import core
from bumble import l2cap
from bumble import rfcomm
from mobly import test_runner
from mobly import signals
from mobly.controllers.android_device_lib import adb
from typing_extensions import override

from navi.bumble_ext import obex
from navi.bumble_ext import opp
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import retry

_OPP_SERVICE_RECORD_HANDLE = 1
_DEFAULT_TIMEOUT_SECONDS = 30.0
_UI_TIMEOUT = datetime.timedelta(seconds=20.0)
_TEST_FILE_MIME_TYPE = 'image/jpeg'
_VIDEO_SERVICE_NAME = 'video'
_TEST_DATA = bytes(i % 256 for i in range(100000))
_ACTION_ACCEPT = 'android.btopp.intent.action.ACCEPT'
_ACTION_DECLINE = 'android.btopp.intent.action.DECLINE'


_CallbackHandler: TypeAlias = bl4a_api.CallbackHandler
_Module: TypeAlias = bl4a_api.Module


class OppTest(navi_test_base.TwoDevicesTestBase):
  ref_opp_server: opp.Server
  bluetooth_package: str

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if self.dut.getprop(android_constants.Property.OPP_ENABLED) != 'true':
      raise signals.TestAbortClass('OPP is not enabled on DUT.')

    # Disable Better Bug to avoid unexpected popups.
    with contextlib.suppress(adb.AdbError):
      self.dut.shell([
          'pm',
          'disable-user',
          '--user',
          f'{self.dut.adb.current_user_id}',
          'com.google.android.apps.internal.betterbug',
      ])

    # Stay awake during the test.
    self.dut.shell('svc power stayon true')
    # Dismiss the keyguard.
    self.dut.shell('wm dismiss-keyguard')
    # Disable heads up notifications to prevent popups from blocking the UI.
    self.dut.shell('settings put global heads_up_notifications_enabled 0')

    # CoD must include OBJECT_TRANSFER for OPP to work.
    self.ref.config.class_of_device = int(
        core.ClassOfDevice(
            major_service_classes=core.ClassOfDevice.MajorServiceClasses.OBJECT_TRANSFER,
            major_device_class=core.ClassOfDevice.MajorDeviceClass.PHONE,
            minor_device_class=core.ClassOfDevice.PhoneMinorDeviceClass.SMARTPHONE,
        )
    )

    if match := re.search(
        r'^package:(com\.(?:google\.)?android\.bluetooth)$',
        self.dut.shell('pm list packages'),
        re.MULTILINE,
    ):
      self.bluetooth_package = match.group(1)
    else:
      self.fail('Failed to find Bluetooth package.')

  @override
  async def async_teardown_class(self) -> None:
    await super().async_teardown_class()
    # Re-enable heads up notifications.
    self.dut.shell('settings put global heads_up_notifications_enabled 1')
    # Stop staying awake during the test.
    self.dut.shell('svc power stayon false')

  @override
  @retry.retry_on_exception()
  async def async_setup_test(self) -> None:
    self.ref.config.name = uuid.uuid4().hex[:8]
    await super().async_setup_test()
    self.dut.ui.screen.on()
    self.dut.shell('wm dismiss-keyguard')
    self.dut.ui.press.home()

    # Set up OPP server on REF.
    self.ref_opp_server = opp.Server(self.ref.device)
    self.ref.device.sdp_service_records = {
        _OPP_SERVICE_RECORD_HANDLE: opp.make_sdp_records(
            opp.SdpInfo(
                service_record_handle=_OPP_SERVICE_RECORD_HANDLE,
                rfcomm_channel=self.ref_opp_server.rfcomm_channel,
                profile_version=opp.Version.V_1_2,
                goep_l2cap_psm=self.ref_opp_server.l2cap_server.psm,
            )
        )
    }

    # Setup pairing and terminate connection.
    with self.dut.bl4a.register_callback(_Module.ADAPTER) as dut_cb:
      await self.classic_connect_and_pair()
      # Wait for ACL disconnection (since there isn't any active profile, it
      # should be disconnected immediately).
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected(
              address=self.ref.address,
              transport=android_constants.Transport.CLASSIC,
          ),
      )

  async def _wait_for_incoming_share_id(self) -> int:
    """Waits for incoming OPP file transfer request via ADB query."""
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      while True:
        output = self.dut.shell(
            'content query --uri content://com.android.bluetooth.opp/btopp'
            ' --projection _id:confirm:direction --where "direction=1 AND'
            ' (confirm=0 OR confirm IS NULL)"'
        )
        if match := re.search(r'_id=(\d+)', output or ''):
          return int(match.group(1))
        await asyncio.sleep(0.2)

  def _answer_incoming_file(self, share_id: int | str, accept: bool) -> None:
    """Accepts an incoming OPP file transfer via ADB broadcast."""
    self.dut.shell([
        'am',
        'broadcast',
        '-a',
        _ACTION_ACCEPT if accept else _ACTION_DECLINE,
        '-d',
        f'content://com.android.bluetooth.opp/btopp/{share_id}',
        '-n',
        f'{self.bluetooth_package}/com.android.bluetooth.opp.BluetoothOppReceiver',
    ])

  async def _make_opp_client_from_ref(self, use_l2cap: bool) -> opp.Client:
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info('[REF] Connect to DUT.')
      ref_dut_acl = await self.ref.device.connect(
          self.dut.address, transport=core.BT_BR_EDR_TRANSPORT
      )
      self.logger.info('[REF] Authenticate and encrypt.')
      await ref_dut_acl.authenticate()
      await ref_dut_acl.encrypt()

      self.logger.info('[REF] Find SDP record.')
      sdp_info = await opp.find_sdp_record(ref_dut_acl)
      if not sdp_info:
        self.fail('Failed to find SDP record for OPP.')

      bearer: obex.Bearer
      if use_l2cap:
        if not sdp_info.goep_l2cap_psm:
          self.fail('Failed to find L2CAP PSM for OPP.')
        self.logger.info('[REF] Connect to OPP over L2CAP.')
        bearer = await ref_dut_acl.create_l2cap_channel(
            l2cap.ClassicChannelSpec(
                psm=sdp_info.goep_l2cap_psm,
                mode=l2cap.TransmissionMode.ENHANCED_RETRANSMISSION,
                fcs_enabled=True,
            )
        )
      else:
        self.logger.info('[REF] Connect to OPP.')
        rfcomm_client = await rfcomm.Client(ref_dut_acl).start()
        bearer = await rfcomm_client.open_dlc(sdp_info.rfcomm_channel)
      return opp.Client(bearer)

  @retry.retry_on_exception()
  async def _select_target_device(self) -> None:
    """Selects the target device in DevicePickerActivity."""
    ref_name = self.ref.device.name
    target_selector = self.dut.ui(textContains=ref_name)
    # Wait for the target device preference item to appear in the device
    # picker.
    self.assertTrue(
        await asyncio.to_thread(
            lambda: target_selector.wait.exists(timeout=_UI_TIMEOUT)
        ),
        f'Target device with name {ref_name} did not appear in device picker.',
    )

    # Wait for the UI to stabilize after window animations and list updates.
    await asyncio.to_thread(self.dut.ui.wait.idle)
    # Click the preference item and wait for the picker to be dismissed.
    self.assertTrue(
        await asyncio.to_thread(
            lambda: target_selector.click()
            and target_selector.wait.gone(timeout=_UI_TIMEOUT)
        ),
        f'Failed to select target device with name {ref_name}.',
    )

  @navi_test_base.named_parameterized(
      rfcomm=False,
      l2cap=True,
  )
  async def test_outbound_single_file(self, use_l2cap: bool) -> None:
    """Tests sending a single file from DUT to REF.

    Test steps:
      1. Generate a test file on DUT.
      2. Set a random alias to avoid collision with other tests.
      3. Send a sharing file intent from DUT.
      4. Select the target device on DUT.
      5. Wait for OPP connection on REF.
      6. Wait for file transfer to complete on REF.
      7. Check the received file on REF.

    Args:
      use_l2cap: Whether to use L2CAP for OPP connection.
    """
    self.ref.device.sdp_service_records = {
        _OPP_SERVICE_RECORD_HANDLE: opp.make_sdp_records(
            opp.SdpInfo(
                service_record_handle=_OPP_SERVICE_RECORD_HANDLE,
                rfcomm_channel=self.ref_opp_server.rfcomm_channel,
                profile_version=opp.Version.V_1_2,
                goep_l2cap_psm=(
                    self.ref_opp_server.l2cap_server.psm if use_l2cap else None
                ),
            )
        )
    }

    user_id = self.dut.adb.current_user_id
    # [DUT] Generate a test file.
    with tempfile.NamedTemporaryFile(
        mode='wb',
        # On Windows, NamedTemporaryFile cannot be deleted if used multiple
        # times.
        delete=(sys.platform != 'win32'),
    ) as temp_file:
      temp_file.write(_TEST_DATA)
      temp_file.flush()
      self.dut.adb.push(
          [temp_file.name, f'/data/media/{user_id}/opp_test_file.jpg']
      )

    self.logger.info('[DUT] Send sharing file intent.')
    # The file path is different here:
    #  - /storage/ is accessible for Android apps.
    #  - /data/media/ is accessible for adb.
    self.dut.bt.oppShareFiles(
        ['/storage/self/primary/opp_test_file.jpg'], _TEST_FILE_MIME_TYPE
    )

    self.logger.info('[DUT] Select the target device')
    # After receiving the sharing file intent, OPP service will pop a Device
    # Selector Activity, showing all available devices with their alias names.
    await self._select_target_device()

    self.logger.info('[REF] Wait for OPP connection.')
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      opp_server_connection = await self.ref_opp_server.wait_connection()

    self.logger.info('[REF] Wait file transfer to complete.')
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      received_file = await opp_server_connection.completed_sessions.get()

    # [REF] Check the received file.
    self.assertEqual(received_file.name, 'opp_test_file.jpg')
    self.assertStartsWith(received_file.file_type, _TEST_FILE_MIME_TYPE)
    self.assertEqual(received_file.body, _TEST_DATA)

  @navi_test_base.named_parameterized(
      rfcomm=False,
      l2cap=True,
  )
  async def test_outbound_multiple_files(self, use_l2cap: bool) -> None:
    """Tests sending multiple files from DUT to REF.

    Test steps:
      1. Generate test files on DUT.
      2. Set a random alias to avoid collision with other tests.
      3. Send a sharing files intent from DUT.
      4. Select the target device on DUT.
      5. Wait for OPP connection on REF.
      6. Wait for file transfers to complete on REF.
      7. Check the received files on REF.

    Args:
      use_l2cap: Whether to use L2CAP for OPP connection.
    """
    self.ref.device.sdp_service_records = {
        _OPP_SERVICE_RECORD_HANDLE: opp.make_sdp_records(
            opp.SdpInfo(
                service_record_handle=_OPP_SERVICE_RECORD_HANDLE,
                rfcomm_channel=self.ref_opp_server.rfcomm_channel,
                profile_version=opp.Version.V_1_2,
                goep_l2cap_psm=(
                    self.ref_opp_server.l2cap_server.psm if use_l2cap else None
                ),
            )
        )
    }

    user_id = self.dut.adb.current_user_id
    file_names = ['opp_test_file_1.jpg', 'opp_test_file_2.jpg']
    dut_file_paths: list[str] = []

    # [DUT] Generate test files.
    for name in file_names:
      with tempfile.NamedTemporaryFile(
          mode='wb',
          delete=(sys.platform != 'win32'),
      ) as temp_file:
        temp_file.write(_TEST_DATA)
        temp_file.flush()
        self.dut.adb.push([temp_file.name, f'/data/media/{user_id}/{name}'])
        dut_file_paths.append(f'/storage/self/primary/{name}')

    self.logger.info('[DUT] Send sharing files intent.')
    self.dut.bt.oppShareFiles(dut_file_paths, _TEST_FILE_MIME_TYPE)

    self.logger.info('[DUT] Select the target device')
    await self._select_target_device()

    self.logger.info('[REF] Wait for OPP connection.')
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      opp_server_connection = await self.ref_opp_server.wait_connection()

    self.logger.info('[REF] Wait file transfers to complete.')
    received_files: list[opp.TransferSession] = []
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      for _ in file_names:
        received_file = await opp_server_connection.completed_sessions.get()
        received_files.append(received_file)

    # [REF] Check the received files.
    received_names = [f.name for f in received_files]
    self.assertCountEqual(received_names, file_names)
    for received_file in received_files:
      self.assertStartsWith(received_file.file_type, _TEST_FILE_MIME_TYPE)
      self.assertEqual(received_file.body, _TEST_DATA)

  @navi_test_base.named_parameterized(
      rfcomm=False,
      l2cap=True,
  )
  async def test_inbound_single_file(self, use_l2cap: bool) -> None:
    """Tests sending a single file from REF to DUT.

    Test steps:
      1. Connect ACL to DUT.
      2. Find SDP record for OPP.
      3. Connect OPP to DUT.
      4. Start file transfer from REF.
      5. Accept file transfer on DUT via ADB.
      6. Wait for file transfer to complete on REF.

    Args:
      use_l2cap: Whether to use L2CAP for OPP connection.
    """
    user_id = self.dut.adb.current_user_id
    file_name = f'opp_test_file_{uuid.uuid4().hex[:8]}.jpg'
    file_name_pattern_android = (
        f'/data/media/{user_id}/Download/opp_test_file*.jpg'
    )
    # Make sure there isn't any similar file on DUT.
    with contextlib.suppress(adb.AdbError):
      self.dut.shell(
          f'test -f {file_name_pattern_android} && '
          f'rm {file_name_pattern_android}'
      )

    opp_client = await self._make_opp_client_from_ref(use_l2cap)
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      await opp_client.connect(count=1)

    self.logger.info('[REF] Start file transfer.')
    transfer_task = asyncio.create_task(
        opp_client.transmit_file(
            file_name=file_name,
            file_content=_TEST_DATA,
            file_type=_TEST_FILE_MIME_TYPE,
        )
    )
    self.logger.info('[DUT] Wait for incoming file event.')
    share_id = await self._wait_for_incoming_share_id()
    self.logger.info('[DUT] Accept incoming file share: %s', share_id)
    self._answer_incoming_file(share_id, accept=True)

    self.logger.info('[REF] Wait file transfer to complete.')
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      await transfer_task

    @retry.retry_on_exception()
    def check_file_on_dut() -> None:
      # Android always generate a new file name with timestamp, so we need to
      # find the file with the same prefix.
      dut_file_path = self.dut.shell(['ls', file_name_pattern_android])
      with tempfile.TemporaryDirectory() as temp_dir:
        self.dut.adb.pull([dut_file_path, temp_dir])
        with open(
            pathlib.Path(temp_dir, pathlib.Path(dut_file_path).name), 'rb'
        ) as f:
          self.assertEqual(f.read(), _TEST_DATA)

    check_file_on_dut()

  @navi_test_base.named_parameterized(
      rfcomm=False,
      l2cap=True,
  )
  async def test_inbound_transfer_reject(self, use_l2cap: bool) -> None:
    """Tests sending files from REF to DUT and reject the transfer on DUT.

    Test steps:
      1. Connect ACL to DUT.
      2. Find SDP record for OPP.
      3. Connect OPP to DUT.
      4. Start file transfer from REF.
      5. Reject file transfer on DUT via ADB.
      6. Wait for file transfer to complete on REF.

    Args:
      use_l2cap: Whether to use L2CAP for OPP connection.
    """
    file_name = f'opp_test_file_{uuid.uuid4().hex[:8]}.jpg'

    opp_client = await self._make_opp_client_from_ref(use_l2cap)
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      await opp_client.connect(count=1)

    self.logger.info('[REF] Start file transfer.')
    transfer_task = asyncio.create_task(
        opp_client.transmit_file(
            file_name=file_name,
            file_content=_TEST_DATA,
            file_type=_TEST_FILE_MIME_TYPE,
        )
    )
    self.logger.info('[DUT] Wait for incoming file event.')
    share_id = await self._wait_for_incoming_share_id()
    self.logger.info('[DUT] Reject incoming file share: %s', share_id)
    self._answer_incoming_file(share_id, accept=False)

    self.logger.info('[REF] Wait file transfer to complete.')
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      with self.assertRaises((opp.OppError, obex.ObexConnectionError)) as e:
        await transfer_task
      if isinstance(e.exception, opp.OppError):
        self.assertEqual(e.exception.error_code, obex.ResponseCode.FORBIDDEN)
      else:
        self.logger.info('Connection closed by peer.')


if __name__ == '__main__':
  test_runner.main()
