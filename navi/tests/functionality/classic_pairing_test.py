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

from __future__ import annotations

import asyncio

from bumble import core
from bumble import device
from bumble import hci
from bumble import pairing

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import pairing as pairing_utils


_TERMINATED_BOND_STATES = (
    android_constants.BondState.BONDED,
    android_constants.BondState.NONE,
)
_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_PIN_CODE_DEFAULT = '834701'


class ClassicPairingTest(navi_test_base.TwoDevicesTestBase):
  """Tests related to Bluetooth Classic pairing."""

  async def test_legacy_pairing_incoming_mode3(self) -> None:
    """Tests incoming Legacy Pairing in mode 3.

    Test steps:
      1. Enable always Authentication and disable build-in pairing delegation on
      REF.
      2. Connect DUT from REF.
      3. Wait for pairing requests on REF.
      4. Set pairing PIN on REF.
      5. Wait for pairing requests on DUT.
      6. Set pairing PIN on DUT.
      7. Verify final states.
    """

    pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=pairing.PairingDelegate.IoCapability.KEYBOARD_INPUT_ONLY,
        auto_accept=False,
    )

    def pairing_config_factory(
        connection: device.Connection,
    ) -> pairing.PairingConfig:
      del connection  # Unused callback parameter.
      return pairing.PairingConfig(delegate=pairing_delegate)

    self.ref.device.pairing_config_factory = pairing_config_factory

    dut_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(dut_cb)
    self.logger.info('[REF] Disable SSP on REF.')
    await self.ref.device.send_command(
        hci.HCI_Write_Simple_Pairing_Mode_Command(simple_pairing_mode=0),
        check_result=True,
    )
    self.logger.info('[REF] Enable always authenticate on REF.')
    await self.ref.device.send_command(
        hci.HCI_Write_Authentication_Enable_Command(authentication_enable=1),
        check_result=True,
    )

    self.logger.info('[REF] Connect to DUT.')
    ref_connection_task = asyncio.create_task(
        self.ref.device.connect(
            self.dut.address,
            transport=core.BT_BR_EDR_TRANSPORT,
        )
    )

    self.logger.info('[REF] Wait for pairing request.')
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_pairing_request = await pairing_delegate.pairing_events.get()
      self.assertEqual(
          ref_pairing_request.variant,
          pairing_utils.PairingVariant.PIN_CODE_REQUEST,
      )

    self.logger.info('[REF] Handle pairing confirmation.')
    pairing_delegate.pairing_answers.put_nowait(_PIN_CODE_DEFAULT)

    self.logger.info('[DUT] Wait for pairing request.')
    dut_pairing_request = await dut_cb.wait_for_event(
        bl4a_api.PairingRequest,
        predicate=lambda e: (e.address == self.ref.address),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.assertEqual(
        dut_pairing_request.variant, android_constants.PairingVariant.PIN
    )

    self.logger.info('[DUT] Handle pairing confirmation.')
    self.dut.bt.setPin(self.ref.address, _PIN_CODE_DEFAULT)

    self.logger.info('[DUT] Check final state.')
    dut_pairing_complete = await dut_cb.wait_for_event(
        bl4a_api.BondStateChanged,
        predicate=lambda e: (e.state in _TERMINATED_BOND_STATES),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.assertEqual(
        dut_pairing_complete.state, android_constants.BondState.BONDED
    )

    self.logger.info('[REF] Wait for connection complete.')
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await ref_connection_task

  async def test_bond_retention_after_bluetooth_restart(self) -> None:
    """Tests bond retention after Bluetooth restart on DUT.

    Test steps:
      1. Bond DUT and REF over BR/EDR.
      2. Restart Bluetooth on DUT.
      3. Verify the bond remains present on DUT.
      4. Reconnect and verify the link is encrypted.
    """
    self.logger.info('Step 1: Bond DUT and REF over BR/EDR.')
    await self.classic_connect_and_pair()
    self.assertIn(self.ref.address, self.dut.bt.getBondedDevices())

    self.logger.info('Step 2: Restart Bluetooth on DUT.')
    self.dut.bt.disable()
    self.dut.bt.waitForAdapterState(android_constants.AdapterState.OFF)
    self.dut.bt.enable()
    self.dut.bt.waitForAdapterState(android_constants.AdapterState.ON)

    self.logger.info('Step 3: Verify the bond remains present on DUT.')
    self.assertIn(self.ref.address, self.dut.bt.getBondedDevices())

    self.logger.info('Step 4: Reconnect and verify the link is encrypted.')
    self.logger.info('[REF] Reconnect BR/EDR to DUT.')
    ref_conn = await self.ref.device.connect(
        self.dut.address,
        transport=core.BT_BR_EDR_TRANSPORT,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    dut_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(dut_cb)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info('[REF] Trigger authentication.')
      await ref_conn.authenticate()
      self.logger.info('[REF] Trigger encryption.')
      await ref_conn.encrypt()

    self.logger.info('[DUT] Wait for encryption changed.')
    await dut_cb.wait_for_event(
        bl4a_api.EncryptionChanged(
            address=self.ref.address,
            transport=android_constants.Transport.CLASSIC,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.assertTrue(ref_conn.is_encrypted)
