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
import contextlib
import datetime
import enum
import itertools

from bumble import core
from bumble import device
from bumble import hci
from bumble import keys as bumble_keys
from bumble import l2cap
from bumble import pairing
from bumble import smp
from mobly import test_runner
from typing_extensions import override

from navi.tests import navi_test_base
from navi.tests.smoke import pairing_utils
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import pyee_extensions


_TERMINATED_BOND_STATES = (
    android_constants.BondState.BONDED,
    android_constants.BondState.NONE,
)
_DEFAULT_STEP_TIMEOUT = datetime.timedelta(seconds=10)
_DEFAULT_STEP_TIMEOUT_SECONDS = _DEFAULT_STEP_TIMEOUT.total_seconds()
_COD_DEFAULT = 0x1F00
_COD_HEADSETS = 0x0404
_PIN_CODE_AUTO_PAIR = '0000'
_PIN_CODE_DEFAULT = '834701'


class TestVariant(enum.Enum):
  ACCEPT = 'accept'
  REJECT = 'reject'
  REJECTED = 'rejected'
  DISCONNECTED = 'disconnected'


_Direction = constants.Direction
_Role = constants.HciRole
_KeyDistribution = pairing.PairingDelegate.KeyDistribution
_IoCapability = pairing.PairingDelegate.IoCapability


class ClassicPairingTest(navi_test_base.TwoDevicesTestBase):
  """Tests related to Bluetooth Classic pairing."""

  pairing_delegate: pairing_utils.PairingDelegate

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()

  async def _test_ssp_pairing_async(
      self,
      variant: TestVariant,
      pairing_direction: _Direction,
      ref_io_capability: _IoCapability,
      ref_role: _Role,
  ) -> None:
    """Tests Classic SSP pairing.

    Test steps:
    1. Setup configurations.
    2. Make ACL connections.
    3. Start pairing.
    4. Wait for pairing requests and verify pins.
    5. Make actions corresponding to variants.
    6. Verify final states.

    Args:
      variant: Action to perform in the pairing procedure.
      pairing_direction: Direction of pairing. DUT->REF is outgoing, and vice
        versa.
      ref_io_capability: IO Capability on the REF device.
      ref_role: HCI role on the REF device.
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

    self.logger.info('[REF] Allow role switch')
    await self.ref.device.send_command(
        hci.HCI_Write_Default_Link_Policy_Settings_Command(
            default_link_policy_settings=0x01
        ),
        check_result=True,
    )

    dut_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.close_after_test.append(dut_cb)
    ref_addr = str(self.ref.address)

    ref_dut: device.Connection
    auth_task: asyncio.tasks.Task = None
    if pairing_direction == _Direction.OUTGOING:
      self.logger.info('[REF] Prepare to accept connection.')
      ref_accept_task = asyncio.tasks.create_task(
          self.ref.device.accept(
              f'{self.dut.address}/P',
              role=ref_role,
              timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
          )
      )
      self.logger.info('[DUT] Create bond and connect implicitly.')
      self.assertTrue(
          self.dut.bt.createBond(ref_addr, android_constants.Transport.CLASSIC)
      )
      self.logger.info('[REF] Accept connection')
      ref_dut = await ref_accept_task
    else:
      self.logger.info('[REF] Connect to DUT.')
      ref_dut = await self.ref.device.connect(
          f'{self.dut.address}/P',
          transport=core.BT_BR_EDR_TRANSPORT,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )
      self.logger.info('[REF] Create bond.')
      auth_task = asyncio.tasks.create_task(ref_dut.authenticate())
      self.logger.info('[DUT] Wait for incoming connection.')
      await dut_cb.wait_for_event(
          callback_type=bl4a_api.AclConnected,
          predicate=lambda e: (e.address == ref_addr),
          timeout=_DEFAULT_STEP_TIMEOUT,
      )

    self.logger.info('[DUT] Wait for pairing request.')
    dut_pairing_event = await dut_cb.wait_for_event(
        callback_type=bl4a_api.PairingRequest,
        predicate=lambda e: (e.address == ref_addr),
        timeout=_DEFAULT_STEP_TIMEOUT,
    )

    self.logger.info('[DUT] Check reported pairing method.')
    expected_dut_pairing_variant, expected_ref_pairing_variant, ref_answer = {
        pairing.PairingDelegate.NO_OUTPUT_NO_INPUT: (
            android_constants.PairingVariant.CONSENT,
            pairing_utils.PairingVariant.JUST_WORK,
            pairing_utils.PairingEvent(
                pairing_utils.PairingVariant.JUST_WORK, True
            ),
        ),
        pairing.PairingDelegate.KEYBOARD_INPUT_ONLY: (
            android_constants.PairingVariant.DISPLAY_PASSKEY,
            pairing_utils.PairingVariant.PASSKEY_ENTRY_REQUEST,
            pairing_utils.PairingEvent(
                pairing_utils.PairingVariant.PASSKEY_ENTRY_REQUEST,
                dut_pairing_event.pin,
            ),
        ),
        pairing.PairingDelegate.DISPLAY_OUTPUT_ONLY: (
            android_constants.PairingVariant.PASSKEY_CONFIRMATION,
            pairing_utils.PairingVariant.PASSKEY_ENTRY_NOTIFICATION,
            pairing_utils.PairingEvent(
                pairing_utils.PairingVariant.JUST_WORK, True
            ),
        ),
        pairing.PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT: (
            android_constants.PairingVariant.PASSKEY_CONFIRMATION,
            pairing_utils.PairingVariant.NUMERIC_COMPARISON,
            pairing_utils.PairingEvent(
                pairing_utils.PairingVariant.NUMERIC_COMPARISON,
                dut_pairing_event.pin,
            ),
        ),
    }[ref_io_capability]
    self.assertEqual(dut_pairing_event.variant, expected_dut_pairing_variant)

    self.logger.info('[REF] Wait for pairing request.')
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_pairing_event = await pairing_delegate.pairing_events.get()

    self.logger.info('[REF] Check reported pairing method.')
    self.assertEqual(ref_pairing_event.variant, expected_ref_pairing_variant)

    self.logger.info('[DUT] Handle pairing confirmation.')
    if variant in (TestVariant.ACCEPT, TestVariant.REJECTED):
      self.dut.bt.setPairingConfirmation(ref_addr, True)
    elif variant in (TestVariant.REJECT,):
      self.dut.bt.setPairingConfirmation(ref_addr, False)

    self.logger.info('[REF] Handle pairing confirmation.')
    if variant in (TestVariant.ACCEPT, TestVariant.REJECT):
      pairing_delegate.pairing_answers.put_nowait(ref_answer)
    elif variant in (TestVariant.DISCONNECTED,):
      await ref_dut.disconnect()
    elif variant in (TestVariant.REJECTED,):
      if ref_answer.variant in (
          pairing_utils.PairingVariant.JUST_WORK,
          pairing_utils.PairingVariant.NUMERIC_COMPARISON,
      ):
        ref_answer.arg = False
      else:
        ref_answer.arg = None

      pairing_delegate.pairing_answers.put_nowait(ref_answer)

    self.logger.info('[DUT] Check final state.')
    expect_state = (
        android_constants.BondState.BONDED
        if variant == TestVariant.ACCEPT
        else android_constants.BondState.NONE
    )
    actual_state = (
        await dut_cb.wait_for_event(
            callback_type=bl4a_api.BondStateChanged,
            predicate=lambda e: (e.state in _TERMINATED_BOND_STATES),
            timeout=_DEFAULT_STEP_TIMEOUT,
        )
    ).state
    self.assertEqual(actual_state, expect_state)

    if auth_task:
      self.logger.info('[REF] Wait authentication complete.')
      expected_errors = [] if variant == TestVariant.ACCEPT else [hci.HCI_Error]
      with contextlib.suppress(*expected_errors):
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          await auth_task

  async def _test_smp_over_classic_async(
      self,
      expected_key_distribution: _KeyDistribution,
  ) -> None:
    """Tests CTKD procedure with SMP over Classic channel.

    Prerequisite:
      * A Classic ACL connection has been established.

    Test steps:
      1. Initiate or accept CTKD on REF.
      2. Wait for CTKD complete.
      3. Make an LE connection.
         (If IRK is present, RPA wil be used in this stage, otherwise using
         identity address.)
      4. (If LTK present) Encrypt the link to verify LTK.
      5. (If CSRK present) Verify CSRK lierally.

    Args:
      expected_key_distribution: Keys expected to be distributed.
    """
    pairing_delegate = self.pairing_delegate
    assert pairing_delegate is not None

    ref_dut = self.ref.device.find_connection_by_bd_addr(
        hci.Address(f'{self.dut.address}/P')
    )
    assert ref_dut is not None

    # #################################
    # CTKD procedure.
    # #################################
    ref_pairing_future = asyncio.futures.Future[bumble_keys.PairingKeys]()
    ref_key_updates: asyncio.Queue[None] | None = None

    with pyee_extensions.EventWatcher() as watcher:
      # [REF] Watch pairing complete.
      @watcher.once(ref_dut, 'pairing')
      def on_pairing(keys: bumble_keys.PairingKeys) -> None:
        ref_pairing_future.set_result(keys)

      if _KeyDistribution.DISTRIBUTE_IDENTITY_KEY in expected_key_distribution:
        # [REF] IRK exchange will trigger an async resolving list update.
        ref_key_updates = watcher.async_monitor(
            self.ref.device, 'key_store_update'
        )

      self.logger.info('[REF] REF has role=%s.', _Role(ref_dut.role).name)
      pair_task: asyncio.tasks.Task = None
      if ref_dut.role == _Role.CENTRAL:
        # [REF] Send SMP pairing request.
        pair_task = asyncio.tasks.create_task(ref_dut.pair())
      else:
        # [REF] Accept SMP pairing request.
        pairing_delegate.acceptions.put_nowait(True)

      self.logger.info('[REF] Wait for CTKD complete.')
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        if pair_task:
          await pair_task
        if ref_key_updates:
          await ref_key_updates.get()
        keys = await ref_pairing_future

    # If IRK is not exchanged, devices cannot recognize each other from RPA,
    # so they will use identity address for verification.
    ref_address_type = hci.OwnAddressType.PUBLIC
    if _KeyDistribution.DISTRIBUTE_IDENTITY_KEY in expected_key_distribution:
      self.assertIsNotNone(keys.irk)
      ref_address_type = hci.OwnAddressType.RESOLVABLE_OR_PUBLIC

    # #################################
    # IRK & LTK verification.
    # #################################

    self.logger.info('[REF] Create LE L2CAP server and start advertising.')
    ref_l2cap_server = self.ref.device.create_l2cap_server(
        l2cap.LeCreditBasedChannelSpec()
    )
    await self.ref.device.start_advertising(own_address_type=ref_address_type)

    self.logger.info('[DUT] Make LE connection.')
    secure_connection = (
        _KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY in expected_key_distribution
    )
    await self.dut.bl4a.create_l2cap_channel(
        address=self.ref.address,
        secure=secure_connection,
        psm=ref_l2cap_server.psm,
        transport=android_constants.Transport.LE,
        address_type=android_constants.AddressTypeStatus.PUBLIC,
    )

    # #################################
    # CSRK verification.
    # #################################
    if _KeyDistribution.DISTRIBUTE_SIGNING_KEY in expected_key_distribution:
      self.assertIsNotNone(keys.csrk)

  @navi_test_base.parameterized(*(
      (variant, ref_io_capability, ref_role)
      for (variant, ref_io_capability, ref_role) in itertools.product(
          list(TestVariant),
          (
              _IoCapability.NO_OUTPUT_NO_INPUT,
              _IoCapability.KEYBOARD_INPUT_ONLY,
              _IoCapability.DISPLAY_OUTPUT_ONLY,
              _IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
          ),
          (_Role.CENTRAL, _Role.PERIPHERAL),
      )
      if not (
          variant == TestVariant.REJECT
          and ref_io_capability == _IoCapability.KEYBOARD_INPUT_ONLY
      )
  ))
  @navi_test_base.retry(max_count=2)
  async def test_outgoing_pairing_ssp_only(
      self,
      variant: TestVariant,
      ref_io_capability: _IoCapability,
      ref_role: _Role,
  ) -> None:
    """Tests outgoing Simple Secure Pairing.

    Test steps:
      1. Perform SSP.

    Args:
      variant: variant of pairing actions performmed in the test.
      ref_io_capability: IO capabilities of the REF device.
      ref_role: ACL role of the REF device.
    """
    # [REF] Disable SMP over Classic L2CAP channel.
    self.ref.device.l2cap_channel_manager.deregister_fixed_channel(
        smp.SMP_BR_CID
    )
    self.pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=ref_io_capability,
        auto_accept=True,
    )
    await self._test_ssp_pairing_async(
        variant=variant,
        pairing_direction=_Direction.OUTGOING,
        ref_io_capability=ref_io_capability,
        ref_role=ref_role,
    )

  @navi_test_base.parameterized(*(
      (variant, ref_io_capability)
      for (variant, ref_io_capability) in itertools.product(
          list(TestVariant),
          (
              _IoCapability.NO_OUTPUT_NO_INPUT,
              _IoCapability.KEYBOARD_INPUT_ONLY,
              _IoCapability.DISPLAY_OUTPUT_ONLY,
              _IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
          ),
      )
      if not (
          variant == TestVariant.REJECT
          and ref_io_capability == _IoCapability.KEYBOARD_INPUT_ONLY
      )
  ))
  @navi_test_base.retry(max_count=2)
  async def test_incoming_pairing_ssp_only(
      self,
      variant: TestVariant,
      ref_io_capability: _IoCapability,
  ) -> None:
    """Tests incoming Simple Secure Pairing.

    Test steps:
      1. Perform SSP.

    Args:
      variant: variant of pairing actions performmed in the test.
      ref_io_capability: IO capabilities of the REF device.
    """
    # [REF] Disable SMP over Classic L2CAP channel.
    self.ref.device.l2cap_channel_manager.deregister_fixed_channel(
        smp.SMP_BR_CID
    )
    self.pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=ref_io_capability,
        auto_accept=True,
    )
    await self._test_ssp_pairing_async(
        variant=variant,
        pairing_direction=_Direction.INCOMING,
        ref_io_capability=ref_io_capability,
        ref_role=_Role.CENTRAL,  # unused
    )

  @navi_test_base.parameterized(
      *itertools.product(
          (_Role.CENTRAL, _Role.PERIPHERAL),
          (
              # LTK + IRK
              (
                  _KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY
                  | _KeyDistribution.DISTRIBUTE_IDENTITY_KEY
              ),
              # LTK + IRK + CSRK
              (
                  _KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY
                  | _KeyDistribution.DISTRIBUTE_IDENTITY_KEY
                  | _KeyDistribution.DISTRIBUTE_SIGNING_KEY
              ),
          ),
      ),
  )
  @navi_test_base.retry(max_count=2)
  async def test_outgoing_pairing_ssp_ctkd(
      self,
      ref_role: _Role,
      key_distribution: _KeyDistribution,
  ) -> None:
    """Tests outgoing Simple Secure Pairing with CTKD.

    Test steps:
      1. Perform SSP.
      2. Perform CTKD (Cross-Transport Key Derivation).

    Args:
      ref_role: ACL role of the REF device.
      key_distribution: key distribution in SMP preferred by the REF device.
    """
    ref_io_capability = _IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT
    # [REF] Enable SMP over Classic L2CAP channel.
    self.ref.device.l2cap_channel_manager.register_fixed_channel(
        smp.SMP_BR_CID, self.ref.device.on_smp_pdu
    )
    self.pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=ref_io_capability,
        local_initiator_key_distribution=key_distribution,
        local_responder_key_distribution=key_distribution,
        auto_accept=False,
    )
    await self._test_ssp_pairing_async(
        variant=TestVariant.ACCEPT,
        pairing_direction=_Direction.OUTGOING,
        ref_io_capability=ref_io_capability,
        ref_role=ref_role,
    )
    await self._test_smp_over_classic_async(
        expected_key_distribution=key_distribution
    )

  @navi_test_base.retry(max_count=2)
  async def test_legacy_pairing_incoming(self) -> None:
    """Tests incoming Legacy Pairing.

    Test steps:
      1. Disable SSP on REF.
      2. Pair DUT from REF.
      3. Wait for pairing requests on REF.
      4. Set pairing PIN on REF.
      5. Wait for pairing requests on DUT.
      6. Set pairing PIN on DUT.
      7. Verify final states.
    """

    dut_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.close_after_test.append(dut_cb)
    ref_addr = str(self.ref.address)
    pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=_IoCapability.KEYBOARD_INPUT_ONLY,
        auto_accept=False,
    )

    def pairing_config_factory(
        connection: device.Connection,
    ) -> pairing.PairingConfig:
      del connection
      return pairing.PairingConfig(delegate=pairing_delegate)

    self.ref.device.pairing_config_factory = pairing_config_factory

    self.logger.info('[REF] Disable SSP on REF.')
    await self.ref.device.send_command(
        hci.HCI_Write_Simple_Pairing_Mode_Command(simple_pairing_mode=0)
    )

    self.logger.info('[REF] Connect to DUT.')
    ref_dut = await self.ref.device.connect(
        f'{self.dut.address}/P',
        transport=core.BT_BR_EDR_TRANSPORT,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.logger.info('[REF] Create bond.')
    auth_task = asyncio.tasks.create_task(ref_dut.authenticate())
    self.logger.info('[DUT] Wait for incoming connection.')
    await dut_cb.wait_for_event(
        callback_type=bl4a_api.AclConnected,
        predicate=lambda e: (e.address == ref_addr),
        timeout=_DEFAULT_STEP_TIMEOUT,
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
        callback_type=bl4a_api.PairingRequest,
        predicate=lambda e: (e.address == ref_addr),
        timeout=_DEFAULT_STEP_TIMEOUT,
    )
    self.assertEqual(
        dut_pairing_request.variant, android_constants.PairingVariant.PIN
    )

    self.logger.info('[DUT] Handle pairing confirmation.')
    self.dut.bt.setPin(ref_addr, _PIN_CODE_DEFAULT)

    self.logger.info('[DUT] Check final state.')
    actual_state = (
        await dut_cb.wait_for_event(
            callback_type=bl4a_api.BondStateChanged,
            predicate=lambda e: (e.state in _TERMINATED_BOND_STATES),
            timeout=_DEFAULT_STEP_TIMEOUT,
        )
    ).state
    self.assertEqual(actual_state, android_constants.BondState.BONDED)

    self.logger.info('[REF] Wait authentication complete.')
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await auth_task

  @navi_test_base.parameterized(
      *itertools.product((_COD_DEFAULT, _COD_HEADSETS))
  )
  @navi_test_base.retry(max_count=2)
  async def test_legacy_pairing_outgoing(self, ref_cod: int) -> None:
    """Tests outgoing Legacy Pairing.

    Test steps:
      1. Disable SSP on REF.
      2. Pair REF from DUT.
      3. Wait for pairing requests on DUT.
      4. Set pairing PIN on DUT.
      5. Wait for pairing requests on REF.
      6. Set pairing PIN on REF.
      7. Verify final states.

    Args:
      ref_cod: Class of Device code of REF.
    """

    dut_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.close_after_test.append(dut_cb)
    ref_addr = str(self.ref.address)
    pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=_IoCapability.KEYBOARD_INPUT_ONLY,
        auto_accept=False,
    )

    auto_pair = ref_cod in (_COD_HEADSETS,)
    if auto_pair:
      pin_code = _PIN_CODE_AUTO_PAIR
    else:
      pin_code = _PIN_CODE_DEFAULT

    def pairing_config_factory(
        connection: device.Connection,
    ) -> pairing.PairingConfig:
      del connection
      return pairing.PairingConfig(delegate=pairing_delegate)

    self.ref.device.pairing_config_factory = pairing_config_factory

    self.logger.info('[REF] Set CoD.')
    await self.ref.device.send_command(
        hci.HCI_Write_Class_Of_Device_Command(class_of_device=ref_cod)
    )

    self.logger.info('[REF] Disable SSP on REF.')
    await self.ref.device.send_command(
        hci.HCI_Write_Simple_Pairing_Mode_Command(simple_pairing_mode=0)
    )

    self.logger.info('[DUT] Search for REF to update CoD.')
    self.dut.bt.startInquiry()
    await dut_cb.wait_for_event(
        callback_type=bl4a_api.DeviceFound,
        predicate=lambda e: (e.address == ref_addr),
        timeout=_DEFAULT_STEP_TIMEOUT,
    )

    self.logger.info('[DUT] Create bond and connect implicitly.')
    self.assertTrue(
        self.dut.bt.createBond(ref_addr, android_constants.Transport.CLASSIC)
    )

    if not auto_pair:
      self.logger.info('[DUT] Wait for pairing request.')
      dut_pairing_request = await dut_cb.wait_for_event(
          callback_type=bl4a_api.PairingRequest,
          predicate=lambda e: (e.address == ref_addr),
          timeout=_DEFAULT_STEP_TIMEOUT,
      )
      self.assertEqual(
          dut_pairing_request.variant, android_constants.PairingVariant.PIN
      )

      self.logger.info('[DUT] Handle pairing confirmation.')
      self.dut.bt.setPin(ref_addr, pin_code)

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
            variant=pairing_utils.PairingVariant.PIN_CODE_REQUEST, arg=pin_code
        )
    )

    self.logger.info('[DUT] Check final state.')
    actual_state = (
        await dut_cb.wait_for_event(
            callback_type=bl4a_api.BondStateChanged,
            predicate=lambda e: (e.state in _TERMINATED_BOND_STATES),
            timeout=_DEFAULT_STEP_TIMEOUT,
        )
    ).state
    self.assertEqual(actual_state, android_constants.BondState.BONDED)


if __name__ == '__main__':
  test_runner.main()
