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

"""Tests Bluetooth Autonomous Repairing."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import contextlib
import enum
import itertools
from typing import Any, override
from unittest import mock
import uuid

from bumble import a2dp
from bumble import core
from bumble import device
from bumble import gatt
from bumble import hci
from bumble import keys
from bumble import l2cap
from bumble import pairing
from bumble import rfcomm
from bumble import smp
from mobly import test_runner
from mobly import signals

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import pairing as pairing_utils

_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_DEFAULT_ACL_DISCONNECTION_TIMEOUT_SECONDS = 60.0
_RFCOMM_UUID = "130c8436-15ac-4d08-aa60-595af4547e8d"
_RFCOMM_SERVICE_RECORD_HANDLE = 1
_DEFAUILT_ADVERTISING_PARAMETERS = device.AdvertisingParameters(
    own_address_type=hci.OwnAddressType.PUBLIC,
    primary_advertising_interval_min=20,
    primary_advertising_interval_max=20,
)
_DEFAULT_KEY_DISTRIBUTION = (
    pairing.PairingDelegate.KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY
    | pairing.PairingDelegate.KeyDistribution.DISTRIBUTE_IDENTITY_KEY
    | pairing.PairingDelegate.KeyDistribution.DISTRIBUTE_LINK_KEY
)


class TestVariant(enum.Enum):
  ACCEPT = "accept"
  REJECTED = "rejected"
  DISCONNECTED = "disconnected"
  NOT_RESPONDED = "not_responded"
  ENCRYPTION_FAILED = "encryption_failed"


_Role = hci.Role
_IoCapability = pairing.PairingDelegate.IoCapability


class AutonomousRepairingTest(navi_test_base.TwoDevicesTestBase):
  """Test Bluetooth Autonomous Repairing."""

  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.is_watch:
      raise signals.TestAbortClass("This test is not supported on watches.")

  def _check_bond_on_dut(
      self,
      transports: Iterable[android_constants.Transport] = (
          android_constants.Transport.CLASSIC,
          android_constants.Transport.LE,
      ),
  ) -> None:
    """Checks LE and Classic bonds on DUT."""
    for transport in transports:
      self.assertIsNotNone(
          self.dut.bt.getBondStatus(self.ref.address, transport),
          f"[DUT] No {transport.name} bond found.",
      )

  async def _check_keys_on_ref(
      self, link_key: bool = True, check_ltk: bool = True
  ) -> None:
    """Checks link key and LTK on REF."""
    if not self.ref.device.keystore:
      self.fail("[REF] Keystore is not initialized.")

    bumble_keys = await self.ref.device.keystore.get(f"{self.dut.address}/P")
    assert bumble_keys is not None, f"No keys found for {self.dut.address}/P"
    if link_key:
      self.assertIsNotNone(bumble_keys.link_key, "No link key found.")
    if check_ltk:
      self.assertIsNotNone(bumble_keys.ltk, "No LTK found.")

  async def _delete_keys_on_ref(self) -> None:
    if not self.ref.device.keystore:
      self.fail("[REF] Keystore is not initialized.")

    self.logger.info("[REF] Delete all keys.")
    await self.ref.device.keystore.delete_all()

    self.logger.info("[REF] Clear resolving list in the controller.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await self.ref.device.refresh_resolving_list()

  async def _wait_for_repairing_success(
      self,
      ref_address: str,
      adapter_cb: bl4a_api.CallbackHandler,
      encrypted_transports: Iterable[android_constants.Transport],
      pairing_future: asyncio.Future[dict[str, Any]] | None = None,
      start_advertising: bool = False,
  ) -> None:
    """Waits for bonding events."""
    self.logger.info("[DUT] Wait for bond state change to none.")
    await adapter_cb.wait_for_event(
        bl4a_api.BondStateChanged(
            address=ref_address,
            state=android_constants.BondState.NONE,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    self.logger.info("[DUT] Wait for bond state change to bonding.")
    await adapter_cb.wait_for_event(
        bl4a_api.BondStateChanged(
            address=ref_address,
            state=android_constants.BondState.BONDING,
        )
    )

    for transport in encrypted_transports:
      self.logger.info("[DUT] Wait for %s encryption changed.", transport.name)
      await adapter_cb.wait_for_event(
          bl4a_api.EncryptionChanged(
              address=ref_address,
              transport=transport,
          )
      )

      # Start advertising on REF to establish LE connection for encryption.
      if start_advertising and transport == android_constants.Transport.CLASSIC:
        self.logger.info("[REF] Start advertising")
        await self.ref.device.create_advertising_set(
            advertising_parameters=_DEFAUILT_ADVERTISING_PARAMETERS,
        )

    self.logger.info("[DUT] Wait for bond state change to bonded.")
    await adapter_cb.wait_for_event(
        bl4a_api.BondStateChanged(
            address=ref_address,
            state=android_constants.BondState.BONDED,
        )
    )

    if pairing_future:
      self.logger.info("[REF] Wait for pairing complete.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await pairing_future

  async def _wait_for_repairing_fail(
      self,
      ref_address: str,
      adapter_cb: bl4a_api.CallbackHandler,
      transport: android_constants.Transport,
  ) -> None:
    """Waits for repairing fail events."""
    self.logger.info("[DUT] Wait for key missing.")
    key_missing_event = await adapter_cb.wait_for_event(
        bl4a_api.KeyMissing(
            address=ref_address,
        ),
        timeout=_DEFAULT_ACL_DISCONNECTION_TIMEOUT_SECONDS,
    )

    self.logger.info("[DUT] Wait for ACL disconnection.")
    acl_disconnected_event = await adapter_cb.wait_for_event(
        bl4a_api.AclDisconnected(
            address=ref_address,
            transport=transport,
        ),
    )

    self.assertLess(
        key_missing_event.creation_time,
        acl_disconnected_event.creation_time,
        "[DUT] Key missing event occurred after ACL disconnection.",
    )

    if not self.ref.device.keystore:
      self.fail("[REF] Keystore is not initialized.")

    self.assertIsNone(
        await self.ref.device.keystore.get(f"{self.dut.address}/P")
    )

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()

    self.logger.info("[REF] Setup A2DP record.")
    self.ref.device.sdp_service_records = {
        1: a2dp.make_audio_sink_service_sdp_records(1),
    }

    service_uuid = str(uuid.uuid4())

    self.logger.info("[REF] Add GATT service with UUID: %s", service_uuid)
    self.ref.device.add_service(
        gatt.Service(uuid=service_uuid, characteristics=[])
    )

  @navi_test_base.parameterized(
      *itertools.product(
          [
              TestVariant.ACCEPT,
              TestVariant.REJECTED,
              TestVariant.DISCONNECTED,
              TestVariant.NOT_RESPONDED,
              TestVariant.ENCRYPTION_FAILED,
          ],
          [constants.Direction.OUTGOING, constants.Direction.INCOMING],
      )
  )
  async def test_repairing_classic(
      self,
      variant: TestVariant,
      pairing_direction: constants.Direction,
  ) -> None:
    """Tests re-pairing when the remote device loses the bond over BR/EDR.

    Test steps:
      1. Bond DUT and REF over BR/EDR.
      2. Disconnect from DUT.
      3. Remove the bond on REF.
      4. Initiate connection depending on pairing_direction.
      5. Verify DUT detects bond loss and initiates re-pairing.
      6. Reply based on variant.
      7. [If accepted] Verify REF has the key for DUT.
      8. Verify DUT has the key for REF.

    Args:
      variant: Action to take when a pairing request is received on REF.
      pairing_direction: The direction of the pairing request.
    """
    await self.classic_connect_and_pair()

    await self.disconnect_with_check(
        self.ref.address, android_constants.Transport.CLASSIC
    )

    self._check_bond_on_dut()

    await self._check_keys_on_ref()

    await self._delete_keys_on_ref()

    pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=_IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
        auto_accept=True,
    )

    def pairing_config_factory(
        _: device.Connection,
    ) -> pairing.PairingConfig:
      return pairing.PairingConfig(
          identity_address_type=pairing.PairingConfig.AddressType.PUBLIC,
          delegate=pairing_delegate,
      )

    self.logger.info("[REF] Set pairing config factory.")
    self.ref.device.pairing_config_factory = pairing_config_factory

    adapter_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(adapter_cb)

    auth_task: asyncio.Task[None] | None = None
    ref_dut_acl: device.Connection | None

    if pairing_direction == constants.Direction.OUTGOING:
      self.logger.info("[DUT] Initiate ACL connection from DUT.")
      self.dut.bt.connect(self.ref.address)
    else:
      self.logger.info("[REF] Connect to DUT.")
      ref_dut_acl = await self.ref.device.connect(
          f"{self.dut.address}/P",
          transport=core.BT_BR_EDR_TRANSPORT,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      self.logger.info("[REF] Create bond.")
      auth_task = asyncio.tasks.create_task(ref_dut_acl.authenticate())

    self.logger.info("[DUT] Wait for connection.")
    await adapter_cb.wait_for_event(
        event=bl4a_api.AclConnected(
            address=self.ref.address,
            transport=android_constants.Transport.CLASSIC,
        ),
    )

    self.logger.info("[DUT] Wait for pairing request.")
    await adapter_cb.wait_for_event(
        bl4a_api.PairingRequest(
            address=self.ref.address, variant=mock.ANY, pin=mock.ANY
        )
    )

    self._check_bond_on_dut()

    pairing_future = asyncio.get_running_loop().create_future()

    def on_connection_pairing(ltk: keys.PairingKeys):
      if not pairing_future.done():
        pairing_future.set_result(ltk)

    def on_connection_pairing_failure(reason: int):
      if not pairing_future.done():
        pairing_future.set_exception(
            RuntimeError(f"[REF] Pairing failed with reason code: {reason}")
        )

    ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
        hci.Address(self.dut.address),
        transport=core.PhysicalTransport.BR_EDR,
    )
    if not ref_dut_acl:
      self.fail("[REF] No ACL connection found.")

    ref_dut_acl.once(ref_dut_acl.EVENT_PAIRING, on_connection_pairing)
    ref_dut_acl.once(
        ref_dut_acl.EVENT_PAIRING_FAILURE, on_connection_pairing_failure
    )

    self.logger.info("[REF] Wait for pairing request.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await pairing_delegate.pairing_events.get()

    self.logger.info("[DUT] Accept pairing request.")
    self.dut.bt.setPairingConfirmation(self.ref.address, True)

    ref_accept = variant in (TestVariant.ACCEPT, TestVariant.ENCRYPTION_FAILED)

    match variant:
      case TestVariant.ACCEPT:
        self.logger.info("[REF] Accept pairing request.")
        pairing_delegate.pairing_answers.put_nowait(True)

      case TestVariant.REJECTED:
        self.logger.info("[REF] Reject pairing request.")
        pairing_delegate.pairing_answers.put_nowait(False)

      case TestVariant.NOT_RESPONDED:
        self.logger.info("[REF] No response.")
        # TODO: Remove this once the bug is fixed.
        if self.dut.device.is_emulator:
          self.logger.info("[REF] Disconnect from DUT.")
          await ref_dut_acl.disconnect()

      case TestVariant.DISCONNECTED:
        ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
            hci.Address(self.dut.address),
            transport=core.PhysicalTransport.BR_EDR,
        )
        if not ref_dut_acl:
          self.fail("[REF] No ACL connection found.")

        self.logger.info("[REF] Disconnect from DUT.")
        await ref_dut_acl.disconnect()

      case TestVariant.ENCRYPTION_FAILED:
        self.logger.info("[REF] Remove link key provider to fail encryption.")
        self.ref.device.host.link_key_provider = None

        self.logger.info("[REF] Accept pairing request.")
        pairing_delegate.pairing_answers.put_nowait(True)

    if auth_task:
      self.logger.info("[REF] Wait for authentication complete.")
      expected_errors = (
          []
          if variant == TestVariant.ACCEPT
          else [hci.HCI_Error, asyncio.CancelledError]
      )
      with contextlib.suppress(*expected_errors):
        async with self.assert_not_timeout(
            _DEFAULT_ACL_DISCONNECTION_TIMEOUT_SECONDS
        ):
          await auth_task

    if ref_accept:
      await self._wait_for_repairing_success(
          ref_address=self.ref.address,
          adapter_cb=adapter_cb,
          encrypted_transports=[
              android_constants.Transport.CLASSIC,
              android_constants.Transport.LE,
          ],
          pairing_future=pairing_future,
          start_advertising=True,
      )

      await self._check_keys_on_ref()

    else:
      await self._wait_for_repairing_fail(
          ref_address=self.ref.address,
          adapter_cb=adapter_cb,
          transport=android_constants.Transport.CLASSIC,
      )

    self._check_bond_on_dut()

  @navi_test_base.parameterized(*[
      (variant, pairing_direction)
      for variant, pairing_direction in itertools.product(
          [
              TestVariant.ACCEPT,
              TestVariant.REJECTED,
              TestVariant.DISCONNECTED,
              TestVariant.NOT_RESPONDED,
              TestVariant.ENCRYPTION_FAILED,
          ],
          [constants.Direction.OUTGOING, constants.Direction.INCOMING],
      )
      if not (
          variant == TestVariant.ENCRYPTION_FAILED
          and pairing_direction == constants.Direction.INCOMING
      )
  ])
  async def test_repairing_le(
      self,
      variant: TestVariant,
      pairing_direction: constants.Direction,
  ) -> None:
    """Tests re-pairing when the remote device loses the bond over LE.

    Test steps:
      1. Bond DUT and REF over LE.
      2. Disconnect from DUT.
      3. Remove the bond on REF.
      4. Initiate connection depending on pairing_direction.
      5. Verify DUT detects bond loss and initiates re-pairing.
      6. Reply based on variant.
      7. [If accepted] Verify REF has the key for DUT.
      8. Verify DUT has the key for REF.

    Args:
      variant: Action to take when a pairing request is received on REF.
      pairing_direction: The direction of the pairing request.
    """
    await self.le_connect_and_pair(
        ref_address_type=hci.OwnAddressType.PUBLIC,
        delegate=pairing.PairingDelegate(
            io_capability=_IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
            local_initiator_key_distribution=_DEFAULT_KEY_DISTRIBUTION,
            local_responder_key_distribution=_DEFAULT_KEY_DISTRIBUTION,
        ),
    )

    await self.disconnect_with_check(
        self.ref.address, android_constants.Transport.LE
    )

    self._check_bond_on_dut()

    await self._check_keys_on_ref()

    await self._delete_keys_on_ref()

    pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=_IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
        auto_accept=True,
        local_initiator_key_distribution=_DEFAULT_KEY_DISTRIBUTION,
        local_responder_key_distribution=_DEFAULT_KEY_DISTRIBUTION,
    )

    def pairing_config_factory(
        _: device.Connection,
    ) -> pairing.PairingConfig:
      return pairing.PairingConfig(
          identity_address_type=pairing.PairingConfig.AddressType.PUBLIC,
          delegate=pairing_delegate,
      )

    self.logger.info("[REF] Set pairing config factory.")
    self.ref.device.pairing_config_factory = pairing_config_factory

    adapter_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(adapter_cb)

    pair_task: asyncio.Task[None] | None = None

    if pairing_direction == constants.Direction.OUTGOING:
      self.logger.info("[REF] Start advertising")
      await self.ref.device.create_advertising_set(
          advertising_parameters=_DEFAUILT_ADVERTISING_PARAMETERS,
      )

      self.logger.info("[DUT] Initiate ACL connection from DUT.")
      gatt_client = await self.dut.bl4a.connect_gatt_client(
          address=self.ref.address,
          transport=android_constants.Transport.LE,
      )
      self.test_case_context.push(gatt_client)
    else:
      ref_dut_acl = await self.connect_le_from_ref(
          dut_address_type=android_constants.AddressTypeStatus.PUBLIC,
          ref_address_type=hci.OwnAddressType.PUBLIC,
          wait_for_dut_connected=False,
      )

      self.logger.info("[REF] Pair.")
      pair_task = asyncio.create_task(ref_dut_acl.pair())

    self.logger.info("[DUT] Wait for connection.")
    await adapter_cb.wait_for_event(
        event=bl4a_api.AclConnected(
            address=self.ref.address,
            transport=android_constants.Transport.LE,
        ),
    )

    self.logger.info("[DUT] Wait for pairing request.")
    await adapter_cb.wait_for_event(
        bl4a_api.PairingRequest(
            address=self.ref.address, variant=mock.ANY, pin=mock.ANY
        )
    )

    self._check_bond_on_dut()

    self.logger.info("[REF] Wait for pairing request.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await pairing_delegate.pairing_events.get()

    self.logger.info("[DUT] Accept pairing request.")
    self.dut.bt.setPairingConfirmation(self.ref.address, True)

    ref_accept = variant == TestVariant.ACCEPT

    match variant:
      case TestVariant.ACCEPT:
        self.logger.info("[REF] Accept pairing request.")
        pairing_delegate.pairing_answers.put_nowait(True)

      case TestVariant.REJECTED:
        self.logger.info("[REF] Reject pairing request.")
        pairing_delegate.pairing_answers.put_nowait(False)

      case TestVariant.NOT_RESPONDED:
        self.logger.info("[REF] No response.")

      case TestVariant.DISCONNECTED:
        # Find the ACL connection from REF to DUT. Since DUT has RPA, we need
        # to find the connection from the connection list.
        ref_dut_acl = next(
            (
                c
                for c in self.ref.device.connections.values()
                if c.transport == core.PhysicalTransport.LE
            ),
            None,
        )
        if not ref_dut_acl:
          self.fail("[REF] No ACL connection found.")

        self.logger.info("[REF] Disconnect from DUT.")
        await ref_dut_acl.disconnect()

      case TestVariant.ENCRYPTION_FAILED:
        self.logger.info("[REF] Remove LTK provider to fail encryption.")
        self.ref.device.host.long_term_key_provider = None

        self.logger.info("[REF] Accept pairing request.")
        pairing_delegate.pairing_answers.put_nowait(True)

    if ref_accept:
      await self._wait_for_repairing_success(
          ref_address=self.ref.address,
          adapter_cb=adapter_cb,
          encrypted_transports=[
              android_constants.Transport.LE,
              android_constants.Transport.CLASSIC,
          ],
      )

      await self._check_keys_on_ref()
    else:
      await self._wait_for_repairing_fail(
          ref_address=self.ref.address,
          adapter_cb=adapter_cb,
          transport=android_constants.Transport.LE,
      )

    self._check_bond_on_dut()

    if pair_task:
      self.logger.info("[REF] Wait pairing complete.")
      if variant == TestVariant.ACCEPT:
        await pair_task
      else:
        with self.assertRaises((core.ProtocolError, asyncio.CancelledError)):
          await pair_task

  @navi_test_base.parameterized(
      constants.Direction.OUTGOING, constants.Direction.INCOMING
  )
  async def test_repairing_classic_insecure(
      self, pairing_direction: constants.Direction
  ) -> None:
    """Tests re-pairing over an insecure BR/EDR socket (RFCOMM).

    Test steps:
      1. Bond DUT and REF over BR/EDR.
      2. Disconnect from DUT.
      3. Remove the bond on REF.
      4. Initiate an insecure RFCOMM connection depending on pairing_direction.
      5. Verify DUT detects bond loss and initiates re-pairing.
      6. Accept pairing requests on DUT and REF.
      7. Verify REF has the key for DUT.

    Args:
      pairing_direction: The direction of the pairing request.
    """
    await self.classic_connect_and_pair()

    await self.disconnect_with_check(
        self.ref.address, android_constants.Transport.CLASSIC
    )

    self._check_bond_on_dut()

    await self._check_keys_on_ref()

    await self._delete_keys_on_ref()

    pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=_IoCapability.NO_OUTPUT_NO_INPUT,
        auto_accept=True,
    )

    def pairing_config_factory(
        _: device.Connection,
    ) -> pairing.PairingConfig:
      return pairing.PairingConfig(
          identity_address_type=pairing.PairingConfig.AddressType.PUBLIC,
          delegate=pairing_delegate,
      )

    self.logger.info("[REF] Set pairing config factory.")
    self.ref.device.pairing_config_factory = pairing_config_factory

    self.logger.info("[REF] Deregister SMP fixed channel to disable CTKD.")
    self.ref.device.l2cap_channel_manager.deregister_fixed_channel(
        smp.SMP_BR_CID
    )

    adapter_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(adapter_cb)

    auth_task: asyncio.Task[None] | None = None
    ref_dut_acl: device.Connection | None = None
    server: bl4a_api.RfcommServerSocket | None = None

    if pairing_direction == constants.Direction.OUTGOING:
      ref_accept_future = asyncio.get_running_loop().create_future()

      self.logger.info("[REF] Listen for insecure RFCOMM connection.")
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

      dut_socket = self.dut.bl4a.create_rfcomm_channel_async(
          address=self.ref.address,
          secure=False,
          uuid=_RFCOMM_UUID,
      )
      self.test_case_context.push_async_exit(dut_socket)
    else:
      self.logger.info("[DUT] Listen RFCOMM.")
      server = self.dut.bl4a.create_rfcomm_server(
          _RFCOMM_UUID,
          secure=False,
      )
      self.test_case_context.push(server)

      self.logger.info("[REF] Connect to DUT.")
      ref_dut_acl = await self.ref.device.connect(
          self.dut.address,
          transport=core.BT_BR_EDR_TRANSPORT,
      )

      self.logger.info("[REF] Find RFCOMM channel via SDP.")
      found_channel = await rfcomm.find_rfcomm_channel_with_uuid(
          ref_dut_acl, _RFCOMM_UUID
      )
      if not found_channel:
        self.fail("Failed to find RFCOMM channel with UUID.")
      channel = found_channel

      self.logger.info("[REF] Wait for authentication.")
      auth_task = asyncio.create_task(ref_dut_acl.authenticate())

    self.logger.info("[DUT] Wait for ACL connection.")
    await adapter_cb.wait_for_event(
        event=bl4a_api.AclConnected(
            address=self.ref.address,
            transport=android_constants.Transport.CLASSIC,
        ),
    )

    self.logger.info("[DUT] Wait for pairing request.")
    await adapter_cb.wait_for_event(
        bl4a_api.PairingRequest(
            address=self.ref.address, variant=mock.ANY, pin=mock.ANY
        )
    )

    self.logger.info("[REF] Wait for pairing request.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await pairing_delegate.pairing_events.get()

    self.logger.info("[DUT] Accept pairing request.")
    self.dut.bt.setPairingConfirmation(self.ref.address, True)

    self.logger.info("[REF] Accept pairing request.")
    pairing_delegate.pairing_answers.put_nowait(True)

    if auth_task:
      self.logger.info("[REF] Wait for authentication complete.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await auth_task

    if pairing_direction == constants.Direction.INCOMING:
      assert ref_dut_acl is not None, "No ACL connection found."
      assert server is not None, "No RFCOMM server found."

      self.logger.info("[REF] Encrypt ACL connection.")
      await ref_dut_acl.encrypt()

      self.logger.info("[REF] Connect RFCOMM channel to DUT.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        ref_rfcomm = await rfcomm.Client(ref_dut_acl).start()

      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await asyncio.gather(
            ref_rfcomm.open_dlc(channel),
            server.accept(),
        )

    await self._wait_for_repairing_success(
        ref_address=self.ref.address,
        adapter_cb=adapter_cb,
        encrypted_transports=[android_constants.Transport.CLASSIC],
    )

    self._check_bond_on_dut()

    await self._check_keys_on_ref(check_ltk=False)

  @navi_test_base.parameterized(
      constants.Direction.OUTGOING, constants.Direction.INCOMING
  )
  async def test_repairing_le_insecure(
      self, pairing_direction: constants.Direction
  ) -> None:
    """Tests re-pairing over an insecure LE socket (L2CAP).

    Test steps:
      1. Bond DUT and REF over LE.
      2. Disconnect from DUT.
      3. Remove the bond on REF.
      4. Initiate an insecure L2CAP connection depending on pairing_direction.
      5. Verify DUT detects bond loss and initiates re-pairing.
      6. Accept pairing requests on DUT and REF.
      7. Verify REF has the key for DUT.

    Args:
      pairing_direction: The direction of the pairing request.
    """
    await self.le_connect_and_pair(
        ref_address_type=hci.OwnAddressType.PUBLIC,
        delegate=pairing.PairingDelegate(
            io_capability=_IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
        ),
    )

    await self.disconnect_with_check(
        self.ref.address, android_constants.Transport.LE
    )

    self._check_bond_on_dut(transports=[android_constants.Transport.LE])

    await self._check_keys_on_ref(link_key=False)

    await self._delete_keys_on_ref()

    pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=_IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
        auto_accept=True,
    )

    def pairing_config_factory(
        _: device.Connection,
    ) -> pairing.PairingConfig:
      return pairing.PairingConfig(
          identity_address_type=pairing.PairingConfig.AddressType.PUBLIC,
          delegate=pairing_delegate,
      )

    self.logger.info("[REF] Set pairing config factory.")
    self.ref.device.pairing_config_factory = pairing_config_factory

    self.logger.info("[REF] Deregister SMP fixed channel to disable CTKD.")
    self.ref.device.l2cap_channel_manager.deregister_fixed_channel(
        smp.SMP_BR_CID
    )

    adapter_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(adapter_cb)

    pair_task: asyncio.Task[None] | None = None

    if pairing_direction == constants.Direction.OUTGOING:
      ref_accept_future = asyncio.get_running_loop().create_future()

      server = self.ref.device.create_l2cap_server(
          spec=l2cap.LeCreditBasedChannelSpec(),
          handler=ref_accept_future.set_result,
      )
      self.logger.info("[REF] Listen L2CAP on PSM %d", server.psm)

      self.logger.info("[REF] Start advertising.")
      await self.ref.device.start_advertising(
          own_address_type=hci.OwnAddressType.PUBLIC
      )

      self.logger.info("[DUT] Connect L2CAP channel to REF.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await asyncio.gather(
            ref_accept_future,
            self.dut.bl4a.create_l2cap_channel(
                address=self.ref.address,
                secure=False,
                psm=server.psm,
                address_type=android_constants.AddressTypeStatus.PUBLIC,
            ),
        )
    else:
      dut_server = self.dut.bl4a.create_l2cap_server(secure=False)
      self.test_case_context.push(dut_server)
      self.logger.info("[DUT] Listen L2CAP on PSM %d", dut_server.psm)

      def on_connection(connection: device.Connection) -> None:
        if connection.transport == core.PhysicalTransport.LE:

          def on_security_request(auth_req: smp.AuthReq) -> None:
            del auth_req  # Unused.

            self.logger.info("[REF] Initiating pairing.")
            nonlocal pair_task
            pair_task = asyncio.create_task(self.ref.device.pair(connection))

          connection.on(connection.EVENT_SECURITY_REQUEST, on_security_request)

      self.ref.device.on(self.ref.device.EVENT_CONNECTION, on_connection)

      try:
        ref_dut_acl = await self.connect_le_from_ref(
            dut_address_type=android_constants.AddressTypeStatus.PUBLIC,
            ref_address_type=hci.OwnAddressType.PUBLIC,
            wait_for_dut_connected=False,
        )
      finally:
        self.ref.device.remove_listener(
            self.ref.device.EVENT_CONNECTION, on_connection
        )

      self.logger.info("[REF] Connect L2CAP channel to DUT.")
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await asyncio.gather(
            ref_dut_acl.create_l2cap_channel(
                l2cap.LeCreditBasedChannelSpec(psm=dut_server.psm)
            ),
            dut_server.accept(),
        )

    self.logger.info("[DUT] Wait for ACL connection.")
    await adapter_cb.wait_for_event(
        event=bl4a_api.AclConnected(
            address=self.ref.address,
            transport=android_constants.Transport.LE,
        ),
    )

    self.logger.info("[DUT] Wait for pairing request.")
    await adapter_cb.wait_for_event(
        bl4a_api.PairingRequest(
            address=self.ref.address, variant=mock.ANY, pin=mock.ANY
        )
    )

    self.logger.info("[REF] Wait for pairing request.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await pairing_delegate.pairing_events.get()

    self.logger.info("[DUT] Accept pairing request.")
    self.dut.bt.setPairingConfirmation(self.ref.address, True)

    self.logger.info("[REF] Accept pairing request.")
    pairing_delegate.pairing_answers.put_nowait(True)

    await self._wait_for_repairing_success(
        ref_address=self.ref.address,
        adapter_cb=adapter_cb,
        encrypted_transports=[android_constants.Transport.LE],
    )

    self._check_bond_on_dut(transports=[android_constants.Transport.LE])

    await self._check_keys_on_ref(link_key=False)

    if pair_task:
      self.logger.info("[REF] Wait pairing complete.")
      await pair_task


if __name__ == "__main__":
  test_runner.main()
