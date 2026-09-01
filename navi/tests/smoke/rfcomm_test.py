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
import contextlib
import datetime
import enum
import uuid

from bumble import core
from bumble import device
from bumble import hci
from bumble import pairing
from bumble import rfcomm
from bumble import smp
from mobly import test_runner
from mobly import records
from typing_extensions import override

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import errors

_DEFAULT_STEP_TIMEOUT_SECONDS = 15.0
_DEFAULT_TEST_TIMEOUT_SECONDS = 15.0
_TRANSMISSION_TIMEOUT_SECONDS = 180.0
_RFCOMM_SERVICE_RECORD_HANDLE = 1
_RFCOMM_UUID = "130c8436-15ac-4d08-aa60-595af4547e8d"
_TEST_DATA = bytes(i % 256 for i in range(10000))


@enum.unique
class _Variant(enum.Enum):
  SECURE = "secure"
  INSECURE = "insecure"


_PairingDelegate = pairing.PairingDelegate


class RfcommTest(navi_test_base.TwoDevicesTestBase):

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()

    # Using highest authentication level to allow secure sockets.
    def pairing_config_factory(
        connection: device.Connection,
    ) -> pairing.PairingConfig:
      del connection  # Unused parameter.
      return pairing.PairingConfig(
          delegate=_PairingDelegate(
              io_capability=(
                  _PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT
              )
          )
      )

    self.ref.device.pairing_config_factory = pairing_config_factory
    # Disable CTKD.
    self.ref.device.l2cap_channel_manager.deregister_fixed_channel(
        smp.SMP_BR_CID
    )
    # Clear SDP records.
    self.ref.device.sdp_service_records.clear()

  @override
  def on_fail(self, record: records.TestResultRecord):
    super().on_fail(record)
    self.dut.reload_snippet()

  async def _setup_pairing(self) -> None:
    ref_dut_acl = await self.classic_connect_and_pair()

    # Terminate ACL connection after pairing.
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      # Disconnection may "fail" if the ACL is already disconnecting or
      # disconnected.
      with contextlib.suppress(core.BaseBumbleError):
        await ref_dut_acl.disconnect()
      self.logger.info("[DUT] Wait for disconnected.")
      await dut_cb.wait_for_event(bl4a_api.AclDisconnected)

    # Wait for 2 seconds to let controllers become idle.
    await asyncio.sleep(datetime.timedelta(seconds=2).total_seconds())

  async def _transmission_test(
      self,
      ref_dut_dlc: rfcomm.DLC,
      dut_ref_dlc: bl4a_api.RfcommSocket,
  ) -> None:
    """Tests transmissting data between DUT and REF over RFCOMM.

    (Not a standalone test.)

    Args:
      ref_dut_dlc: DLC instance of REF, connected to DUT.
      dut_ref_dlc: DLC token of DUT, connected to REF.
    """
    # Store received SDUs in queue.
    ref_sdu_rx_queue = asyncio.Queue[bytes]()
    ref_dut_dlc.sink = ref_sdu_rx_queue.put_nowait

    self.logger.info("Start sending data from REF to DUT")
    async with self.assert_not_timeout(_TRANSMISSION_TIMEOUT_SECONDS):
      ref_dut_dlc.write(_TEST_DATA)
      data_read = await dut_ref_dlc.read(len(_TEST_DATA))
      self.assertEqual(data_read, _TEST_DATA)

    async def ref_rx_task() -> bytearray:
      data_read = bytearray()
      while len(data_read) < len(_TEST_DATA):
        data_read += await ref_sdu_rx_queue.get()
      return data_read

    self.logger.info("Start sending data from DUT to REF")
    async with self.assert_not_timeout(_TRANSMISSION_TIMEOUT_SECONDS):
      data_read, _ = await asyncio.gather(
          ref_rx_task(),
          dut_ref_dlc.write(_TEST_DATA),
      )
      self.assertEqual(data_read, _TEST_DATA)

  @navi_test_base.parameterized(_Variant.SECURE, _Variant.INSECURE)
  async def test_incoming_connection(self, variant: _Variant) -> None:
    """Tests RFCOMM incoming connection, read and write.

    Typical duration: 30-60s.

    Test steps:
      1. Open RFCOMM server on DUT.
      2. Connect ACL from REF to DUT.
      3. Connect RFCOMM from REF to DUT.
      4. Transmit SDU from REF to DUT.
      5. Transmit SDU from DUT to REF.
      6. Disconnect RFCOMM from REF.

    Args:
      variant: Whether Secure API is used. (They have the same behavior for
        Bluetooth device in version >=2.1)
    """
    await self._setup_pairing()

    self.logger.info("[DUT] Listen RFCOMM.")
    rfcomm_uuid = str(uuid.uuid4())
    with self.dut.bl4a.create_rfcomm_server(
        rfcomm_uuid,
        secure=variant == _Variant.SECURE,
    ) as server:
      self.logger.info("[REF] Connect to DUT.")
      ref_dut_acl = await self.ref.device.connect(
          str(self.dut.address),
          transport=core.BT_BR_EDR_TRANSPORT,
      )
      await ref_dut_acl.authenticate()
      await ref_dut_acl.encrypt(True)

      self.logger.info("[REF] Find RFCOMM channel.")
      channel = await rfcomm.find_rfcomm_channel_with_uuid(
          ref_dut_acl, rfcomm_uuid
      )
      if not channel:
        self.fail("Failed to find RFCOMM channel with UUID.")

      self.logger.info("[REF] Connect RFCOMM channel to DUT.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_rfcomm = await rfcomm.Client(ref_dut_acl).start()

      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_dut_dlc, dut_ref_dlc = await asyncio.gather(
            ref_rfcomm.open_dlc(channel),
            server.accept(),
        )

      await self._transmission_test(ref_dut_dlc, dut_ref_dlc)

      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.logger.info("[REF] Disconnect RFCOMM channel.")
        await ref_dut_dlc.disconnect()

  @navi_test_base.parameterized(_Variant.SECURE, _Variant.INSECURE)
  async def test_outgoing_connection(self, variant: _Variant) -> None:
    """Tests RFCOMM outgoing connection, read and write.

    Typical duration: 30-60s.

    Test steps:
      1. Open RFCOMM server on REF.
      2. Connect RFCOMM from REF to DUT.
      3. Transmit SDU from REF to DUT.
      4. Transmit SDU from DUT to REF.
      5. Disconnect RFCOMM from DUT.

    Args:
      variant: Whether Secure API is used. (They have the same behavior for
        Bluetooth device in version >=2.1)
    """
    await self._setup_pairing()

    ref_accept_future = asyncio.get_running_loop().create_future()
    channel = rfcomm.Server(self.ref.device).listen(
        acceptor=ref_accept_future.set_result
    )
    self.ref.device.sdp_service_records[_RFCOMM_SERVICE_RECORD_HANDLE] = (
        rfcomm.make_service_sdp_records(
            service_record_handle=_RFCOMM_SERVICE_RECORD_HANDLE,
            channel=channel,
            uuid=core.UUID(_RFCOMM_UUID),
        )
    )

    self.logger.info("[DUT] Connect RFCOMM channel to REF.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_dut_dlc, dut_ref_dlc = await asyncio.gather(
          ref_accept_future,
          self.dut.bl4a.create_rfcomm_channel(
              address=self.ref.address,
              secure=variant == _Variant.SECURE,
              uuid=_RFCOMM_UUID,
          ),
      )

    await self._transmission_test(ref_dut_dlc, dut_ref_dlc)

    self.logger.info("[DUT] Disconnect RFCOMM channel.")
    await dut_ref_dlc.close()

  async def test_outgoing_connection_rejected(self) -> None:
    """Tests RFCOMM outgoing connection to a non-registered UUID, should be rejected.

    Test steps:
      1. Setup pairing.
      2. Connect RFCOMM from DUT to REF with an unregistered UUID.
    """
    await self._setup_pairing()

    self.logger.info("[DUT] Connect RFCOMM channel to REF.")
    with self.assertRaises(errors.ConnectionError):
      await self.dut.bl4a.create_rfcomm_channel(
          address=self.ref.address,
          secure=True,
          uuid=_RFCOMM_UUID,
          retry_count=0,
      )

  async def test_incoming_connection_rejected(self) -> None:
    """Tests RFCOMM incoming connection to a non-registered channel number, should be rejected.

    Test steps:
      1. Setup pairing.
      2. Connect ACL from REF to DUT.
      3. Connect RFCOMM from REF to DUT with an unregistered channel number.
    """
    await self._setup_pairing()
    async with self.assert_not_timeout(_DEFAULT_TEST_TIMEOUT_SECONDS):
      self.logger.info("[REF] Connect to DUT.")
      ref_dut_acl = await self.ref.device.connect(
          str(self.dut.address),
          transport=core.BT_BR_EDR_TRANSPORT,
      )
      await ref_dut_acl.authenticate()
      await ref_dut_acl.encrypt(True)

      self.logger.info("[REF] Find an unregistered RFCOMM channel.")
      registered_channels = await rfcomm.find_rfcomm_channels(ref_dut_acl)
      unregistered_channel = next(
          (
              channel
              for channel in range(
                  rfcomm.RFCOMM_DYNAMIC_CHANNEL_NUMBER_START,
                  rfcomm.RFCOMM_DYNAMIC_CHANNEL_NUMBER_END + 1,
              )
              if channel not in registered_channels
          ),
          None,
      )
      if not unregistered_channel:
        self.skipTest("Failed to find an unregistered RFCOMM channel.")

      self.logger.info("[REF] Connect RFCOMM channel to DUT.")
      ref_rfcomm = await rfcomm.Client(ref_dut_acl).start()
      with self.assertRaises(core.ConnectionError):
        await ref_rfcomm.open_dlc(unregistered_channel)

  # TODO: Remove this skip when the flag is removed.
  @navi_test_base.TwoDevicesTestBase.require_flag(
      "com.android.bluetooth.flags.add_force_disconnect_api"
  )
  async def test_rfcomm_disconnect_trigger_acl_disconnected(self) -> None:
    """Tests if BluetoothDevice.disconnect() triggers ACL_DISCONNECTED.

    Test Steps:
      1. DUT and REF pair and connect.
      2. REF listens for RFCOMM connection.
      3. DUT connects to REF via RFCOMM.
      4. DUT triggers BluetoothDevice.disconnect().
      5. Verify DUT receives ACTION_ACL_DISCONNECTED intent.
    """
    self.logger.info("[DUT] Pair and connect with REF.")
    await self.classic_connect_and_pair()

    # Step 2: REF listens for RFCOMM connection.
    self.logger.info("[REF] Listen for RFCOMM connection.")
    ref_accept_future = asyncio.get_running_loop().create_future()
    channel = rfcomm.Server(self.ref.device).listen(
        acceptor=ref_accept_future.set_result
    )
    self.ref.device.sdp_service_records[_RFCOMM_SERVICE_RECORD_HANDLE] = (
        rfcomm.make_service_sdp_records(
            service_record_handle=_RFCOMM_SERVICE_RECORD_HANDLE,
            channel=channel,
            uuid=core.UUID(_RFCOMM_UUID),
        )
    )

    # Step 3: DUT connects to REF via RFCOMM.
    self.logger.info("[DUT] Connect RFCOMM to REF.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      _, dut_ref_dlc = await asyncio.gather(
          ref_accept_future,
          self.dut.bl4a.create_rfcomm_channel(
              address=self.ref.address,
              secure=True,
              uuid=_RFCOMM_UUID,
          ),
      )

    # Step 4: Register callback for ACL_DISCONNECTED and trigger disconnect.
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      self.logger.info("[DUT] Trigger BluetoothDevice.disconnectAcl().")
      self.dut.bt.disconnectAcl(
          self.ref.address, android_constants.Transport.AUTO
      )

      # Step 5: Verify DUT receives ACTION_ACL_DISCONNECTED intent.
      self.logger.info("[DUT] Wait for ACL_DISCONNECTED event.")
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected(
              address=self.ref.address,
              transport=android_constants.Transport.CLASSIC,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

    # Cleanup: close RFCOMM if still open (it should be closed by disconnect).
    await dut_ref_dlc.close()

  @navi_test_base.retry(max_count=2)
  async def test_sniff_mode(self) -> None:
    """Test Sniff mode transitions.

    Test steps:
      1. Enable Sniff mode on REF.
      2. Pair and connect Classic ACL. Verify it is in Active mode initially.
      3. Establish RFCOMM connection to keep ACL alive.
      4. Verify link automatically transitions to Sniff mode due to inactivity.
      5. Send data from DUT to REF (TX for DUT), verify rapid transition to
        Active mode.
      6. Verify link automatically re-enters Sniff mode after TX idle.
      7. Send data from REF to DUT (RX for DUT), verify rapid transition to
        Active mode.
      8. Verify link automatically re-enters Sniff mode after RX idle.
      9. Verify link stays in Sniff mode when idle.
      10. Disconnect RFCOMM.
    """
    if self.dut.device.is_emulator:
      self.skipTest("Sniff mode is not supported by Rootcanal.")

    # 1. Enable Sniff mode on REF.
    await self.ref.device.send_sync_command(
        hci.HCI_Write_Default_Link_Policy_Settings_Command(
            default_link_policy_settings=1 << 2,  # ENABLE_SNIFF_MODE.
        )
    )

    ref_accept_future = asyncio.get_running_loop().create_future()
    channel = rfcomm.Server(self.ref.device).listen(
        acceptor=ref_accept_future.set_result
    )
    self.ref.device.sdp_service_records[_RFCOMM_SERVICE_RECORD_HANDLE] = (
        rfcomm.make_service_sdp_records(
            service_record_handle=_RFCOMM_SERVICE_RECORD_HANDLE,
            channel=channel,
            uuid=core.UUID(_RFCOMM_UUID),
        )
    )

    # 2. Pair and connect Classic ACL.
    self.logger.info("Pairing and connecting Classic ACL...")
    ref_dut_acl = await self.classic_connect_and_pair()
    self.assertEqual(
        ref_dut_acl.classic_mode,
        hci.HCI_Mode_Change_Event.Mode.ACTIVE,
        "Link must be in Active Mode initially after pairing.",
    )

    # 3. Establish RFCOMM connection.
    self.logger.info("[DUT] Connecting RFCOMM...")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_dut_dlc, dut_ref_dlc = await asyncio.gather(
          ref_accept_future,
          self.dut.bl4a.create_rfcomm_channel(
              address=self.ref.address,
              secure=True,
              uuid=_RFCOMM_UUID,
          ),
      )
    self.test_case_context.push_async_exit(dut_ref_dlc)

    self.logger.info("RFCOMM connection established.")

    # TODO: This is likely a bug.
    # Trigger a write to initialize the BTA JV PM state machine.
    # If we don't write anything, the idle timer might not start because
    # PORT_EV_TXEMPTY is only triggered after some TX activity.
    self.logger.info("Sending byte to initialize PM state...")
    await dut_ref_dlc.write(b"I")

    # Helper to wait for mode change using asyncio.Condition.
    mode_condition = asyncio.Condition()

    @ref_dut_acl.on(ref_dut_acl.EVENT_MODE_CHANGE)
    async def on_mode_change() -> None:
      async with mode_condition:
        self.logger.info(
            "[REF] Mode change event: mode=%s, interval=%s",
            ref_dut_acl.classic_mode,
            ref_dut_acl.classic_interval,
        )
        mode_condition.notify_all()

    async def wait_for_mode(
        target_mode: hci.HCI_Mode_Change_Event.Mode,
    ) -> None:
      async with mode_condition:
        async with self.assert_not_timeout(15.0):
          await mode_condition.wait_for(
              lambda: ref_dut_acl.classic_mode == target_mode
          )

    # 4. Verify link automatically transitions to Sniff mode due to inactivity.
    self.logger.info(
        "[REF] Waiting for link to automatically transition to Sniff mode..."
    )
    # Default idle timeout is ~8s (X=8), so 12s timeout is safe.
    with self.subTest(name="automatic sniff mode transition due to inactivity"):
      await wait_for_mode(hci.HCI_Mode_Change_Event.Mode.SNIFF)
      self.logger.info("Link automatically entered Sniff mode.")

    # 5. Send data from DUT to REF (TX for DUT), verify rapid transition to
    # Active mode.
    self.logger.info("[DUT] Sending data to trigger exit from Sniff (TX)...")
    await dut_ref_dlc.write(_TEST_DATA)

    # Verify rapid transition to Active (Y = 500ms).
    with self.subTest(name="exit sniff mode on TX"):
      await wait_for_mode(hci.HCI_Mode_Change_Event.Mode.ACTIVE)
      self.logger.info(
          "[REF] Link transitioned to Active mode rapidly after TX."
      )

    # 6. Verify link automatically re-enters Sniff mode after TX idle.
    self.logger.info(
        "[REF] Waiting for link to automatically re-enter Sniff mode after TX"
        " idle..."
    )
    with self.subTest(name="re-enter sniff mode after TX idle"):
      await wait_for_mode(hci.HCI_Mode_Change_Event.Mode.SNIFF)
      self.logger.info(
          "Link automatically re-entered Sniff mode after TX idle."
      )

    # 7. Send data from REF to DUT (RX for DUT), verify rapid transition to
    # Active mode if exit_sniff is enabled.
    exit_sniff_prop = self.dut.getprop(
        android_constants.Property.RFCOMM_SYSPROXY_RX_EXIT_SNIFF
    )
    self.logger.info(
        "%s prop value: %s",
        android_constants.Property.RFCOMM_SYSPROXY_RX_EXIT_SNIFF,
        exit_sniff_prop,
    )

    if exit_sniff_prop == "true":
      with self.subTest(name="exit sniff mode on RX"):
        self.logger.info(
            "[REF] Sending data to trigger exit from Sniff (RX)..."
        )
        ref_dut_dlc.write(_TEST_DATA)
        # Read data on DUT to prevent congestion.
        await dut_ref_dlc.read(len(_TEST_DATA))

        # Verify rapid transition to Active (Y = 500ms).
        with self.subTest(name="exit sniff mode on RX"):
          await wait_for_mode(hci.HCI_Mode_Change_Event.Mode.ACTIVE)
          self.logger.info("Link transitioned to Active mode rapidly after RX.")

        # 8. Verify link automatically re-enters Sniff mode after RX idle.
        self.logger.info(
            "Waiting for link to automatically re-enter Sniff mode after RX"
            " idle..."
        )
        with self.subTest(name="re-enter sniff mode after RX idle"):
          await wait_for_mode(hci.HCI_Mode_Change_Event.Mode.SNIFF)
          self.logger.info(
              "Link automatically re-entered Sniff mode after RX idle."
          )

    # 9. Verify link stays in Sniff mode when idle.
    self.logger.info("Verifying link stays in Sniff mode...")
    await asyncio.sleep(3.0)

    with self.subTest(name="stay in Sniff mode when idle"):
      self.assertEqual(
          ref_dut_acl.classic_mode,
          hci.HCI_Mode_Change_Event.Mode.SNIFF,
          "Link must stay in Sniff Mode when idle.",
      )
      self.logger.info("Link stayed in Sniff mode.")

    # TODO: Re-enable once the bug is fixed.
    # with self.subTest(name="re-enter sniff mode after mode change to active"):
    #   self.logger.info("Sending exit sniff mode command...")
    #   await self.ref.device.send_async_command(
    #       hci.HCI_Exit_Sniff_Mode_Command(ref_dut_acl.handle)
    #   )
    #   self.logger.info("Waiting for link to transition to Active mode...")
    #   await wait_for_mode(hci.HCI_Mode_Change_Event.Mode.ACTIVE)
    #   self.logger.info(
    #       "Waiting for link to automatically re-enter Sniff mode issued by"
    #       " DUT."
    #   )
    #   await wait_for_mode(hci.HCI_Mode_Change_Event.Mode.SNIFF)


if __name__ == "__main__":
  test_runner.main()
