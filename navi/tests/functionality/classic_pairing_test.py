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

from __future__ import annotations

import asyncio

from bumble import core
from bumble import device
from bumble import hci
from bumble import pairing

from navi.tests import navi_test_base
from navi.tests.smoke import pairing_utils
from navi.utils import android_constants
from navi.utils import bl4a_api


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
    self.close_after_test.append(dut_cb)
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
    pairing_delegate.pairing_answers.put_nowait(
        pairing_utils.PairingEvent(
            variant=pairing_utils.PairingVariant.PIN_CODE_REQUEST,
            arg=_PIN_CODE_DEFAULT,
        )
    )

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
