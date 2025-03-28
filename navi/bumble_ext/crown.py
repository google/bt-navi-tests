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

"""Crown, a Bumble host wrapper to run on Android."""

from __future__ import annotations

import asyncio
import enum
import io
import subprocess
import sys
import time
from typing import Self, Type
import uuid

from bumble import device as bumble_device
from bumble import hci
from bumble import host
from bumble import snoop
from bumble import transport
from mobly.controllers import android_device
from typing_extensions import override

from navi.utils import resources
from navi.utils import adb_snippets
from navi.utils import retry


_HCI_PROXY_G3_PATH_PREFIX = 'navi/bumble_ext/android_hci_proxy_'
_HCI_PROXY_DEVICE_PATH = '/data/local/tmp/hci_proxy'
_DEVICE_ABSTRACT_SOCKET_PATH = 'hci_socket'


class Driver(enum.StrEnum):
  ANDROID = 'android'
  PASSTHROUGH = 'passthrough'


def _make_device(
    config: bumble_device.DeviceConfiguration,
) -> bumble_device.Device:
  """Initialize an idle Bumble device instance."""

  # initialize bumble device.
  device = bumble_device.Device(config=config, host=None)

  return device


class CrownDevice:
  """Wrapper around a Bumble device and its HCI transport.

  Attributes:
    device: Bumble device.
    config: Bumble device configuration.
    hci: HCI transport instance.
    adapter: Adapter between Bumble host and Android controller.
    address: Public address of this device.
    random_address: Random identity address of this device.
    snoop_buffer: Buffer for HCI snoop logs. This is cleared when the device is
      opened.
  """

  device: bumble_device.Device
  config: bumble_device.DeviceConfiguration

  hci: transport.Transport | None
  adapter: CrownAdapter

  def __init__(
      self,
      adapter: CrownAdapter,
      config: bumble_device.DeviceConfiguration | None = None,
  ) -> None:
    self.config = config or bumble_device.DeviceConfiguration(
        classic_enabled=True
    )
    self.device = _make_device(self.config)
    self.hci = None
    self.adapter = adapter
    self.snoop_buffer = io.BytesIO()

  async def open(self) -> None:
    """Opens this device and its HCI transport."""
    if self.hci is not None:
      return

    # open HCI transport & set device host.
    self.hci = await transport.open_transport(self.adapter.hci_spec)
    self.device.host = host.Host(
        controller_source=self.hci.source, controller_sink=self.hci.sink
    )
    self.snoop_buffer = io.BytesIO()
    self.device.host.snooper = snoop.BtSnooper(self.snoop_buffer)
    # Reset.
    await self.device.host.reset()
    # Get controller name.
    await self.device.host.send_command(hci.HCI_Read_Local_Name_Command())

    # power-on.
    await self.device.power_on()

  async def close(self) -> None:
    """Closes this device and its HCI transport."""
    if self.hci is None:
      return

    # flush & re-initialize device.
    await self.device.host.flush()
    self.device.host = None
    self.device = _make_device(self.config)

    # close HCI transport.
    await self.hci.close()
    self.hci = None

  async def reset(self) -> None:
    """Resets this device."""
    await self.close()
    await self.open()

  @property
  def address(self) -> str:
    return str(self.device.public_address)[:-2]

  @property
  def random_address(self) -> str:
    return str(self.device.random_address)

  @classmethod
  async def create(
      cls,
      adapter: CrownAdapter,
      config: bumble_device.DeviceConfiguration | None = None,
      start_timeout: float = 10.0,
  ) -> Self:
    """Creates a CrownDevice instance with a given adapter.

    Args:
      adapter: Adapter between Bumble host and Android controller.
      config: Bumble device configuration.
      start_timeout: Timeout for the device to start.

    Returns:
      A CrownDevice instance.
    """
    instance = cls(adapter, config)

    @retry.retry_on_exception()
    async def inner() -> None:
      async with asyncio.timeout(start_timeout):
        await instance.open()

    await inner()
    return instance

  @classmethod
  async def from_android_device(
      cls: Type[Self],
      device: android_device.AndroidDevice,
      config: bumble_device.DeviceConfiguration | None = None,
      start_timeout: float = 10.0,
  ) -> Self:
    """Creates a CrownDevice instance from an Android device.

    Args:
      device: Android device to create the CrownDevice instance from.
      config: Bumble device configuration.
      start_timeout: Timeout for the device to start.

    Returns:
      A CrownDevice instance.
    """
    return await cls.create(AndroidCrownAdapter(device), config, start_timeout)


class CrownAdapter:
  """Base Adapter class for CrownDevice without additional setup.

  Attributes:
    hci_spec: Bumble transport spec of the exposed HCI socket.
  """

  def __init__(self, spec: str) -> None:
    self._hci_spec = spec

  @property
  def hci_spec(self) -> str:
    return self._hci_spec

  def stop(self) -> None:
    """Stops the adapter."""
    # Doesn't need to do anything here.


class AndroidCrownAdapter(CrownAdapter):
  """Adapter launching HCI proxy on Android to adapt Bumble.

  Attributes:
    ad: Android Mobly raw device.
    hci_spec: Bumble transport spec of the exposed HCI socket.
  """

  _socket_name: str
  ad: android_device.AndroidDevice

  def __init__(self, device: android_device.AndroidDevice) -> None:
    self.ad = device
    self.ad.adb.root()

    # Sync time.
    adb_snippets.sync_time(self.ad)

    # Enable Satellite Mode to avoid turning on Bluetooth.
    self.ad.adb.shell(
        ['settings', 'put', 'global', 'satellite_mode_enabled', '1']
    )
    # Disable Bluetooth auto on.
    self.ad.adb.shell(
        ['settings', 'put', 'secure', 'bluetooth_automatic_turn_on', '0']
    )
    self.ad.adb.shell(['cmd', 'bluetooth_manager', 'disable'])
    self.ad.adb.shell(['cmd', 'bluetooth_manager', 'wait-for-state:STATE_OFF'])

    # Push HCI proxy to device.
    abi_type = self.ad.adb.getprop('ro.product.cpu.abi')
    file_path = resources.GetResourceFilename(
        _HCI_PROXY_G3_PATH_PREFIX + abi_type
    )
    self.ad.adb.push([file_path, _HCI_PROXY_DEVICE_PATH], timeout=60)
    if b'ok' not in self.ad.adb.shell(
        f'test -x {_HCI_PROXY_DEVICE_PATH} && echo ok'
    ):
      self.ad.adb.shell(f'chmod +x {_HCI_PROXY_DEVICE_PATH}')

    self._hci_proxy_process = subprocess.Popen(
        [
            'adb',
            '-s',
            self.ad.serial,
            'shell',
            f'{_HCI_PROXY_DEVICE_PATH} @{_DEVICE_ABSTRACT_SOCKET_PATH}',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
    )
    # Wait for HCI proxy to start.
    time.sleep(1.0)

    if sys.platform == 'linux':
      # Select a random socket name to avoid collision.
      self._socket_name = str(uuid.uuid4())
      self.ad.adb.forward([
          f'localabstract:{self._socket_name}',
          f'localabstract:{_DEVICE_ABSTRACT_SOCKET_PATH}',
      ])
      super().__init__(f'unix:@{self._socket_name}')
    else:
      # ADB doesn't support UNIX socket on Windows, so we need to use TCP.
      self._socket_name = (
          self.ad.adb.forward([
              'tcp:0',
              f'localabstract:{_DEVICE_ABSTRACT_SOCKET_PATH}',
          ])
          .decode('utf-8')
          .strip()
      )
      super().__init__(f'tcp-client:127.0.0.1:{self._socket_name}')

  @override
  def stop(self) -> None:
    """Stops the HCI Proxy."""
    # Kill HCI proxy.
    self.ad.adb.shell(['killall', 'hci_proxy'])
    # Remove forwarding.
    if sys.platform == 'linux':
      self.ad.adb.forward(['--remove', f'localabstract:{self._socket_name}'])
    else:
      self.ad.adb.forward(['--remove', f'tcp:{self._socket_name}'])
    self._hci_proxy_process.kill()
    # Disable Satellite Mode.
    self.ad.adb.shell(
        ['settings', 'put', 'global', 'satellite_mode_enabled', '0']
    )
