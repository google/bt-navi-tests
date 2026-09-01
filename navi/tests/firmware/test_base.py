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

"""Base classes for Bluetooth firmware tests."""

import asyncio
from collections.abc import Sequence
import functools
import pathlib
import secrets
from typing import Mapping

from bumble import core
from bumble import device as bumble_device
from bumble import hci
from bumble import keys
from mobly import records
from mobly.controllers import android_device
from typing_extensions import override

from navi.bumble_ext import crown
from navi.tests import navi_test_base
from navi.utils import adb_snippets
from navi.utils import retry as retry_lib

_RESET_TIMEOUT_SECONDS = 15.0
_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0

_CrownDriver = navi_test_base.CrownDriver


# See b/227146913#comment5.
_BTRT_ENABLE_COMMAND = hci.HCI_Command(
    op_code=0xFE20,
    parameters=bytes.fromhex(
        "010100000000000000003f0000000201000000000000000000000000"
    ),
)


class _FirmwareTestBase(navi_test_base.BaseTestBase):
  """Base class for firmware tests."""

  NUMBLE_OF_DEVICES: int
  _devices: Sequence[crown.CrownDevice] = ()
  _android_devices: Sequence[android_device.AndroidDevice] = ()
  is_emulator: bool = False

  @override
  async def async_setup_class(self):
    await super().async_setup_class()
    match self.user_params.get("crown_driver", _CrownDriver.ANDROID):
      case _CrownDriver.ANDROID:
        self._android_devices = self._get_android_controllers(
            self.NUMBLE_OF_DEVICES
        )
        self._devices = [
            await crown.CrownDevice.from_android_device(device)
            for device in self._android_devices
        ]
        if self._android_devices:
          self.is_emulator = any(
              device.is_emulator for device in self._android_devices
          )
      case _CrownDriver.PASSTHROUGH:
        no_android = bool(self.user_params.get("no_android", False))
        self._android_devices = (
            [] if no_android else self._get_android_controllers(1)
        )
        crown_driver_specs = self.user_params.get("crown_driver_specs", "")
        if isinstance(crown_driver_specs, str):
          crown_driver_specs = [
              spec for spec in crown_driver_specs.split(",") if spec
          ]
        reset_delay = float(
            self.user_params.get("crown_reset_delay", 0.0) or 0.0
        )
        self._devices = [
            await crown.CrownDevice.from_android_device(device)
            for device in self._android_devices
        ] + [
            await crown.CrownDevice.create(
                crown.CrownAdapter(hci_spec),
                reset_delay=reset_delay,
            )
            for hci_spec in crown_driver_specs
        ]
        self.is_emulator = bool(self.user_params.get("is_emulator", False))
      case _:
        raise ValueError("Unsupported Crown driver")

  @override
  @retry_lib.retry_on_exception()
  async def async_setup_test(self):
    await super().async_setup_test()
    async with self.assert_not_timeout(_RESET_TIMEOUT_SECONDS):
      try:
        await asyncio.gather(*[device.reset() for device in self._devices])
      except ExceptionGroup as e:
        for exc in e.exceptions:
          self.logger.exception(f"Device reset failed: {exc}")
    if self.user_params.get("enable_btrt"):
      for dev in self._devices:
        await dev.device.send_command(_BTRT_ENABLE_COMMAND)

  @override
  async def async_teardown_class(self):
    await super().async_teardown_class()
    async with self.assert_not_timeout(_RESET_TIMEOUT_SECONDS):
      try:
        await asyncio.gather(*[device.close() for device in self._devices])
      except ExceptionGroup as e:
        for exc in e.exceptions:
          self.logger.exception(f"Device close failed: {exc}")

    for device in self._devices:
      device.adapter.stop()

  def _get_btsnoop(self) -> None:
    for device in self._devices:
      with open(
          pathlib.Path(
              self.current_test_info.output_path,
              f"bumble_{device.address}_btsnoop.log",
          ),
          "wb",
      ) as f:
        f.write(device.snoop_buffer.getvalue())
      if isinstance(device.adapter, crown.AndroidCrownAdapter):
        adb_snippets.download_btsnoop(
            device=device.adapter.ad,
            destination_base_path=self.current_test_info.output_path,
            filename_prefix="bumble",
        )
        adb_snippets.cleanup_btsnoop(device=device.adapter.ad)

  @override
  def on_fail(self, record: records.TestResultRecord) -> None:
    self._get_btsnoop()

  @override
  def on_pass(self, record: records.TestResultRecord) -> None:
    self._get_btsnoop()


class SingleDeviceTestBase(_FirmwareTestBase):
  """Base class for single device firmware tests."""

  NUMBLE_OF_DEVICES = 1
  dut: crown.CrownDevice
  dut_android_device: android_device.AndroidDevice

  @override
  async def async_setup_class(self):
    await super().async_setup_class()
    self.dut = self._devices[0]
    self.dut_android_device = self._android_devices[0]


class DualDeviceTestBase(_FirmwareTestBase):
  """Base class for dual device firmware tests."""

  NUMBLE_OF_DEVICES = 2
  dut: crown.CrownDevice
  ref: crown.CrownDevice

  @override
  async def async_setup_class(self):
    await super().async_setup_class()
    self.dut, self.ref = self._devices

  @retry_lib.retry_on_exception(initial_delay_sec=1, num_retries=3)
  async def create_connection(
      self,
      central: bumble_device.Device,
      peripheral: bumble_device.Device,
      link_type: core.PhysicalTransport,
      connection_parameters: (
          Mapping[hci.Phy, bumble_device.ConnectionParametersPreferences] | None
      ) = None,
      timeout: float = _DEFAULT_STEP_TIMEOUT_SECONDS,
  ) -> tuple[bumble_device.Connection, bumble_device.Connection]:
    """Create the Bluetooth ACL link between the central and peripheral devices.

    And divide the ACL connection with BREDR and LE.

    Args:
      central: The central device to create the connection.
      peripheral: The peripheral device to accept the connection from central.
      link_type: The link type to connect. BREDR or LE.
      connection_parameters: The connection parameters to use.
      timeout: The timeout to wait for the connection to be established.

    Returns:
      A tuple of the central and peripheral connections.

    Raises:
      ValueError: If the link type is not supported.
    """
    peripheral_connections = asyncio.Queue[bumble_device.Connection]()
    peripheral.on(
        peripheral.EVENT_CONNECTION, peripheral_connections.put_nowait
    )

    async with self.assert_not_timeout(timeout, "Making connection"):
      if link_type == core.BT_BR_EDR_TRANSPORT:
        central_connection = await central.connect(
            peripheral.public_address, transport=link_type
        )
      elif link_type == core.BT_LE_TRANSPORT:
        await peripheral.start_advertising(
            own_address_type=hci.OwnAddressType.RANDOM
        )
        central_connection = await central.connect(
            peripheral.random_address,
            transport=core.BT_LE_TRANSPORT,
            connection_parameters_preferences=dict(connection_parameters)
            if connection_parameters
            else None,
        )
        await central_connection.get_remote_le_features()
      else:
        raise ValueError(f"Unsupported link type: {link_type}")
      peripheral_connection = await peripheral_connections.get()
    return central_connection, peripheral_connection

  async def encrypt_connection(
      self,
      connections: tuple[bumble_device.Connection, bumble_device.Connection],
      ltk: bytes | None = None,
      timeout: float = _DEFAULT_STEP_TIMEOUT_SECONDS,
  ) -> None:
    """Encrypt the Bluetooth ACL link.

    Args:
      connections: The connections to encrypt.
      ltk: The Long-Term Key to use.
      timeout: The timeout to wait for the encryption to complete.
    """
    # Inject pairing keys to the devices.
    pairing_keys = keys.PairingKeys()
    pairing_keys.ltk = keys.PairingKeys.Key(
        ltk or secrets.token_bytes(16), authenticated=True
    )
    for connection in connections:
      await connection.device.update_keys(
          str(connection.peer_address), pairing_keys
      )

    encryption_events = [asyncio.Event() for _ in range(2)]
    for connection, encryption_event in zip(connections, encryption_events):

      def on_encryption_change(event: asyncio.Event):
        event.set()

      connection.once(
          connection.EVENT_CONNECTION_ENCRYPTION_CHANGE,
          functools.partial(on_encryption_change, encryption_event),
      )

    async with self.assert_not_timeout(timeout):
      self.logger.info("Encrypting connection.")
      await connections[0].encrypt()
      self.logger.info("Waiting for encryption on initiator.")
      await encryption_events[0].wait()
      self.logger.info("Waiting for encryption on responder.")
      await encryption_events[1].wait()
