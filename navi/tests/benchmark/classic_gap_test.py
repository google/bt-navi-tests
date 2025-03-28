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
import statistics

from bumble import core
from bumble import device
from bumble import hci
from bumble import pairing
from bumble import smp
from mobly import test_runner

from navi.tests import navi_test_base
from navi.tests.benchmark import performance_tool
from navi.tests.smoke import pairing_utils
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants


_TERMINATED_BOND_STATES = (
    android_constants.BondState.BONDED,
    android_constants.BondState.NONE,
)
_MAJOR_CASE_DEFAULT_REPEAT_TIMES = 50
_MINOR_CASE_DEFAULT_REPEAT_TIMES = 5
_DEFAULT_REPEAT_TIMES = 50
_DEFAULT_STEP_TIMEOUT_SECONDS = 5.0
_Direction = constants.Direction
_Role = constants.HciRole
_IoCapability = pairing.PairingDelegate.IoCapability
_Callback = bl4a_api.CallbackHandler


class ClassicGapTest(navi_test_base.TwoDevicesTestBase):
  pairing_delegate: pairing_utils.PairingDelegate

  async def test_incoming_connection(self) -> None:
    """Tests RFCOMM incoming connection.

    Test steps:
      1. REF sends connection request to DUT.
      2. DUT accepts the connection.
      3. REF terminates the connection.
      4. Repeat step 1-3 for 50 times.
    """
    success_count = 0
    latency_list = list[float]()
    for i in range(_DEFAULT_REPEAT_TIMES):
      try:
        with performance_tool.Stopwatch() as stop_watch:
          self.logger.info("[REF] Connect to DUT.")
          await self.ref.device.connect(
              self.dut.address,
              transport=core.BT_BR_EDR_TRANSPORT,
          )
          self.logger.info("[REF] Connected to DUT.")

        latency_seconds = stop_watch.elapsed_time.total_seconds()
        self.logger.info(
            "Success connection in %.2f seconds", latency_seconds
        )
        self.logger.info("Test %d Success", i + 1)
        latency_list.append(latency_seconds)
        success_count += 1
      except (core.BaseBumbleError, AssertionError):
        self.logger.exception("Failed to make ACL connection")
      finally:
        await performance_tool.cleanup_connections(self.dut, self.ref)
    self.logger.info(
        "[success rate] Passes: %d / Attempts: %d",
        success_count,
        _DEFAULT_REPEAT_TIMES,
    )
    self.logger.info(
        "[connection time] avg: %.2f, min: %.2f, max: %.2f, stdev: %.2f",
        statistics.mean(latency_list),
        min(latency_list),
        max(latency_list),
        statistics.stdev(latency_list),
    )
    self.record_data(
        navi_test_base.RecordData(
            test_name=self.current_test_info.name,
            properties={
                "passes": success_count,
                "attempts": _DEFAULT_REPEAT_TIMES,
                "avg_latency": statistics.mean(latency_list),
                "min_latency": min(latency_list),
                "max_latency": max(latency_list),
                "stdev_latency": statistics.stdev(latency_list),
            },
        )
    )

  async def _test_ssp_pairing_async(
      self,
      pairing_direction: _Direction,
      ref_io_capability: _IoCapability,
      ref_role: _Role,
  ) -> float:
    """Tests Classic SSP pairing.

    Test steps:
    1. Setup configurations.
    2. Make ACL connections.
    3. Start pairing.
    4. Wait for pairing requests and verify pins.
    5. Make actions corresponding to variants.
    6. Verify final states.

    Args:
      pairing_direction: Direction of pairing.
      ref_io_capability: IO Capability on the REF device.
      ref_role: Role of the REF device.

    Returns:
      The latency of the pairing process.
    """

    pairing_delegate = self.pairing_delegate

    def pairing_config_factory(
        _: device.Connection,
    ) -> pairing.PairingConfig:
      return pairing.PairingConfig(
          sc=True,
          mitm=True,
          bonding=True,
          identity_address_type=pairing.PairingConfig.AddressType.PUBLIC,
          delegate=pairing_delegate,
      )

    self.ref.device.pairing_config_factory = pairing_config_factory

    self.logger.info("[REF] Allow role switch")
    await self.ref.device.send_command(
        hci.HCI_Write_Default_Link_Policy_Settings_Command(
            default_link_policy_settings=0x01
        ),
        check_result=True,
    )
    dut_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.close_after_test.append(dut_cb)
    ref_addr = str(self.ref.address)
    begin = datetime.datetime.now()
    auth_task: asyncio.tasks.Task = None
    if pairing_direction == _Direction.OUTGOING:
      self.logger.info("[REF] Prepare to accept connection.")
      ref_accept_task = asyncio.tasks.create_task(
          self.ref.device.accept(
              f"{self.dut.address}/P",
              role=ref_role,
              timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
          )
      )
      self.logger.info("[DUT] Create bond and connect implicitly.")
      self.assertTrue(
          self.dut.bt.createBond(ref_addr, android_constants.Transport.CLASSIC)
      )
      self.logger.info("[REF] Accept connection")
      await ref_accept_task
    else:
      self.logger.info("[REF] Connect to DUT.")
      ref_dut = await self.ref.device.connect(
          f"{self.dut.address}/P",
          transport=core.BT_BR_EDR_TRANSPORT,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )
      self.logger.info("[REF] Create bond.")
      auth_task = asyncio.tasks.create_task(ref_dut.authenticate())
      self.logger.info("[DUT] Wait for incoming connection.")
      await dut_cb.wait_for_event(
          callback_type=bl4a_api.AclConnected,
          predicate=lambda e: (e.address == ref_addr),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

    self.logger.info("[DUT] Wait for pairing request.")
    dut_pairing_event = await dut_cb.wait_for_event(
        callback_type=bl4a_api.PairingRequest,
        predicate=lambda e: (e.address == ref_addr),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    self.logger.info("[DUT] Check reported pairing method.")
    match ref_io_capability:
      case _IoCapability.NO_OUTPUT_NO_INPUT:
        expected_dut_pairing_variant = android_constants.PairingVariant.CONSENT
        expected_ref_pairing_variant = pairing_utils.PairingVariant.JUST_WORK
        ref_answer = pairing_utils.PairingEvent(
            pairing_utils.PairingVariant.JUST_WORK, True
        )
      case _IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT:
        expected_dut_pairing_variant = (
            android_constants.PairingVariant.PASSKEY_CONFIRMATION
        )
        expected_ref_pairing_variant = (
            pairing_utils.PairingVariant.NUMERIC_COMPARISON
        )
        ref_answer = pairing_utils.PairingEvent(
            pairing_utils.PairingVariant.NUMERIC_COMPARISON,
            dut_pairing_event.pin,
        )
      case _:
        raise ValueError(f"Unsupported IO capability: {ref_io_capability}")

    self.assertEqual(dut_pairing_event.variant, expected_dut_pairing_variant)

    self.logger.info("[REF] Wait for pairing request.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_pairing_event = await pairing_delegate.pairing_events.get()

    self.logger.info("[REF] Check reported pairing method.")
    self.assertEqual(ref_pairing_event.variant, expected_ref_pairing_variant)

    self.logger.info("[DUT] Handle pairing confirmation.")
    self.dut.bt.setPairingConfirmation(ref_addr, True)

    self.logger.info("[REF] Handle pairing confirmation.")
    pairing_delegate.pairing_answers.put_nowait(ref_answer)
    if ref_io_capability == _IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT:
      self.logger.info("[REF] Check pairing pin.")
      self.assertEqual(ref_pairing_event.arg, dut_pairing_event.pin)

    self.logger.info("[DUT] Check final state.")
    actual_state = (
        await dut_cb.wait_for_event(
            callback_type=bl4a_api.BondStateChanged,
            predicate=lambda e: (e.state in _TERMINATED_BOND_STATES),
            timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
        )
    ).state
    self.assertEqual(actual_state, android_constants.BondState.BONDED)
    if auth_task:
      self.logger.info("[REF] Wait authentication complete.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await auth_task
    latency_seconds = (datetime.datetime.now() - begin).total_seconds()
    self.logger.info("Success connection in %.2f seconds", latency_seconds)
    return latency_seconds

  @navi_test_base.named_parameterized(
      outgoing_ref_is_NOIO_PERI=(
          _Direction.OUTGOING,
          _IoCapability.NO_OUTPUT_NO_INPUT,
          _Role.PERIPHERAL,
          _MAJOR_CASE_DEFAULT_REPEAT_TIMES,
      ),
      outgoing_ref_is_YESNO_PERI=(
          _Direction.OUTGOING,
          _IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
          _Role.PERIPHERAL,
          _MINOR_CASE_DEFAULT_REPEAT_TIMES,
      ),
      incoming_ref_is_NOIO_PERI=(
          _Direction.INCOMING,
          _IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
          _Role.PERIPHERAL,
          _MINOR_CASE_DEFAULT_REPEAT_TIMES,
      ),
      incoming_ref_is_YESNO_PERI=(
          _Direction.INCOMING,
          _IoCapability.NO_OUTPUT_NO_INPUT,
          _Role.PERIPHERAL,
          _MINOR_CASE_DEFAULT_REPEAT_TIMES,
      ),
      incoming_ref_is_YESNO_CENTRAL=(
          _Direction.INCOMING,
          _IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
          _Role.CENTRAL,
          _MINOR_CASE_DEFAULT_REPEAT_TIMES,
      ),
      outgoing_ref_is_YESNO_CENTRAL=(
          _Direction.OUTGOING,
          _IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
          _Role.CENTRAL,
          _MINOR_CASE_DEFAULT_REPEAT_TIMES,
      ),
      outgoing_ref_is_NOIO_CENTRAL=(
          _Direction.OUTGOING,
          _IoCapability.NO_OUTPUT_NO_INPUT,
          _Role.CENTRAL,
          _MINOR_CASE_DEFAULT_REPEAT_TIMES,
      ),
      incoming_ref_is_NOIO_CENTRAL=(
          _Direction.INCOMING,
          _IoCapability.NO_OUTPUT_NO_INPUT,
          _Role.CENTRAL,
          _MINOR_CASE_DEFAULT_REPEAT_TIMES,
      ),
  )
  @navi_test_base.retry(max_count=2)
  async def test_pairing_ssp_only(
      self,
      pairing_direction: _Direction,
      ref_io_capability: _IoCapability,
      ref_role: _Role,
      default_repeat_times: int,
  ) -> None:
    """Tests Simple Secure Pairing.

    Test steps:
      1. Perform SSP.

    Args:
      pairing_direction: Direction of pairing.
      ref_io_capability: IO capabilities of the REF device.
      ref_role: Role of the REF device.
      default_repeat_times: Testcases repeat times.
    """
    # [REF] Disable SMP over Classic L2CAP channel.

    success_count = 0
    latency_list = []
    for _ in range(default_repeat_times):
      try:
        self.ref.device.l2cap_channel_manager.deregister_fixed_channel(
            smp.SMP_BR_CID
        )
        self.pairing_delegate = pairing_utils.PairingDelegate(
            io_capability=ref_io_capability,
            auto_accept=True,
        )
        latency_list.append(await self._test_ssp_pairing_async(
            pairing_direction=pairing_direction,
            ref_io_capability=ref_io_capability,
            ref_role=ref_role,
        ))
        success_count += 1
      except (core.BaseBumbleError, AssertionError):
        self.logger.exception("Failed to make outgoing pairing")
      finally:
        self.dut.bt.removeBond(self.ref.address)
        await performance_tool.cleanup_connections(self.dut, self.ref)

    self.logger.info(
        "[success rate] Passes: %d / Attempts: %d",
        success_count,
        default_repeat_times,
    )
    self.logger.info(
        "[connection time] avg: %.2f, min: %.2f, max: %.2f, stdev: %.2f",
        statistics.mean(latency_list),
        min(latency_list),
        max(latency_list),
        statistics.stdev(latency_list),
    )
    self.record_data(
        navi_test_base.RecordData(
            test_name=self.current_test_info.name,
            properties={
                "passes": success_count,
                "attempts": default_repeat_times,
                "avg_latency": statistics.mean(latency_list),
                "min_latency": min(latency_list),
                "max_latency": max(latency_list),
                "stdev_latency": statistics.stdev(latency_list),
            },
        )
    )

if __name__ == "__main__":
  test_runner.main()
