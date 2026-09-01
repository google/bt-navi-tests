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
import dataclasses
import enum
import functools
import itertools
import secrets
from typing import override

from bumble import core
from bumble import device as device_lib
from bumble import hci
from bumble import hfp
from bumble import keys
from bumble import pairing
from mobly import test_runner

from navi.bumble_ext import hfp as hfp_ext
from navi.tests import navi_test_base
from navi.tests.firmware import test_base
from navi.utils import constants
from navi.utils import pairing as pairing_utils

_DEFAULT_TIMEOUT_SECONDS = 10.0

_IoCapability = pairing.PairingDelegate.IoCapability
_PairingVariant = pairing_utils.PairingVariant


@dataclasses.dataclass(frozen=True)
class SniffModeParams:
  max_interval: int
  min_interval: int
  sniff_attempt: int
  sniff_timeout: int


# From packages/modules/Bluetooth/system/bta/dm/bta_dm_cfg.cc.
_SNIFF_MODE_PARAMS = (
    SniffModeParams(
        max_interval=800,
        min_interval=400,
        sniff_attempt=4,
        sniff_timeout=1,
    ),
    SniffModeParams(
        max_interval=400,
        min_interval=200,
        sniff_attempt=4,
        sniff_timeout=1,
    ),
    SniffModeParams(
        max_interval=54,
        min_interval=30,
        sniff_attempt=4,
        sniff_timeout=1,
    ),
    SniffModeParams(
        max_interval=150,
        min_interval=50,
        sniff_attempt=4,
        sniff_timeout=1,
    ),
    SniffModeParams(
        max_interval=18,
        min_interval=10,
        sniff_attempt=4,
        sniff_timeout=1,
    ),
    SniffModeParams(
        max_interval=36,
        min_interval=30,
        sniff_attempt=2,
        sniff_timeout=0,
    ),
    SniffModeParams(
        max_interval=18,
        min_interval=14,
        sniff_attempt=1,
        sniff_timeout=0,
    ),
)


class TestVariant(enum.Enum):
  ACCEPT = 'accept'
  REJECT = 'reject'
  REJECTED = 'rejected'


class _LegacyPairingDelegate(pairing.PairingDelegate):

  def __init__(
      self, io_capability: pairing.PairingDelegate.IoCapability, pin: str = ''
  ) -> None:
    super().__init__(io_capability=io_capability)
    self.pin = pin

  @override
  async def get_string(self, max_length: int) -> str:
    return self.pin


class ClassicTest(test_base.DualDeviceTestBase):
  """Tests for Classic connection."""

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    response = await self.dut.device.send_sync_command(
        hci.HCI_Read_Local_Supported_Codecs_Command()
    )
    self.dut_supported_codecs = set(response.standard_codec_ids)
    response = await self.ref.device.send_sync_command(
        hci.HCI_Read_Local_Supported_Codecs_Command()
    )
    self.ref_supported_codecs = set(response.standard_codec_ids)

    self.logger.info('dut_supported_codecs: %s', self.dut_supported_codecs)
    self.logger.info('ref_supported_codecs: %s', self.ref_supported_codecs)

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_connect(
      self, direction: constants.Direction
  ) -> tuple[device_lib.Connection, device_lib.Connection]:
    """Tests connecting to a remote device."""

    if direction == constants.Direction.OUTGOING:
      central, peripheral = self.dut.device, self.ref.device
    else:
      central, peripheral = self.ref.device, self.dut.device
    return await self.create_connection(
        central,
        peripheral,
        core.PhysicalTransport.BR_EDR,
    )

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_inquiry(self, direction: constants.Direction) -> None:
    """Tests inquiry."""

    if direction == constants.Direction.OUTGOING:
      central, peripheral = self.dut.device, self.ref.device
    else:
      central, peripheral = self.ref.device, self.dut.device

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info('[Peripheral] Set discoverable.')
      await peripheral.set_discoverable(True)
      self.logger.info('[Central] Look for discoverable devices.')
      device_found = asyncio.Event()

      @central.on(central.EVENT_INQUIRY_RESULT)
      def on_inquiry_result(
          address: hci.Address,
          class_of_device: int,
          data: device_lib.AdvertisingData,
          rssi: int,
      ) -> None:
        del class_of_device, data, rssi
        if address == peripheral.public_address:
          device_found.set()

      self.logger.info('[Central] Start discovery.')
      await central.start_discovery()
      self.logger.info('[Central] Waiting for device found.')
      await device_found.wait()

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_remote_name_request(
      self, direction: constants.Direction
  ) -> None:
    """Tests remote name request."""

    if direction == constants.Direction.OUTGOING:
      central, peripheral = self.dut.device, self.ref.device
    else:
      central, peripheral = self.ref.device, self.dut.device

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      name = await central.request_remote_name(peripheral.public_address)
      self.assertEqual(name, peripheral.name)

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_get_remote_features(
      self, direction: constants.Direction
  ) -> None:
    """Tests get remote features."""

    connections = await self.test_connect(direction)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info('[Central] Get remote features.')
      await connections[0].get_remote_classic_features()
      self.logger.info('[Peripheral] Get remote features.')
      await connections[1].get_remote_classic_features()

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_authentication(
      self, direction: constants.Direction
  ) -> tuple[device_lib.Connection, device_lib.Connection]:
    """Tests authentication."""
    connections = await self.test_connect(direction)

    # Inject pairing keys to the devices.
    pairing_keys = keys.PairingKeys()
    pairing_keys.link_key = keys.PairingKeys.Key(
        secrets.token_bytes(16), authenticated=True
    )

    for connection in connections:
      await connection.device.update_keys(
          str(connection.peer_address), pairing_keys
      )
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info('Authenticating connection.')
      await connections[0].authenticate()

    return connections

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_encryption(self, direction: constants.Direction) -> None:
    """Tests encryption."""
    connections = await self.test_authentication(direction)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      encryption_results: list[asyncio.Future[None]] = [
          asyncio.get_running_loop().create_future() for _ in range(2)
      ]
      for connection, encryption_result in zip(connections, encryption_results):
        connection.once(
            connection.EVENT_CONNECTION_ENCRYPTION_CHANGE,
            functools.partial(encryption_result.set_result, None),
        )
        connection.once(
            connection.EVENT_CONNECTION_ENCRYPTION_FAILURE,
            functools.partial(
                lambda result, reason: result.set_exception(
                    hci.HCI_Error(reason)
                ),
                encryption_result,
            ),
        )
      self.logger.info('Encrypting connection.')
      await connections[0].encrypt()

      self.logger.info('Waiting for encryption results.')
      await asyncio.gather(*encryption_results)

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_switch_role(self, direction: constants.Direction) -> None:
    """Tests switch role."""

    self.logger.info('Allow role switch.')
    for device in self._devices:
      await device.device.send_sync_command(
          hci.HCI_Write_Default_Link_Policy_Settings_Command(
              default_link_policy_settings=0x01
          )
      )

    connections = await self.create_connection(
        self.dut.device,
        self.ref.device,
        core.PhysicalTransport.BR_EDR,
    )

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      role_change_results: list[asyncio.Future[hci.Role]] = [
          asyncio.get_running_loop().create_future() for _ in range(2)
      ]
      for connection, role_change_result in zip(
          connections, role_change_results
      ):
        connection.once(
            connection.EVENT_ROLE_CHANGE, role_change_result.set_result
        )
        connection.once(
            connection.EVENT_ROLE_CHANGE_FAILURE,
            functools.partial(
                lambda result, reason: result.set_exception(
                    hci.HCI_Error(reason)
                ),
                role_change_result,
            ),
        )

      if direction == constants.Direction.OUTGOING:
        self.logger.info('Switching role to peripheral.')
        await connections[0].switch_role(hci.Role.PERIPHERAL)
      else:  # direction == constants.Direction.INCOMING
        self.logger.info('Switching role to central.')
        await connections[1].switch_role(hci.Role.CENTRAL)

      self.logger.info('Waiting for role change results.')
      await asyncio.gather(*role_change_results)

  async def test_send_acl_data(self) -> None:
    """Tests switch role."""
    connections = await self.create_connection(
        self.dut.device,
        self.ref.device,
        core.PhysicalTransport.BR_EDR,
    )

    cid = 33
    data_size = 4096
    sample_data = bytes([i % 256 for i in range(data_size)])

    sinks = [asyncio.Queue[bytes]() for _ in range(2)]
    connections[0].device.l2cap_channel_manager.register_fixed_channel(
        cid, lambda _, data: sinks[0].put_nowait(data)
    )
    connections[1].device.l2cap_channel_manager.register_fixed_channel(
        cid, lambda _, data: sinks[1].put_nowait(data)
    )

    self.logger.info('Enqueuing ACL data.')
    connections[0].send_l2cap_pdu(cid, sample_data)
    connections[1].send_l2cap_pdu(cid, sample_data)
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info('Waiting for ACL data received.')
      received_datas = (bytearray(), bytearray())
      while len(received_datas[0]) < data_size:
        received_datas[0].extend(await sinks[0].get())
      while len(received_datas[1]) < data_size:
        received_datas[1].extend(await sinks[1].get())
      self.assertEqual(received_datas[0], sample_data)
      self.assertEqual(received_datas[1], sample_data)

  @navi_test_base.parameterized(
      *itertools.product(constants.Direction, _SNIFF_MODE_PARAMS)
  )
  async def test_sniff_mode(
      self, direction: constants.Direction, sniff_mode_param: SniffModeParams
  ) -> None:
    """Tests sniff mode."""
    if self.is_emulator:
      self.skipTest('Sniff mode is not supported by Rootcanal.')
    # Enable sniff mode on both devices.
    for device in self._devices:
      await device.device.send_sync_command(
          hci.HCI_Write_Default_Link_Policy_Settings_Command(
              default_link_policy_settings=1 << 2,
          )
      )

    connections = await self.test_connect(direction)

    def register_mode_change_callbacks(
        connections: list[device_lib.Connection],
    ) -> list[asyncio.Future[None]]:
      mode_change_results: list[asyncio.Future[None]] = [
          asyncio.get_running_loop().create_future() for _ in range(2)
      ]
      for connection, mode_change_result in zip(
          connections, mode_change_results
      ):
        connection.once(
            connection.EVENT_MODE_CHANGE,
            functools.partial(mode_change_result.set_result, None),
        )
        connection.once(
            connection.EVENT_MODE_CHANGE_FAILURE,
            mode_change_result.set_result,
        )
      return mode_change_results

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      mode_change_results = register_mode_change_callbacks(connections)
      self.logger.info('Entering sniff mode.')
      await connections[0].device.send_async_command(
          hci.HCI_Sniff_Mode_Command(
              connection_handle=connections[0].handle,
              sniff_max_interval=sniff_mode_param.max_interval,
              sniff_min_interval=sniff_mode_param.min_interval,
              sniff_attempt=sniff_mode_param.sniff_attempt,
              sniff_timeout=sniff_mode_param.sniff_timeout,
          )
      )

      self.logger.info('Waiting for mode change results.')
      self.assertSequenceEqual(
          await asyncio.gather(*mode_change_results),
          [None, None],
          msg='Failed to enter sniff mode.',
      )

      for connection in connections:
        self.assertEqual(
            connection.classic_mode,
            hci.HCI_Mode_Change_Event.Mode.SNIFF,
            msg='Connection is not in sniff mode.',
        )
        self.assertGreaterEqual(
            connection.classic_interval,
            sniff_mode_param.min_interval,
            msg='Connection mode interval is less than min interval.',
        )
        self.assertLessEqual(
            connection.classic_interval,
            sniff_mode_param.max_interval,
            msg='Connection mode interval is greater than max interval.',
        )

      mode_change_results = register_mode_change_callbacks(connections)

      self.logger.info('Exiting sniff mode.')
      await connections[0].device.send_async_command(
          hci.HCI_Exit_Sniff_Mode_Command(
              connection_handle=connections[0].handle,
          )
      )
      self.logger.info('Waiting for mode change results.')
      self.assertSequenceEqual(
          await asyncio.gather(*mode_change_results),
          [None, None],
          msg='Failed to exit sniff mode.',
      )

      for connection in connections:
        self.assertEqual(
            connection.classic_mode,
            hci.HCI_Mode_Change_Event.Mode.ACTIVE,
            msg='Connection is not in active mode.',
        )

  @navi_test_base.named_parameterized(
      incoming_cvsd_s4=dict(
          direction=constants.Direction.INCOMING,
          sco_parameters=hfp.ESCO_PARAMETERS[
              hfp.DefaultCodecParameters.ESCO_CVSD_S4
          ],
      ),
      outgoing_cvsd_s4=dict(
          direction=constants.Direction.OUTGOING,
          sco_parameters=hfp.ESCO_PARAMETERS[
              hfp.DefaultCodecParameters.ESCO_CVSD_S4
          ],
      ),
      incoming_transparent_t2=dict(
          direction=constants.Direction.INCOMING,
          sco_parameters=hfp_ext.ESCO_PARAMETERS_T2_TRANSPARENT,
      ),
      outgoing_transparent_t2=dict(
          direction=constants.Direction.OUTGOING,
          sco_parameters=hfp_ext.ESCO_PARAMETERS_T2_TRANSPARENT,
      ),
      incoming_msbc_t2=dict(
          direction=constants.Direction.INCOMING,
          sco_parameters=hfp.ESCO_PARAMETERS[
              hfp.DefaultCodecParameters.ESCO_MSBC_T2
          ],
      ),
      outgoing_msbc_t2=dict(
          direction=constants.Direction.OUTGOING,
          sco_parameters=hfp.ESCO_PARAMETERS[
              hfp.DefaultCodecParameters.ESCO_MSBC_T2
          ],
      ),
      incoming_lc3_t2=dict(
          direction=constants.Direction.INCOMING,
          sco_parameters=hfp_ext.ESCO_PARAMETERS_LC3_T2,
      ),
      outgoing_lc3_t2=dict(
          direction=constants.Direction.OUTGOING,
          sco_parameters=hfp_ext.ESCO_PARAMETERS_LC3_T2,
      ),
  )
  async def test_esco_connection(
      self, direction: constants.Direction, sco_parameters: hfp.EscoParameters
  ) -> None:
    """Tests legacy pairing."""
    codec_id = sco_parameters.transmit_coding_format.codec_id
    if not self.is_emulator:
      if codec_id not in self.dut_supported_codecs:
        self.skipTest(f'Codec {codec_id} is not supported by DUT.')
      if codec_id not in self.ref_supported_codecs:
        self.skipTest(f'Codec {codec_id} is not supported by REF.')

    connections = await self.test_connect(direction)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      sco_connection_results: list[asyncio.Future[device_lib.ScoLink]] = [
          asyncio.get_running_loop().create_future() for _ in range(2)
      ]
      for connection, sco_connection_result in zip(
          connections, sco_connection_results
      ):
        connection.device.once(
            connection.device.EVENT_SCO_CONNECTION,
            sco_connection_result.set_result,
        )
        # EVENT_SCO_CONNECTION_FAILURE doesn't provide status code.
        connection.device.once(
            connection.device.EVENT_SCO_CONNECTION_FAILURE,
            functools.partial(
                sco_connection_result.set_exception,
                AssertionError('SCO connection failed.'),
            ),
        )

      sco_requests = asyncio.Queue[int]()
      connections[1].device.on(
          connections[1].device.EVENT_SCO_REQUEST,
          lambda connection, link_type: sco_requests.put_nowait(link_type),
      )

      self.logger.info('[Central] Establishing SCO connection.')
      await connections[0].device.send_async_command(
          hci.HCI_Enhanced_Setup_Synchronous_Connection_Command(
              connection_handle=connections[0].handle,
              **sco_parameters.asdict(),
          )
      )

      self.logger.info('[Peripheral] Waiting for SCO request.')
      link_type = await sco_requests.get()

      self.assertEqual(
          link_type,
          hci.HCI_Connection_Complete_Event.LinkType.ESCO,
          msg='SCO link type is not ESCO.',
      )

      self.logger.info('[Peripheral] Accepting SCO request.')
      await connections[1].device.send_async_command(
          hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
              bd_addr=connections[1].peer_address,
              **sco_parameters.asdict(),
          )
      )

      self.logger.info('Waiting for SCO connection results.')
      sco_connections = await asyncio.gather(*sco_connection_results)

      disconnection_results: list[asyncio.Future[int]] = [
          asyncio.get_running_loop().create_future() for _ in range(2)
      ]
      for sco_connection, disconnection_result in zip(
          sco_connections, disconnection_results
      ):
        sco_connection.once(
            sco_connection.EVENT_DISCONNECTION,
            disconnection_result.set_result,
        )

      self.logger.info('[Central] Disconnecting SCO connection.')
      await sco_connections[0].disconnect()

      self.logger.info('Waiting for disconnection results.')
      await asyncio.gather(*disconnection_results)

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_legacy_pairing(self, direction: constants.Direction) -> None:
    """Tests legacy pairing."""
    self.logger.info('[REF] Disable SSP.')
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.ref.device.classic_sc_enabled = False
      self.ref.device.classic_ssp_enabled = False
      await self.ref.device.power_on()

    for device in self._devices:
      device.device.pairing_config_factory = lambda _: pairing.PairingConfig(
          delegate=_LegacyPairingDelegate(
              io_capability=pairing.PairingDelegate.IoCapability.KEYBOARD_INPUT_ONLY,
              pin='123456',
          )
      )
    connections = await self.test_connect(direction)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      link_key_results: list[asyncio.Future[None]] = [
          asyncio.get_running_loop().create_future() for _ in range(2)
      ]
      for connection, link_key_result in zip(connections, link_key_results):
        connection.device.once(
            connection.device.EVENT_KEY_STORE_UPDATE,
            functools.partial(link_key_result.set_result, None),
        )

      self.logger.info('Authenticating connection.')
      await connections[0].authenticate()

      self.logger.info('Waiting for link key notifications.')
      await asyncio.gather(*link_key_results)

    self.assertEqual(
        await self.dut.device.get_link_key(self.ref.device.public_address),
        await self.ref.device.get_link_key(self.dut.device.public_address),
        msg='Link keys are not the same.',
    )

  @navi_test_base.parameterized(*[
      (variant, direction, io_capability)
      for variant, direction, io_capability in itertools.product(
          TestVariant,
          constants.Direction,
          [
              _IoCapability.NO_OUTPUT_NO_INPUT,
              _IoCapability.KEYBOARD_INPUT_ONLY,
              _IoCapability.DISPLAY_OUTPUT_ONLY,
              _IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
          ],
      )
      if not (
          # PASSKEY_NOTIFICATION cannot be rejected.
          variant == TestVariant.REJECT
          and io_capability == _IoCapability.KEYBOARD_INPUT_ONLY
      )
  ])
  async def test_ssp(
      self,
      variant: TestVariant,
      pairing_direction: constants.Direction,
      ref_io_capability: _IoCapability,
  ) -> None:
    """Tests Simple Secure Pairing.

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
    """
    self.logger.info('Enable SSP.')
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      for device in self._devices:
        device.device.classic_sc_enabled = True
        device.device.classic_ssp_enabled = True
        await device.device.power_on()

    # Android almost always uses DISPLAY_OUTPUT_AND_YES_NO_INPUT.
    dut_pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=_IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
        auto_accept=True,
    )
    ref_pairing_delegate = pairing_utils.PairingDelegate(
        io_capability=ref_io_capability,
        auto_accept=True,
    )

    def pairing_config_factory(
        connection: device_lib.Connection,
        pairing_delegate: pairing_utils.PairingDelegate,
    ) -> pairing.PairingConfig:
      del connection  # Unused.
      return pairing.PairingConfig(
          sc=True,
          mitm=True,
          bonding=True,
          identity_address_type=pairing.PairingConfig.AddressType.PUBLIC,
          delegate=pairing_delegate,
      )

    self.logger.info('[DUT] Set pairing config factory.')
    self.dut.device.pairing_config_factory = functools.partial(
        pairing_config_factory,
        pairing_delegate=dut_pairing_delegate,
    )
    self.logger.info('[REF] Set pairing config factory.')
    self.ref.device.pairing_config_factory = functools.partial(
        pairing_config_factory,
        pairing_delegate=ref_pairing_delegate,
    )

    connections = await self.test_connect(pairing_direction)

    auth_task = asyncio.create_task(connections[0].authenticate())
    dut_accept = variant != TestVariant.REJECT
    ref_accept = variant != TestVariant.REJECTED

    pairing_futures: list[asyncio.Future[int | None]] = [
        asyncio.get_running_loop().create_future() for _ in range(2)
    ]
    link_key_futures: list[asyncio.Future[None]] = [
        asyncio.get_running_loop().create_future() for _ in range(2)
    ]
    for connection, pairing_future, link_key_future in zip(
        connections, pairing_futures, link_key_futures
    ):
      connection.once(
          connection.EVENT_CLASSIC_PAIRING,
          functools.partial(pairing_future.set_result, None),
      )
      connection.once(
          connection.EVENT_CLASSIC_PAIRING_FAILURE,
          pairing_future.set_result,
      )
      connection.device.once(
          connection.device.EVENT_KEY_STORE_UPDATE,
          functools.partial(link_key_future.set_result, None),
      )

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECONDS):
      self.logger.info('[DUT] Wait for pairing event.')
      dut_pairing_event = await dut_pairing_delegate.pairing_events.get()
      self.logger.info('[REF] Wait for pairing event.')
      ref_pairing_event = await ref_pairing_delegate.pairing_events.get()

      match ref_io_capability:
        case _IoCapability.NO_OUTPUT_NO_INPUT:
          expected_dut_pairing_variant = _PairingVariant.JUST_WORK
          expected_ref_pairing_variant = _PairingVariant.JUST_WORK
          dut_pairing_delegate.pairing_answers.put_nowait(dut_accept)
          ref_pairing_delegate.pairing_answers.put_nowait(ref_accept)
        case _IoCapability.KEYBOARD_INPUT_ONLY:
          expected_dut_pairing_variant = (
              _PairingVariant.PASSKEY_ENTRY_NOTIFICATION
          )
          expected_ref_pairing_variant = _PairingVariant.PASSKEY_ENTRY_REQUEST
          # For SSP PASSKEY pairing, Bumble will invoke display_number, and then
          # confirm, so we need to unblock both events.
          dut_pairing_delegate.pairing_answers.put_nowait(None)

          dut_pairing_delegate.pairing_answers.put_nowait(dut_accept)
          ref_pairing_delegate.pairing_answers.put_nowait(
              dut_pairing_event.arg if ref_accept else None
          )
        case _IoCapability.DISPLAY_OUTPUT_ONLY:
          expected_dut_pairing_variant = _PairingVariant.NUMERIC_COMPARISON
          expected_ref_pairing_variant = (
              _PairingVariant.PASSKEY_ENTRY_NOTIFICATION
          )
          self.assertEqual(
              dut_pairing_event.arg,
              ref_pairing_event.arg,
              msg='Numeric comparison values are not the same.',
          )
          # For SSP PASSKEY pairing, Bumble will invoke display_number, and then
          # confirm, so we need to unblock both events.
          ref_pairing_delegate.pairing_answers.put_nowait(None)

          dut_pairing_delegate.pairing_answers.put_nowait(dut_accept)
          ref_pairing_delegate.pairing_answers.put_nowait(ref_accept)
        case _IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT:
          expected_dut_pairing_variant = _PairingVariant.NUMERIC_COMPARISON
          expected_ref_pairing_variant = _PairingVariant.NUMERIC_COMPARISON
          self.assertEqual(
              dut_pairing_event.arg,
              ref_pairing_event.arg,
              msg='Numeric comparison values are not the same.',
          )
          dut_pairing_delegate.pairing_answers.put_nowait(dut_accept)
          ref_pairing_delegate.pairing_answers.put_nowait(ref_accept)
        case _:
          raise ValueError(f'Unsupported IO capability: {ref_io_capability}')
      self.assertEqual(dut_pairing_event.variant, expected_dut_pairing_variant)
      self.assertEqual(ref_pairing_event.variant, expected_ref_pairing_variant)

      if variant == TestVariant.ACCEPT:
        self.logger.info('Waiting for pairing event.')
        self.assertSequenceEqual(
            await asyncio.gather(*pairing_futures), [None, None]
        )

        self.logger.info('Waiting for authentication complete.')
        await auth_task

        self.logger.info('Waiting for link key notifications.')
        await asyncio.gather(*link_key_futures)

        self.assertEqual(
            await self.dut.device.get_link_key(self.ref.device.public_address),
            await self.ref.device.get_link_key(self.dut.device.public_address),
            msg='Link keys are not the same.',
        )
      else:
        self.logger.info('Waiting for pairing failure.')
        self.assertSequenceEqual(
            await asyncio.gather(*pairing_futures),
            [
                hci.HCI_ErrorCode.AUTHENTICATION_FAILURE_ERROR,
                hci.HCI_ErrorCode.AUTHENTICATION_FAILURE_ERROR,
            ],
        )

        self.logger.info('Waiting for authentication failure.')
        with self.assertRaises((hci.HCI_Error, asyncio.CancelledError)):
          await auth_task


if __name__ == '__main__':
  test_runner.main()
