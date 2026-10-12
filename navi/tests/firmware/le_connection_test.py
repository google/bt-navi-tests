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
import functools
import itertools
import secrets

from bumble import core
from bumble import device as device_lib
from bumble import hci
from bumble import keys
from mobly import test_runner

from navi.tests import navi_test_base
from navi.tests.firmware import test_base
from navi.utils import constants

_DEFAULT_UPDATE_CONNECTION_TIMEOUT_SECONDS = 10.0
_DEFAULT_CONNECTION_TIMEOUT_SECONDS = 10.0

# Standard CIS parameters from BAP spec.
_CIG_PARAMETERS = {
    '48_4-1cis-source': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=240,
                max_sdu_p_to_c=0,
            ),
        ],
        sdu_interval_c_to_p=10000,
        sdu_interval_p_to_c=0,
    ),
    '48_4-1cis-sink': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=0,
                max_sdu_p_to_c=240,
            ),
        ],
        sdu_interval_c_to_p=0,
        sdu_interval_p_to_c=10000,
    ),
    '32_2-1cis-source-sink': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=160,
                max_sdu_p_to_c=80,
            ),
        ],
        sdu_interval_c_to_p=10000,
        sdu_interval_p_to_c=10000,
    ),
    '32_2-2cis-source-sink': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=80,
                max_sdu_p_to_c=80,
            ),
            device_lib.CigParameters.CisParameters(
                cis_id=2,
                max_sdu_c_to_p=80,
                max_sdu_p_to_c=80,
            ),
        ],
        sdu_interval_c_to_p=10000,
        sdu_interval_p_to_c=10000,
    ),
    '48_4-2cis-sink': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=0,
                max_sdu_p_to_c=120,
            ),
            device_lib.CigParameters.CisParameters(
                cis_id=2,
                max_sdu_c_to_p=0,
                max_sdu_p_to_c=120,
            ),
        ],
        sdu_interval_c_to_p=0,
        sdu_interval_p_to_c=10000,
    ),
    '48_4-2cis-source': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=120,
                max_sdu_p_to_c=0,
            ),
            device_lib.CigParameters.CisParameters(
                cis_id=2,
                max_sdu_c_to_p=120,
                max_sdu_p_to_c=0,
            ),
        ],
        sdu_interval_c_to_p=10000,
        sdu_interval_p_to_c=0,
    ),
    '48_3-1cis-source': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=180,
                max_sdu_p_to_c=0,
            ),
        ],
        sdu_interval_c_to_p=7500,
        sdu_interval_p_to_c=0,
    ),
    '48_3-1cis-sink': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=0,
                max_sdu_p_to_c=180,
            ),
        ],
        sdu_interval_c_to_p=0,
        sdu_interval_p_to_c=7500,
    ),
    '48_2-32_2-1cis-source-sink': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=200,
                max_sdu_p_to_c=80,
            ),
        ],
        sdu_interval_c_to_p=10000,
        sdu_interval_p_to_c=10000,
    ),
    '48_2-32_2-2cis-source-sink': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=100,
                max_sdu_p_to_c=80,
            ),
            device_lib.CigParameters.CisParameters(
                cis_id=2,
                max_sdu_c_to_p=100,
                max_sdu_p_to_c=80,
            ),
        ],
        sdu_interval_c_to_p=10000,
        sdu_interval_p_to_c=10000,
    ),
    '48_3-2cis-sink': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=0,
                max_sdu_p_to_c=90,
            ),
            device_lib.CigParameters.CisParameters(
                cis_id=2,
                max_sdu_c_to_p=0,
                max_sdu_p_to_c=90,
            ),
        ],
        sdu_interval_c_to_p=0,
        sdu_interval_p_to_c=7500,
    ),
    '48_3-2cis-source': device_lib.CigParameters(
        cig_id=1,
        cis_parameters=[
            device_lib.CigParameters.CisParameters(
                cis_id=1,
                max_sdu_c_to_p=90,
                max_sdu_p_to_c=0,
            ),
            device_lib.CigParameters.CisParameters(
                cis_id=2,
                max_sdu_c_to_p=90,
                max_sdu_p_to_c=0,
            ),
        ],
        sdu_interval_c_to_p=7500,
        sdu_interval_p_to_c=0,
    ),
}


class LeConnectionTest(test_base.DualDeviceTestBase):
  """Tests for LE connection."""

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_connect(
      self, direction: constants.Direction
  ) -> tuple[device_lib.Connection, device_lib.Connection]:
    """Tests connecting to a remote device."""
    self.logger.info('Create Bluetooth LE connection.')

    if direction == constants.Direction.INCOMING:
      central, peripheral = self.ref.device, self.dut.device
    else:
      central, peripheral = self.dut.device, self.ref.device
    return await self.create_connection(
        central,
        peripheral,
        core.PhysicalTransport.LE,
    )

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_get_remote_features(
      self, direction: constants.Direction
  ) -> None:
    """Tests getting remote features.

    Test steps:
      1. Create a Bluetooth LE connection.
      2. Get remote features from the devices.

    Args:
      direction: The direction of the connection.
    """
    connections = await self.test_connect(direction)

    async with self.assert_not_timeout(_DEFAULT_CONNECTION_TIMEOUT_SECONDS):
      self.logger.info('[Central] Get remote features.')
      await connections[0].get_remote_le_features()
      self.logger.info('[Peripheral] Get remote features.')
      await connections[1].get_remote_le_features()

  @navi_test_base.parameterized(
      constants.Direction.INCOMING, constants.Direction.OUTGOING
  )
  async def test_set_phy(self, direction: constants.Direction) -> None:
    """Tests setting phy.

    Test steps:
      1. Create a Bluetooth LE connection.
      2. Set phy to tx: LE_2M, rx: LE_2M.
      3. Set phy to tx: LE_1M, rx: LE_1M.
      4. Set phy to tx: LE_CODED, rx: LE_CODED.
      3. Verify that the connection is not dropped.

    Args:
      direction: The direction of the connection.
    """
    connections = await self.test_connect(direction)

    for phy in [hci.Phy.LE_2M, hci.Phy.LE_1M, hci.Phy.LE_CODED]:
      async with self.assert_not_timeout(_DEFAULT_CONNECTION_TIMEOUT_SECONDS):
        phy_results: list[asyncio.Future[core.ConnectionPHY]] = [
            asyncio.get_running_loop().create_future() for _ in range(2)
        ]
        for connection, phy_result in zip(connections, phy_results):
          connection.once(
              connection.EVENT_CONNECTION_PHY_UPDATE, phy_result.set_result
          )
          connection.once(
              connection.EVENT_CONNECTION_PHY_UPDATE_FAILURE,
              functools.partial(
                  lambda result, reason: result.set_exception(
                      hci.HCI_Error(reason)
                  ),
                  phy_result,
              ),
          )
        self.logger.info('Setting phy to tx: %s, rx: %s', phy, phy)
        await connections[0].set_phy(tx_phys=[phy], rx_phys=[phy])

        updated_phys = await asyncio.gather(*phy_results)
        self.logger.info('Updated phys: %s', updated_phys)

        self.assertEqual(updated_phys[0].tx_phy, phy)
        self.assertEqual(updated_phys[0].rx_phy, phy)

  async def test_update_le_connection(self) -> None:
    """Tests updating LE connection parameters during transmission.

    Test steps:
      1. Create a Bluetooth LE connection.
      2. Send l2cap data packet to the devices.
      3. Update the LE connection parameters.
      4. Verify that the connection is not dropped.
    """
    self.logger.info('Create Bluetooth LE connection.')
    connection_parameters_preferences = {
        hci.Phy.LE_1M: device_lib.ConnectionParametersPreferences(
            connection_interval_min=24 * 1.25,
            connection_interval_max=40 * 1.25,
            max_latency=0,
            supervision_timeout=500 * 10,
        )
    }
    connections = await self.create_connection(
        self.dut.device,
        self.ref.device,
        core.PhysicalTransport.LE,
        connection_parameters=connection_parameters_preferences,
    )

    disconnection = asyncio.Queue[int]()
    for connection in connections:
      connection.on(connection.EVENT_DISCONNECTION, disconnection.put_nowait)

    connections[0].send_l2cap_pdu(0, bytes(50_000))
    connections[1].send_l2cap_pdu(0, bytes(50_000))

    async with self.assert_not_timeout(
        _DEFAULT_UPDATE_CONNECTION_TIMEOUT_SECONDS
    ):
      parameter_update_results: list[asyncio.Future[None]] = [
          asyncio.get_running_loop().create_future() for _ in range(2)
      ]
      for connection, parameter_update_result in zip(
          connections, parameter_update_results
      ):
        connection.once(
            connection.EVENT_CONNECTION_PARAMETERS_UPDATE,
            functools.partial(parameter_update_result.set_result, None),
        )
        connection.once(
            connection.EVENT_CONNECTION_PARAMETERS_UPDATE_FAILURE,
            functools.partial(
                lambda result, reason: result.set_exception(
                    hci.HCI_Error(reason)
                ),
                parameter_update_result,
            ),
        )

      self.logger.info('Updating connection parameters.')
      await connections[0].update_parameters(
          connection_interval_min=8,
          connection_interval_max=16,
          max_latency=0,
          supervision_timeout=500,
      )

      self.logger.info('Waiting for parameter update results.')
      await asyncio.gather(*parameter_update_results)

    # Wait for 10 seconds, or until the disconnections are received.
    async with self.assert_timeout(
        _DEFAULT_CONNECTION_TIMEOUT_SECONDS,
        msg='Keep connection for 10 seconds.',
    ):
      await disconnection.get()

  async def test_encrypt_le_connection(self) -> None:
    """Tests stability by encrypting LE connection during transmission.

    Test steps:
      1. Create a Bluetooth LE connection.
      2. Inject pairing keys to the devices.
      3. Send l2cap data packet to the devices.
      4. Encrypt the LE connection.
      5. Verify that the connection is not dropped.
    """
    self.logger.info('Create Bluetooth LE connection.')
    connections = await self.create_connection(
        self.dut.device,
        self.ref.device,
        core.PhysicalTransport.LE,
    )

    # Inject pairing keys to the devices.
    pairing_keys = keys.PairingKeys()
    pairing_keys.ltk = keys.PairingKeys.Key(
        secrets.token_bytes(16), authenticated=True
    )
    await self.dut.device.update_keys(
        str(connections[0].peer_address), pairing_keys
    )
    await self.ref.device.update_keys(
        str(connections[1].peer_address), pairing_keys
    )

    disconnection = asyncio.Queue[int]()
    for connection in connections:
      connection.on(connection.EVENT_DISCONNECTION, disconnection.put_nowait)

    connections[0].send_l2cap_pdu(0, bytes(50_000))
    connections[1].send_l2cap_pdu(0, bytes(50_000))

    async with self.assert_not_timeout(
        _DEFAULT_UPDATE_CONNECTION_TIMEOUT_SECONDS
    ):
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

    # Wait for 10 seconds, or until the disconnections are received.
    async with self.assert_timeout(
        _DEFAULT_CONNECTION_TIMEOUT_SECONDS,
        msg='Keep connection for 10 seconds.',
    ):
      await disconnection.get()

  @navi_test_base.named_parameterized(*[
      dict(
          testcase_name=f'{direction.name}_{cig_parameters_name}'.lower(),
          direction=direction,
          cig_parameters=cig_parameters,
      )
      for direction, (cig_parameters_name, cig_parameters) in itertools.product(
          constants.Direction,
          _CIG_PARAMETERS.items(),
      )
  ])
  async def test_create_cis(
      self,
      direction: constants.Direction,
      cig_parameters: device_lib.CigParameters,
  ) -> None:
    """Tests creating CIS."""
    if direction == constants.Direction.OUTGOING:
      if not self.dut.device.supports_le_features(
          hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_CENTRAL
      ):
        self.skipTest('CIS central is not supported on DUT.')
      if not self.ref.device.supports_le_features(
          hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL
      ):
        self.skipTest('CIS peripheral is not supported on REF.')
    else:
      if not self.dut.device.supports_le_features(
          hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL
      ):
        self.skipTest('CIS peripheral is not supported on DUT.')
      if not self.ref.device.supports_le_features(
          hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_CENTRAL
      ):
        self.skipTest('CIS central is not supported on REF.')

    # Enable Connected Isochronous Stream.
    async with self.assert_not_timeout(_DEFAULT_CONNECTION_TIMEOUT_SECONDS):
      for device in self._devices:
        await device.device.send_sync_command(
            hci.HCI_LE_Set_Host_Feature_Command(
                bit_number=hci.LeFeature.CONNECTED_ISOCHRONOUS_STREAM,
                bit_value=1,
            )
        )

    self.logger.info('Create Bluetooth LE connection.')
    connections = await self.test_connect(direction)

    async with self.assert_not_timeout(_DEFAULT_CONNECTION_TIMEOUT_SECONDS):

      self.logger.info('Setup CIS.')
      cis_handles = await connections[0].device.setup_cig(cig_parameters)

      # Auto accept CIS request from the central.
      connections[1].on(
          connections[1].EVENT_CIS_REQUEST,
          connections[1].device.accept_cis_request,
      )

      peripheral_cis_link_queue = asyncio.Queue[device_lib.CisLink]()
      connections[1].on(
          connections[1].EVENT_CIS_ESTABLISHMENT,
          peripheral_cis_link_queue.put_nowait,
      )

      self.logger.info('Create CIS.')
      central_cis_links = await connections[0].device.create_cis(
          [(cis_handle, connections[0]) for cis_handle in cis_handles]
      )

      self.logger.info('[Peripheral] Waiting for CIS establishment.')
      peripheral_cis_links = [
          await peripheral_cis_link_queue.get() for _ in central_cis_links
      ]

      self.logger.info('[Central] Setup data path.')
      for central_cis_link in central_cis_links:
        if central_cis_link.max_pdu_c_to_p > 0:
          await central_cis_link.setup_data_path(
              central_cis_link.Direction.HOST_TO_CONTROLLER,
          )
        if central_cis_link.max_pdu_p_to_c > 0:
          await central_cis_link.setup_data_path(
              central_cis_link.Direction.CONTROLLER_TO_HOST,
          )

      self.logger.info('[Peripheral] Setup data path.')
      for peripheral_cis_link in peripheral_cis_links:
        if peripheral_cis_link.max_pdu_c_to_p > 0:
          await peripheral_cis_link.setup_data_path(
              peripheral_cis_link.Direction.CONTROLLER_TO_HOST,
          )
        if peripheral_cis_link.max_pdu_p_to_c > 0:
          await peripheral_cis_link.setup_data_path(
              peripheral_cis_link.Direction.HOST_TO_CONTROLLER,
          )

      disconnection_results: list[asyncio.Future[int]] = [
          asyncio.get_running_loop().create_future()
          for _ in peripheral_cis_links
      ]
      for peripheral_cis_link, disconnection_result in zip(
          peripheral_cis_links, disconnection_results
      ):
        peripheral_cis_link.once(
            peripheral_cis_link.EVENT_DISCONNECTION,
            disconnection_result.set_result,
        )
        peripheral_cis_link.once(
            peripheral_cis_link.EVENT_DISCONNECTION_FAILURE,
            functools.partial(
                disconnection_result.set_exception,
                AssertionError('CIS disconnection failed.'),
            ),
        )

      self.logger.info('[Central] Disconnect CIS.')
      for central_cis_link in central_cis_links:
        await central_cis_link.disconnect()

      self.logger.info('[Peripheral] Waiting for CIS disconnection.')
      await asyncio.gather(*disconnection_results)


if __name__ == '__main__':
  test_runner.main()
