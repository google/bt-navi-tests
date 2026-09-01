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
from collections.abc import Callable
import copy
import enum
import itertools
from typing import Any

from bumble import core
from bumble import device
from bumble import hci
from bumble import pairing
from bumble import smp
from mobly import test_runner

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import matcher
from navi.utils import pairing as pairing_utils
from navi.utils import pyee_extensions
from navi.utils import retry

# Somehow this rule has some issues.
# pylint: disable=redundant-match

_TERMINATED_BOND_STATES = (
    android_constants.BondState.BONDED,
    android_constants.BondState.NONE,
)
_DEFAULT_STEP_TIMEOUT_SECONDS = 15.0


class TestVariant(enum.Enum):
  ACCEPT = 'accept'
  REJECT = 'reject'
  REJECTED = 'rejected'
  DISCONNECTED = 'disconnected'


_AddressType = hci.OwnAddressType
_BondState = android_constants.BondState
_Direction = constants.Direction
_KeyDistribution = pairing.PairingDelegate.KeyDistribution
_AndroidPairingVariant = android_constants.PairingVariant
_BumblePairingVariant = pairing_utils.PairingVariant
_DEFAULT_SETUP_TIMEOUT_SECONDS = 15.0


class LePairingTest(navi_test_base.TwoDevicesTestBase):

  @retry.retry_on_exception()
  async def _make_outgoing_connection(
      self, ref_connection_address_type: _AddressType, create_bond: bool
  ) -> device.Connection:
    ref_addr = str(
        self.ref.random_address
        if ref_connection_address_type == _AddressType.RANDOM
        else self.ref.address
    )
    self.logger.info('[REF] Start advertising.')
    await self.ref.device.start_advertising(
        own_address_type=ref_connection_address_type
    )

    with pyee_extensions.EventWatcher() as watcher:
      ref_dut_connection_future = asyncio.get_running_loop().create_future()

      @watcher.on(self.ref.device, 'connection')
      def _(connection: device.Connection) -> None:
        if connection.transport == core.BT_LE_TRANSPORT:
          ref_dut_connection_future.set_result(connection)

      self.logger.info('[DUT] Connect to REF.')
      if create_bond:
        self.assertTrue(
            self.dut.bt.createBond(
                ref_addr,
                android_constants.Transport.LE,
                ref_connection_address_type,
            )
        )
      else:
        gatt_client = await self.dut.bl4a.connect_gatt_client(
            address=ref_addr,
            address_type=android_constants.AddressTypeStatus(
                ref_connection_address_type.value
            ),
            transport=android_constants.Transport.LE,
        )
        self.test_case_context.push(gatt_client)

      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_dut_connection = await ref_dut_connection_future

      await self.ref.device.stop_advertising()
      return ref_dut_connection

  async def _make_incoming_connection(
      self, ref_connection_address_type: _AddressType
  ) -> device.Connection:
    return await self.connect_le_from_ref(
        dut_address_type=android_constants.AddressTypeStatus.RANDOM,
        ref_address_type=ref_connection_address_type,
        wait_for_dut_connected=False,
    )

  @navi_test_base.parameterized(*(
      (
          variant,
          connection_direction,
          pairing_direction,
          ref_io_capability,
          ref_connection_address_type,
          smp_key_distribution,
      )
      for (
          variant,
          connection_direction,
          pairing_direction,
          ref_io_capability,
          ref_connection_address_type,
          smp_key_distribution,
      ) in itertools.product(
          list(TestVariant),
          list(_Direction),
          list(_Direction),
          (
              pairing.PairingDelegate.NO_OUTPUT_NO_INPUT,
              pairing.PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
          ),
          (_AddressType.RANDOM, _AddressType.PUBLIC),
          (
              # IRK + LTK
              _KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY
              | _KeyDistribution.DISTRIBUTE_IDENTITY_KEY,
              # IRK + LTK + LK (CTKD)
              _KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY
              | _KeyDistribution.DISTRIBUTE_IDENTITY_KEY
              | _KeyDistribution.DISTRIBUTE_LINK_KEY,
          ),
      )
      # Android cannot send SMP_Security_Request.
      if not (
          connection_direction == _Direction.INCOMING
          and pairing_direction == _Direction.OUTGOING
      )
  ))
  @navi_test_base.retry(max_count=2)
  async def test_secure_pairing(
      self,
      variant: TestVariant,
      connection_direction: _Direction,
      pairing_direction: _Direction,
      ref_io_capability: pairing.PairingDelegate.IoCapability,
      ref_connection_address_type: _AddressType,
      smp_key_distribution: _KeyDistribution,
  ) -> None:
    """Tests LE Secure pairing.

    Test steps:
      1. Setup configurations.
      2. Make ACL connections.
      3. Start pairing.
      4. Wait for pairing requests and verify pins.
      5. Make actions corresponding to variants.
      6. Verify final states.

    Args:
      variant: Action to perform in the pairing procedure.
      connection_direction: Direction of connection. DUT->REF is outgoing, and
        vice versa.
      pairing_direction: Direction of pairing. DUT->REF is outgoing, and vice
        versa.
      ref_io_capability: IO Capability on the REF device.
      ref_connection_address_type: OwnAddressType of REF used in LE-ACL.
      smp_key_distribution: Key distribution to be specified by the REF device.
    """

    # #######################
    # Setup stage
    # #######################

    pairing_delegate = pairing_utils.PairingDelegate(
        auto_accept=True,
        io_capability=ref_io_capability,
        local_initiator_key_distribution=smp_key_distribution,
        local_responder_key_distribution=smp_key_distribution,
    )

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

    dut_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(dut_cb)
    ref_addr = str(
        self.ref.random_address
        if ref_connection_address_type == _AddressType.RANDOM
        else self.ref.address
    ).upper()

    need_double_confirmation = (
        connection_direction == _Direction.OUTGOING
        and pairing_direction == _Direction.INCOMING
    )

    # ##############################################
    # Connecting & pairing initiating stage
    # ##############################################

    ref_dut: device.Connection
    pair_task: asyncio.Task[None] | None = None
    if connection_direction == _Direction.OUTGOING:
      if pairing_direction == _Direction.INCOMING:
        ref_dut = await self._make_outgoing_connection(
            ref_connection_address_type, create_bond=False
        )
        self.logger.info('[REF] Request pairing.')
        ref_dut.request_pairing()
      else:
        self.logger.info('[DUT] Start pairing.')
        ref_dut = await self._make_outgoing_connection(
            ref_connection_address_type, create_bond=True
        )
        # Clean all bond state events since there might be some events produced
        # by retries.
        dut_cb.get_all_events(bl4a_api.BondStateChanged)
    else:
      ref_dut = await self._make_incoming_connection(
          ref_connection_address_type
      )
      if pairing_direction == _Direction.INCOMING:
        self.logger.info('[REF] Start pairing.')
        pair_task = asyncio.create_task(ref_dut.pair())
      else:
        self.logger.info('[DUT] Start pairing.')
        self.dut.bt.createBond(
            ref_addr,
            android_constants.Transport.LE,
            ref_connection_address_type,
        )

    # #######################
    # Pairing stage
    # #######################

    self.logger.info('[DUT] Wait for pairing request.')
    dut_pairing_event = await dut_cb.wait_for_event(
        bl4a_api.PairingRequest,
        lambda e: (e.address == ref_addr),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )

    if need_double_confirmation:
      self.logger.info('[DUT] Provide initial pairing confirmation.')
      self.dut.bt.setPairingConfirmation(ref_addr, True)
      self.logger.info('[DUT] Wait for 2nd pairing request.')
      dut_pairing_event = await dut_cb.wait_for_event(
          bl4a_api.PairingRequest,
          lambda e: (e.address == ref_addr),
          timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
      )

    self.logger.info('[REF] Wait for pairing request.')
    ref_pairing_event = await asyncio.wait_for(
        pairing_delegate.pairing_events.get(),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )
    ref_answer = variant != TestVariant.REJECTED

    self.logger.info('[DUT] Check reported pairing method.')
    match ref_io_capability:
      case pairing.PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT:
        expected_dut_pairing_variant = _AndroidPairingVariant.CONSENT
        expected_ref_pairing_variant = _BumblePairingVariant.JUST_WORK
      case pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT:
        expected_dut_pairing_variant = (
            _AndroidPairingVariant.PASSKEY_CONFIRMATION
        )
        expected_ref_pairing_variant = _BumblePairingVariant.NUMERIC_COMPARISON
        self.assertEqual(ref_pairing_event.arg, dut_pairing_event.pin)
      case _:
        raise ValueError(f'Unsupported IO capability: {ref_io_capability}')

    self.assertEqual(dut_pairing_event.variant, expected_dut_pairing_variant)

    self.logger.info('[REF] Check reported pairing method.')
    self.assertEqual(ref_pairing_event.variant, expected_ref_pairing_variant)

    self.logger.info('[DUT] Handle pairing confirmation.')
    match variant:
      case TestVariant.ACCEPT | TestVariant.REJECTED:
        self.dut.bt.setPairingConfirmation(ref_addr, True)
      case TestVariant.REJECT:
        self.dut.bt.cancelBond(ref_addr)
      case _:
        # [DUT] Do nothing.
        ...

    self.logger.info('[REF] Handle pairing confirmation.')
    if variant == TestVariant.DISCONNECTED:
      await ref_dut.disconnect()

    pairing_delegate.pairing_answers.put_nowait(ref_answer)

    self.logger.info('[DUT] Check final state.')
    expect_state = (
        android_constants.BondState.BONDED
        if variant == TestVariant.ACCEPT
        else android_constants.BondState.NONE
    )
    actual_state = (
        await dut_cb.wait_for_event(
            bl4a_api.BondStateChanged,
            lambda e: (e.state in _TERMINATED_BOND_STATES),
            timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
        )
    ).state
    self.assertEqual(actual_state, expect_state)

    if pair_task:
      self.logger.info('[REF] Wait pairing complete.')
      if variant == TestVariant.ACCEPT:
        await pair_task
      else:
        with self.assertRaises((core.ProtocolError, asyncio.CancelledError)):
          await pair_task

  @navi_test_base.parameterized(*(
      (
          variant,
          connection_direction,
          pairing_direction,
          ref_io_capability,
      )
      for (
          variant,
          connection_direction,
          pairing_direction,
          ref_io_capability,
      ) in itertools.product(
          list(TestVariant),
          list(_Direction),
          list(_Direction),
          (
              pairing.PairingDelegate.NO_OUTPUT_NO_INPUT,
              pairing.PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
              pairing.PairingDelegate.DISPLAY_OUTPUT_AND_KEYBOARD_INPUT,
              pairing.PairingDelegate.DISPLAY_OUTPUT_ONLY,
              pairing.PairingDelegate.KEYBOARD_INPUT_ONLY,
          ),
      )
      # Android cannot send SMP_Security_Request.
      if not (
          connection_direction == _Direction.INCOMING
          and pairing_direction == _Direction.OUTGOING
      )
  ))
  @navi_test_base.retry(max_count=2)
  async def test_legacy_pairing(
      self,
      variant: TestVariant,
      connection_direction: _Direction,
      pairing_direction: _Direction,
      ref_io_capability: pairing.PairingDelegate.IoCapability,
  ) -> None:
    """Tests LE Secure pairing.

    Test steps:
      1. Setup configurations.
      2. Make ACL connections.
      3. Start pairing.
      4. Wait for pairing requests and verify pins.
      5. Make actions corresponding to variants.
      6. Verify final states.

    Args:
      variant: Action to perform in the pairing procedure.
      connection_direction: Direction of connection. DUT->REF is outgoing, and
        vice versa.
      pairing_direction: Direction of pairing. DUT->REF is outgoing, and vice
        versa.
      ref_io_capability: IO Capability on the REF device.
    """
    if (
        variant == TestVariant.REJECT
        and connection_direction == _Direction.OUTGOING
        and pairing_direction == _Direction.INCOMING
        and ref_io_capability
        in (
            pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
            pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_ONLY,
        )
        and self.dut.bluetooth_mainline_version < 361000000
    ):
      self.skipTest('This combination is broken before 2025-10 release.')

    # ####################### Setup ##########################
    pairing_delegate = pairing_utils.PairingDelegate(
        auto_accept=True,
        io_capability=ref_io_capability,
        local_initiator_key_distribution=pairing.PairingDelegate.DEFAULT_KEY_DISTRIBUTION,
        local_responder_key_distribution=pairing.PairingDelegate.DEFAULT_KEY_DISTRIBUTION,
    )

    def pairing_config_factory(_: device.Connection) -> pairing.PairingConfig:
      return pairing.PairingConfig(
          sc=False,
          mitm=True,
          bonding=True,
          identity_address_type=pairing.PairingConfig.AddressType.PUBLIC,
          delegate=pairing_delegate,
      )

    self.ref.device.pairing_config_factory = pairing_config_factory

    dut_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(dut_cb)
    ref_addr = self.ref.random_address

    need_double_confirmation = (
        connection_direction == _Direction.OUTGOING
        and pairing_direction == _Direction.INCOMING
    )

    # ####################### Connecting ##########################
    ref_dut: device.Connection
    pair_task: asyncio.Task[None] | None = None
    if connection_direction == _Direction.OUTGOING:
      if pairing_direction == _Direction.INCOMING:
        ref_dut = await self._make_outgoing_connection(
            _AddressType.RANDOM, create_bond=False
        )
        self.logger.info('[REF] Request pairing.')
        ref_dut.request_pairing()
      else:
        self.logger.info('[DUT] Start pairing.')
        ref_dut = await self._make_outgoing_connection(
            _AddressType.RANDOM, create_bond=True
        )
        # Clean all bond state events since there might be some events produced
        # by retries.
        dut_cb.get_all_events(bl4a_api.BondStateChanged)
    else:
      ref_dut = await self._make_incoming_connection(_AddressType.RANDOM)
      if pairing_direction == _Direction.INCOMING:
        self.logger.info('[REF] Start pairing.')
        pair_task = asyncio.create_task(ref_dut.pair())
      else:
        self.logger.info('[DUT] Start pairing.')
        self.dut.bt.createBond(
            ref_addr,
            android_constants.Transport.LE,
            _AddressType.RANDOM,
        )

    # ####################### Pairing ##########################
    self.logger.info('[DUT] Wait for pairing request.')
    dut_pairing_event = await dut_cb.wait_for_event(
        bl4a_api.PairingRequest,
        lambda e: (e.address == ref_addr),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )

    if need_double_confirmation:
      self.logger.info('[DUT] Provide initial pairing confirmation.')
      self.dut.bt.setPairingConfirmation(ref_addr, True)
      self.logger.info('[DUT] Wait for 2nd pairing request.')
      dut_pairing_event = await dut_cb.wait_for_event(
          bl4a_api.PairingRequest,
          lambda e: (e.address == ref_addr),
          timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
      )

    if (
        ref_io_capability
        != pairing.PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT
    ):
      self.logger.info('[REF] Wait for pairing request.')
      async with self.assert_not_timeout(_DEFAULT_SETUP_TIMEOUT_SECONDS):
        ref_pairing_event = await pairing_delegate.pairing_events.get()
    else:
      ref_pairing_event = pairing_utils.PairingEvent(
          _BumblePairingVariant.JUST_WORK, None
      )

    dut_accept = variant != TestVariant.REJECT
    ref_accept = variant != TestVariant.REJECTED
    ref_answer: pairing_utils.PairingAnswer
    dut_answer: Callable[[], Any]

    self.logger.info('[DUT] Check reported pairing method.')
    match ref_io_capability, connection_direction:
      case (pairing.PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT, _):
        expected_dut_pairing_variant = _AndroidPairingVariant.CONSENT
        expected_ref_pairing_variant = _BumblePairingVariant.JUST_WORK
        ref_answer = ref_accept
        dut_answer = lambda: self.dut.bt.setPairingConfirmation(ref_addr, True)
      case (
          pairing.PairingDelegate.IoCapability.KEYBOARD_INPUT_ONLY,
          _,
      ) | (
          pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_KEYBOARD_INPUT,
          _Direction.OUTGOING,
      ):
        expected_dut_pairing_variant = _AndroidPairingVariant.DISPLAY_PASSKEY
        expected_ref_pairing_variant = (
            _BumblePairingVariant.PASSKEY_ENTRY_REQUEST
        )
        ref_answer = dut_pairing_event.pin if ref_accept else None
        dut_answer = lambda: self.dut.bt.setPairingConfirmation(ref_addr, True)
      case (
          pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_ONLY
          | pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
          _,
      ) | (
          pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_KEYBOARD_INPUT,
          _Direction.INCOMING,
      ):
        expected_dut_pairing_variant = _AndroidPairingVariant.PIN
        expected_ref_pairing_variant = (
            _BumblePairingVariant.PASSKEY_ENTRY_NOTIFICATION
        )
        ref_answer = dut_pairing_event.pin if ref_accept else None
        dut_answer = lambda: self.dut.bt.setPin(
            ref_addr, f'{ref_pairing_event.arg:06}'
        )
      case _:
        raise ValueError(f'Unsupported IO capability: {ref_io_capability}')

    self.assertEqual(dut_pairing_event.variant, expected_dut_pairing_variant)
    self.assertEqual(ref_pairing_event.variant, expected_ref_pairing_variant)

    self.logger.info('[DUT] Handle pairing confirmation.')
    if dut_accept:
      dut_answer()
    else:
      self.dut.bt.cancelBond(ref_addr)

    self.logger.info('[REF] Handle pairing confirmation.')
    match variant:
      case TestVariant.ACCEPT | TestVariant.REJECT:
        pairing_delegate.pairing_answers.put_nowait(ref_answer)
      case TestVariant.DISCONNECTED:
        await ref_dut.disconnect()
      case TestVariant.REJECTED:
        smp_session = self.ref.device.smp_manager.sessions[ref_dut.handle]
        smp_session.send_pairing_failed(smp.ErrorCode.UNSPECIFIED_REASON)

    self.logger.info('[DUT] Check final state.')
    expect_state = (
        android_constants.BondState.BONDED
        if variant == TestVariant.ACCEPT
        else android_constants.BondState.NONE
    )
    bond_state_changed_event = await dut_cb.wait_for_event(
        bl4a_api.BondStateChanged,
        lambda e: (e.state in _TERMINATED_BOND_STATES),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )
    self.assertEqual(bond_state_changed_event.state, expect_state)

    if pair_task:
      self.logger.info('[REF] Wait pairing complete.')
      if variant == TestVariant.ACCEPT:
        await pair_task
      else:
        with self.assertRaises((core.ProtocolError, asyncio.CancelledError)):
          await pair_task

  @navi_test_base.named_parameterized(*[
      dict(
          testcase_name=(
              f'{"secure" if sc else "legacy"}_{direction.name}_{address_type.name}'
              .lower()
          ),
          sc=sc,
          pairing_direction=direction,
          ref_connection_address_type=address_type,
      )
      for sc, direction, address_type in itertools.product(
          {True, False},
          {_Direction.OUTGOING, _Direction.INCOMING},
          {_AddressType.PUBLIC, _AddressType.RANDOM},
      )
      # Legacy incoming pairing is not supported on Android.
      if sc or direction == _Direction.OUTGOING
  ])
  async def test_oob_pairing(
      self,
      sc: bool,
      pairing_direction: _Direction,
      ref_connection_address_type: _AddressType,
  ) -> None:
    """Tests LE OOB pairing.

    Test steps:
      1. Setup configurations.
      2. Exchange OOB data.
      3. Start pairing.
      4. Verify final states.

    Note: Legacy variants fail from 24Q4 to 25Q3 due to stack issue.

    Args:
      sc: Whether to use secure connection.
      pairing_direction: Direction of pairing. DUT->REF is outgoing, and vice
        versa.
      ref_connection_address_type: Address type of the REF device.
    """
    if not sc and self.dut.bluetooth_mainline_version < 361000000:
      self.skipTest('Legacy OOB pairing is broken before 2025-10 release.')

    pairing_delegate = pairing_utils.PairingDelegate(
        auto_accept=True,
        local_initiator_key_distribution=pairing.PairingDelegate.DEFAULT_KEY_DISTRIBUTION,
        local_responder_key_distribution=pairing.PairingDelegate.DEFAULT_KEY_DISTRIBUTION,
    )
    ref_oob_context = pairing.OobContext()
    ref_oob_legacy_context = pairing.OobLegacyContext()
    ref_oob_config = pairing.PairingConfig.OobConfig(
        our_context=ref_oob_context,
        peer_data=None,
        legacy_context=ref_oob_legacy_context,
    )
    if ref_connection_address_type == _AddressType.RANDOM:
      ref_address_bytes = bytes(self.ref.device.random_address)
      ref_address_type = hci.AddressType.RANDOM_DEVICE
      ref_address = self.ref.random_address
    else:
      ref_address_bytes = bytes(self.ref.device.public_address)
      ref_address_type = hci.AddressType.PUBLIC_DEVICE
      ref_address = self.ref.address
    ref_address_with_type_bytes = ref_address_bytes + bytes([ref_address_type])

    def pairing_config_factory(_: device.Connection) -> pairing.PairingConfig:
      return pairing.PairingConfig(
          sc=sc,
          mitm=True,
          bonding=True,
          identity_address_type=pairing.PairingConfig.AddressType.PUBLIC,
          delegate=pairing_delegate,
          oob=ref_oob_config,
      )

    self.ref.device.pairing_config_factory = pairing_config_factory

    dut_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(dut_cb)
    ref_pairing_events = asyncio.Queue[None]()

    # Register a callback to get pairing events from the REF device.
    @self.ref.device.on(device.Device.EVENT_CONNECTION)
    def _(connection: device.Connection) -> None:
      connection.on(connection.EVENT_PAIRING, ref_pairing_events.put)

    if pairing_direction == _Direction.INCOMING:
      shared_data_from_dut = self.dut.bl4a.generate_oob_data(
          android_constants.Transport.LE
      )
      ref_oob_config.peer_data = pairing.OobSharedData(
          c=shared_data_from_dut.confirmation_hash,
          r=shared_data_from_dut.randomizer_hash or b'',
      )
      ref_oob_legacy_context.tk = shared_data_from_dut.le_temporary_key or b''
      ref_dut = await self._make_incoming_connection(
          ref_connection_address_type
      )
      self.logger.info('[REF] Start pairing.')
      async with self.assert_not_timeout(_DEFAULT_SETUP_TIMEOUT_SECONDS):
        await ref_dut.pair()
    else:
      self.logger.info('[REF] Start advertising.')
      async with self.assert_not_timeout(_DEFAULT_SETUP_TIMEOUT_SECONDS):
        await self.ref.device.start_advertising(
            own_address_type=ref_connection_address_type, auto_restart=False
        )

      shared_data_from_ref = ref_oob_context.share()
      dut_oob_data = bl4a_api.OobData(
          confirmation_hash=shared_data_from_ref.c,
          randomizer_hash=shared_data_from_ref.r,
          device_address_with_type=ref_address_with_type_bytes,
          le_device_role=core.LeRole.PERIPHERAL_ONLY,
          le_temporary_key=ref_oob_legacy_context.tk,
      )
      self.logger.info('[DUT] Start OOB pairing.')
      result = self.dut.bl4a.create_bond_oob(
          address=ref_address,
          address_type=(
              android_constants.AddressTypeStatus.RANDOM
              if ref_connection_address_type == _AddressType.RANDOM
              else android_constants.AddressTypeStatus.PUBLIC
          ),
          transport=android_constants.Transport.LE,
          p_192_data=dut_oob_data if not sc else None,
          p_256_data=dut_oob_data if sc else None,
      )
      self.assertTrue(result, '[DUT] Failed to create bond')

    self.logger.info('[DUT] Wait for pairing complete.')
    bonded_event = await dut_cb.wait_for_event(
        bl4a_api.BondStateChanged(
            address=ref_address, state=matcher.any_of(*_TERMINATED_BOND_STATES)
        )
    )
    self.assertEqual(bonded_event.state, android_constants.BondState.BONDED)

    self.logger.info('[REF] Wait for pairing complete.')
    async with self.assert_not_timeout(_DEFAULT_SETUP_TIMEOUT_SECONDS):
      await ref_pairing_events.get()

  @navi_test_base.named_parameterized(
      public=pairing.PairingConfig.AddressType.PUBLIC,
      random=pairing.PairingConfig.AddressType.RANDOM,
  )
  async def test_pairing_with_rpa(
      self,
      identity_address_type: pairing.PairingConfig.AddressType,
  ) -> None:
    """Tests pairing with RPA, make sure the identity address is associated with the bond.

    Test steps:
      1. Generate a RPA for the REF device.
      2. Start advertising on REF with RPA.
      3. Connect and bond to REF over LE on DUT.
      4. Verify final states.

    Args:
      identity_address_type: Identity address type of the REF device.
    """
    ref_rpa = hci.Address.generate_private_address(self.ref.device.irk)
    ref_rpa_str = str(ref_rpa).upper()
    self.ref.device.pairing_config_factory = lambda _: pairing.PairingConfig(
        identity_address_type=identity_address_type
    )

    self.logger.info('[REF] Start advertising with RPA: %s', ref_rpa)
    await self.ref.device.create_advertising_set(
        random_address=ref_rpa,
        auto_start=True,
        auto_restart=False,
    )

    adapter_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(adapter_cb)

    self.logger.info('[DUT] Connect and bond to REF over LE.')
    self.dut.bt.createBond(
        ref_rpa_str,
        android_constants.Transport.LE,
        android_constants.AddressTypeStatus.RANDOM,
    )

    self.logger.info('[DUT] Wait for pairing request.')
    await adapter_cb.wait_for_event(
        bl4a_api.PairingRequest(
            address=ref_rpa_str,
            variant=matcher.ANY,
            pin=matcher.ANY,
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )

    self.logger.info('[DUT] Provide pairing confirmation.')
    self.dut.bt.setPairingConfirmation(ref_rpa_str, True)

    self.logger.info('[DUT] Wait for bond state changed.')
    bond_state_changed_event = await adapter_cb.wait_for_event(
        bl4a_api.BondStateChanged(
            address=ref_rpa_str,
            state=matcher.any_of(*_TERMINATED_BOND_STATES),
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )
    self.assertEqual(
        bond_state_changed_event.state,
        android_constants.BondState.BONDED,
        '[DUT] Bond state is not BONDED.',
    )

    # Verify that the identity address is associated with the bond.
    if identity_address_type == pairing.PairingConfig.AddressType.PUBLIC:
      ref_identity_address = self.ref.address
    else:
      ref_identity_address = self.ref.random_address
    self.assertEqual(
        self.dut.bt.getBondState(ref_identity_address),
        android_constants.BondState.BONDED,
        '[DUT] Bond state is not BONDED for identity address.',
    )
    self.assertEqual(
        self.dut.bt.getIdentityAddress(ref_rpa_str),
        ref_identity_address,
        '[DUT] Identity address does not match.',
    )

  async def test_repairing_on_br_after_le_bond_removal(self) -> None:
    """Tests repairing on BR/EDR after LE bond removal.

    Context: b/326294532.

    Test steps:
      1. REF is discoverable and connectable over LE
      2. DUT connects and bonds to REF over LE
      3. DUT disconnects from the REF device
      4. DUT removes the REF device
      5. REF becomes discoverable over BR/EDR but not over LE
      6. DUT finds the REF device via inquiry
      7. DUT attempts to bond with the REF device over BR/EDR
    """
    ref_rpa = hci.Address.generate_private_address(self.ref.device.irk)
    ref_rpa_str = str(ref_rpa).upper()
    self.ref.device.pairing_config_factory = lambda _: pairing.PairingConfig(
        identity_address_type=pairing.PairingConfig.AddressType.PUBLIC,
        delegate=pairing.PairingDelegate(
            io_capability=pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_KEYBOARD_INPUT,
        ),
    )

    self.logger.info('[REF] Stop advertising over BR/EDR.')
    await self.ref.device.set_discoverable(False)

    self.logger.info('[REF] Start advertising with RPA: %s', ref_rpa)
    await self.ref.device.create_advertising_set(
        random_address=ref_rpa,
        auto_start=True,
        auto_restart=False,
    )

    adapter_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(adapter_cb)

    self.logger.info('[DUT] Connect and bond to REF over LE.')
    self.dut.bt.createBond(
        ref_rpa_str,
        android_constants.Transport.LE,
        android_constants.AddressTypeStatus.RANDOM,
    )

    self.logger.info('[DUT] Wait for pairing request.')
    await adapter_cb.wait_for_event(
        bl4a_api.PairingRequest(
            address=ref_rpa_str,
            variant=matcher.ANY,
            pin=matcher.ANY,
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )

    self.logger.info('[DUT] Provide pairing confirmation.')
    self.dut.bt.setPairingConfirmation(ref_rpa_str, True)

    self.logger.info('[DUT] Wait for bond state changed.')
    bond_state_changed_event = await adapter_cb.wait_for_event(
        bl4a_api.BondStateChanged(
            address=ref_rpa_str,
            state=matcher.any_of(*_TERMINATED_BOND_STATES),
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )
    self.assertEqual(
        bond_state_changed_event.state,
        android_constants.BondState.BONDED,
        'Bond state should be BONDED after pairing',
    )

    self.logger.info('[DUT] Disconnect.')
    self.dut.bt.disconnect(ref_rpa_str)

    self.logger.info('[DUT] Wait for ACL disconnected.')
    await adapter_cb.wait_for_event(
        bl4a_api.AclDisconnected(
            address=ref_rpa_str,
            transport=android_constants.Transport.LE,
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )

    self.logger.info('[DUT] Remove bond.')
    self.dut.bt.removeBond(ref_rpa_str)

    self.logger.info('[DUT] Wait for bond state removed.')
    await adapter_cb.wait_for_event(
        bl4a_api.BondStateChanged(
            address=ref_rpa_str,
            state=android_constants.BondState.NONE,
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )

    self.logger.info('[REF] Start advertising over BR/EDR.')
    await self.ref.device.set_discoverable(True)

    self.logger.info('[DUT] Find REF device via inquiry.')
    self.dut.bt.startInquiry()

    self.logger.info('[DUT] Wait for inquiry results.')
    await adapter_cb.wait_for_event(
        bl4a_api.DeviceFound(
            address=self.ref.address,
            name=matcher.ANY,
            rssi=matcher.ANY,
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )

    self.logger.info('[DUT] Attempt to bond with REF over BR/EDR.')
    self.dut.bt.createBond(
        self.ref.address, android_constants.Transport.CLASSIC
    )

    self.logger.info('[DUT] Wait for pairing request.')
    await adapter_cb.wait_for_event(
        bl4a_api.PairingRequest(
            address=self.ref.address,
            variant=matcher.ANY,
            pin=matcher.ANY,
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )

    self.logger.info('[DUT] Provide pairing confirmation.')
    self.dut.bt.setPairingConfirmation(self.ref.address, True)

    self.logger.info('[DUT] Wait for bond state changed.')
    bond_state_changed_event = await adapter_cb.wait_for_event(
        bl4a_api.BondStateChanged(
            address=self.ref.address,
            state=matcher.any_of(*_TERMINATED_BOND_STATES),
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )
    self.assertEqual(
        bond_state_changed_event.state,
        android_constants.BondState.BONDED,
        'Bond state should be BONDED after pairing',
    )

  async def test_le_no_bonding_pairing_does_not_delete_existing_bond(
      self,
  ) -> None:
    """Tests LE No Bonding pairing request doesn't delete existing bond.

    Test steps:
      1. Bond DUT and REF over BR/EDR (without CTKD).
      2. Establish LE connection.
      3. Trigger LE pairing request with AuthReq: No Bonding (bonding=False)
         from REF.
      4. Accept pairing request on DUT.
      5. Verify the bond remains present on DUT (does not transition to
         BOND_NONE).
      6. Restart Bluetooth on DUT.
      7. Verify the bond is still present.
      8. Reconnect and verify the link is encrypted.
    """
    self.logger.info('Step 1: Bond DUT and REF over BR/EDR (without CTKD).')
    # Disable SMP over Classic to prevent CTKD
    self.ref.device.l2cap_channel_manager.deregister_fixed_channel(
        smp.SMP_BR_CID
    )
    await self.classic_connect_and_pair()
    self.assertIn(self.ref.address, self.dut.bt.getBondedDevices())
    keystore = copy.deepcopy(self.ref.device.keystore)

    self.logger.info('Step 2: Establish LE connection.')
    ref_conn = await self._make_outgoing_connection(
        hci.OwnAddressType.PUBLIC, create_bond=False
    )

    # Register adapter callback to monitor bond state changes
    dut_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(dut_cb)

    self.logger.info(
        'Step 3 & 4: Trigger LE pairing request with AuthReq: No Bonding from'
        ' REF and accept on DUT.'
    )
    await self._le_pair_no_bonding(ref_conn, dut_cb)

    self.logger.info('Step 5: Verify the bond remains present on DUT.')
    self.logger.info('[DUT] Wait for bond state to settle.')
    terminal_bond_event = await dut_cb.wait_for_event(
        bl4a_api.BondStateChanged(
            address=self.ref.address,
            state=matcher.any_of(*_TERMINATED_BOND_STATES),
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.assertEqual(
        terminal_bond_event.state,
        android_constants.BondState.BONDED,
        'Bond was deleted or got stuck in bonding.',
    )

    self.logger.info('Step 6: Restart Bluetooth on DUT.')
    self.dut.bt.disable()
    self.dut.bt.waitForAdapterState(android_constants.AdapterState.OFF)
    self.dut.bt.enable()
    self.dut.bt.waitForAdapterState(android_constants.AdapterState.ON)

    self.logger.info('Step 7: Verify the bond is still present.')
    self.assertIn(self.ref.address, self.dut.bt.getBondedDevices())

    self.logger.info('Step 8: Reconnect and verify the link is encrypted.')
    # Workaround: Restore keystore - LTK might be overwritten on LE pairing.
    self.ref.device.keystore = keystore
    self.logger.info('[REF] Reconnect BR/EDR to DUT.')
    ref_classic_conn = await self.ref.device.connect(
        self.dut.address,
        transport=core.BT_BR_EDR_TRANSPORT,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info('[REF] Trigger authentication.')
      await ref_classic_conn.authenticate()
      self.logger.info('[REF] Trigger encryption.')
      await ref_classic_conn.encrypt()

    self.logger.info('[DUT] Wait for encryption changed.')
    await dut_cb.wait_for_event(
        bl4a_api.EncryptionChanged(
            address=self.ref.address,
            transport=android_constants.Transport.CLASSIC,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.assertTrue(ref_classic_conn.is_encrypted)

  async def _le_pair_no_bonding(
      self, ref_conn: device.Connection, dut_cb: bl4a_api.CallbackHandler
  ) -> None:
    # Configure Bumble to not bond
    pairing_delegate = pairing_utils.PairingDelegate(
        auto_accept=True,
        io_capability=pairing.PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT,
    )

    def pairing_config_factory(_: device.Connection) -> pairing.PairingConfig:
      return pairing.PairingConfig(
          sc=True,
          mitm=False,
          bonding=False,  # Crucial: No Bonding
          delegate=pairing_delegate,
      )

    self.ref.device.pairing_config_factory = pairing_config_factory

    ref_pairing_future = asyncio.get_running_loop().create_future()

    @ref_conn.on(ref_conn.EVENT_PAIRING)
    def on_pairing(*args):
      del args
      self.logger.info('[REF] Pairing complete event received on Bumble.')
      ref_pairing_future.set_result(True)

    @ref_conn.on(ref_conn.EVENT_PAIRING_FAILURE)
    def on_pairing_failure(reason):
      self.logger.error(
          '[REF] Pairing failed event received on Bumble. Reason: %s', reason
      )
      ref_pairing_future.set_exception(
          AssertionError(f'Pairing failed on Bumble: {reason}')
      )

    self.logger.info('[REF] Request LE pairing (No Bonding).')
    ref_conn.request_pairing()

    self.logger.info('[DUT] Wait for 1st pairing request.')
    dut_pairing_event = await dut_cb.wait_for_event(
        bl4a_api.PairingRequest(
            address=self.ref.address,
            variant=matcher.ANY,
            pin=matcher.ANY,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.assertEqual(
        dut_pairing_event.variant, android_constants.PairingVariant.CONSENT
    )

    self.logger.info('[DUT] Provide initial pairing confirmation.')
    self.dut.bt.setPairingConfirmation(self.ref.address, True)

    self.logger.info('[DUT] Wait for 2nd pairing request.')
    dut_pairing_event2 = await dut_cb.wait_for_event(
        bl4a_api.PairingRequest(
            address=self.ref.address,
            variant=matcher.ANY,
            pin=matcher.ANY,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.assertEqual(
        dut_pairing_event2.variant, android_constants.PairingVariant.CONSENT
    )

    self.logger.info('[REF] Wait for pairing event on Bumble.')
    ref_pairing_event = await asyncio.wait_for(
        pairing_delegate.pairing_events.get(),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.assertEqual(
        ref_pairing_event.variant, pairing_utils.PairingVariant.JUST_WORK
    )

    self.logger.info('[DUT] Accept 2nd pairing.')
    self.dut.bt.setPairingConfirmation(self.ref.address, True)

    self.logger.info('[REF] Accept pairing.')
    pairing_delegate.pairing_answers.put_nowait(True)

    self.logger.info('[REF] Wait for pairing to complete.')
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await ref_pairing_future

  async def test_security_request_mitm_bit_after_unauthenticated_bonding(
      self,
  ) -> None:
    """Tests that SMP Security Request does not have MITM bit set after unauthenticated bonding.

    This test verifies that after an unauthenticated LE bond, a subsequent
    Security Request from the DUT does not have the MITM bit set. If the DUT
    requests MITM protection when the existing bond is unauthenticated, it
    forces the peer to initiate re-pairing with MITM (e.g., Numeric Comparison),
    which may fail on headless devices or devices without UI.

    See b/500882769 for more details.
    """
    # 1. Bond DUT and REF using unauthenticated SMP pairing (Just Works)
    pairing_delegate = pairing_utils.PairingDelegate(
        auto_accept=True,
        io_capability=pairing.PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT,
    )

    def pairing_config_factory(
        connection: device.Connection,
    ) -> pairing.PairingConfig:
      del connection
      return pairing.PairingConfig(
          sc=True,
          mitm=False,  # Unauthenticated
          bonding=True,
          delegate=pairing_delegate,
      )

    self.ref.device.pairing_config_factory = pairing_config_factory

    dut_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(dut_cb)

    self.logger.info('[DUT] Start pairing.')
    ref_dut_acl = await self._make_outgoing_connection(
        _AddressType.PUBLIC, create_bond=True
    )

    self.logger.info('[DUT] Wait for pairing request.')
    dut_pairing_event = await dut_cb.wait_for_event(
        bl4a_api.PairingRequest(
            address=self.ref.address,
            variant=matcher.ANY,
            pin=matcher.ANY,
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )
    self.assertEqual(dut_pairing_event.variant, _AndroidPairingVariant.CONSENT)

    self.logger.info('[DUT] Provide pairing confirmation.')
    self.dut.bt.setPairingConfirmation(self.ref.address, True)

    self.logger.info('[REF] Wait for pairing event on Bumble.')
    await asyncio.wait_for(
        pairing_delegate.pairing_events.get(),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )
    self.logger.info('[REF] Provide pairing confirmation.')
    pairing_delegate.pairing_answers.put_nowait(True)

    self.logger.info('[DUT] Wait for bond state changed to BONDED.')
    bond_state_changed_event = await dut_cb.wait_for_event(
        bl4a_api.BondStateChanged(
            address=self.ref.address,
            state=matcher.any_of(*_TERMINATED_BOND_STATES),
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )
    self.assertEqual(
        bond_state_changed_event.state, android_constants.BondState.BONDED
    )

    # 2. Disconnect
    self.logger.info('[REF] Disconnect.')
    async with self.assert_not_timeout(_DEFAULT_SETUP_TIMEOUT_SECONDS):
      await ref_dut_acl.disconnect()

    self.logger.info('[DUT] Wait for ACL disconnected.')
    await dut_cb.wait_for_event(
        bl4a_api.AclDisconnected(
            address=self.ref.address,
            transport=android_constants.Transport.LE,
        ),
        timeout=_DEFAULT_SETUP_TIMEOUT_SECONDS,
    )

    # 3. REF connects to DUT in LE central role
    security_request_future: asyncio.Future[smp.AuthReq] = (
        asyncio.get_running_loop().create_future()
    )

    @self.ref.device.on(self.ref.device.EVENT_CONNECTION)
    def on_connection(connection: device.Connection) -> None:
      self.logger.info('[REF] New connection established.')

      @connection.on(connection.EVENT_SECURITY_REQUEST)
      def on_security_request(auth_req: smp.AuthReq) -> None:
        self.logger.info(
            '[REF] Received Security Request with auth_req: %r', auth_req
        )
        if not security_request_future.done():
          security_request_future.set_result(auth_req)

    self.logger.info('[REF] Connect to DUT (incoming to DUT).')
    await self._make_incoming_connection(_AddressType.PUBLIC)

    # 4. Expect DUT to send Security Request, and verify MITM bit is NOT set.
    self.logger.info('[REF] Wait for Security Request.')
    async with self.assert_not_timeout(_DEFAULT_SETUP_TIMEOUT_SECONDS):
      auth_req = await security_request_future

    # Verify MITM bit is NOT set
    self.logger.info('Verifying MITM bit in Security Request.')
    self.assertNotIn(smp.AuthReq.MITM, auth_req, 'MITM bit should NOT be set')


if __name__ == '__main__':
  test_runner.main()
