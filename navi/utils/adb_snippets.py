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

"""Bluetooth ADB helper snippets."""

import contextlib
import datetime
import logging
import pathlib
import re
import time
from typing import cast

from mobly.controllers import android_device
from mobly.controllers.android_device_lib import adb

from navi.utils import retry

_logger = logging.getLogger('AdbSnippets')


def sync_time(device: android_device.AndroidDevice) -> None:
  """Sync device time and timezone with host machine.

  Args:
    device: Android device to sync time.
  """
  device.adb.shell([
      'date',
      '-u',
      datetime.datetime.now(tz=datetime.timezone.utc).strftime('%m%d%H%M%Y.%S'),
  ])
  # Sync timezone.
  # Blaze test always uses "Google Standard Time" - America/Los_Angeles.
  device.adb.shell(['setprop', 'persist.sys.timezone', 'America/Los_Angeles'])


def enable_btsnoop(device: android_device.AndroidDevice) -> None:
  """Enable Bluetooth snoop logging on Android device.

  Args:
    device: Android device to enable btsnoop.
  """
  device.adb.shell(['setprop', 'persist.bluetooth.btsnooplogmode', 'full'])


def download_btsnoop(
    device: android_device.AndroidDevice,
    destination_base_path: str,
    filename_prefix: str = '',
) -> None:
  """Download Bluetooth snoop log from Android device.

  Args:
    device: Android device to download log.
    destination_base_path: destination base path.
    filename_prefix: (Optional) destination file name prefix.
  """
  filename = '_'.join(
      ([filename_prefix] if filename_prefix else [])
      + [device.serial, 'btsnoop']
  )

  device_snoop_paths = [
      '/data/misc/bluetooth/logs/btsnoop_hci.log',
      '/data/misc/bluetooth/logs/btsnoop_hci.log.last',
      '/data/vendor/bluetooth/btsnoop_hci_vnd.log',
      '/data/vendor/bluetooth/btsnoop_hci_vnd.log.last',
  ]
  host_snoop_paths = [
      str(pathlib.Path(destination_base_path) / f'{filename}.log'),
      str(pathlib.Path(destination_base_path) / f'{filename}.log.last'),
      str(pathlib.Path(destination_base_path) / f'{filename}_vnd.log'),
      str(pathlib.Path(destination_base_path) / f'{filename}_vnd.log.last'),
  ]

  for device_snoop_path, host_snoop_path in zip(
      device_snoop_paths, host_snoop_paths
  ):
    # If target file doesn't exist, an AdbError will be raised.
    with contextlib.suppress(adb.AdbError):
      device.adb.pull([device_snoop_path, host_snoop_path])


def download_dumpsys(
    device: android_device.AndroidDevice,
    destination_base_path: str,
    filename: str | None = None,
) -> None:
  """Download Bluetooth Dumpsys Information from Android device.

  Args:
    device: Android device to download log.
    destination_base_path: destination base path.
    filename: (Optional) destination file name.
  """
  if filename is None:
    filename = f'{device.serial}_dumpsys.txt'

  # If target file doesn't exist, an AdbError will be raised.
  with contextlib.suppress(adb.AdbError):
    with open(
        str(pathlib.Path(destination_base_path).joinpath(filename)),
        'wb',
    ) as outfile:
      outfile.write(device.adb.shell(['dumpsys', 'bluetooth_manager']))


def enable_bluetooth(
    device: android_device.AndroidDevice, enable: bool
) -> None:
  """Enable or disable Bluetooth on Android device.

  Args:
    device: Android device to toggle Bluetooth.
    enable: Target state.
  """
  device.adb.shell(
      ['cmd', 'bluetooth_manager', 'enable' if enable else 'disable']
  )
  try:
    device.adb.shell([
        'cmd',
        'bluetooth_manager',
        f'wait-for-state:STATE_{"ON" if enable else "OFF"}',
    ])
  except android_device.adb.AdbError:
    time.sleep(1)


@retry.retry_on_exception(initial_delay_sec=1, num_retries=3, max_delay_sec=10)
def connect_to_wifi(
    device: android_device.AndroidDevice,
    wifi_ssid: str,
    wifi_password: str,
) -> None:
  """Connect to wifi network.

  Args:
    device: Android device to connect to wifi.
    wifi_ssid: Wifi network name.
    wifi_password: Wifi network password.

  Raises:
    RuntimeError: If failed to connect to wifi.
  """
  wifi_status = device.adb.shell('cmd wifi status').decode('utf8')
  wifi_enable_pattern = 'Wifi is enabled'
  wifi_connected_pattern = r'Wifi is connected to \"%s\"' % wifi_ssid
  if re.search(wifi_enable_pattern, wifi_status) is None:
    device.adb.shell('svc wifi enable')
    time.sleep(10)
  else:
    if re.search(wifi_connected_pattern, wifi_status) is not None:
      _logger.info('%s is already connected to wifi.', device.serial)
      return

  _logger.info(
      'Trying to connect %s to network %s with password %s',
      device.serial,
      wifi_ssid,
      '*' * len(wifi_password)  # Mask password in logs
      if wifi_password
      else 'open',
  )
  if not wifi_password:
    device.adb.shell(
        f'cmd wifi connect-network {wifi_ssid} open',
        timeout=30,
    )
  else:
    device.adb.shell(
        f'cmd wifi connect-network {wifi_ssid} wpa2 {wifi_password}',
        timeout=30,
    )

  time.sleep(30)
  wifi_status = device.adb.shell('cmd wifi status').decode('utf8')
  if not re.search(wifi_connected_pattern, wifi_status):
    raise RuntimeError('Failed to connect to wifi.')

  _logger.info('Successfully connected %s to wifi.', device.serial)


def get_bluetooth_flags(
    device: android_device.AndroidDevice,
) -> dict[str, bool]:
  """Get Bluetooth flags from Android device."""
  pattern = re.compile(r'\[(■| )\]: (\w+)')

  output = cast(
      bytes,
      device.adb.shell(
          "dumpsys bluetooth_manager | sed -n '/🚩Flag dump:/,/^$/p'"
      ),
  ).decode('utf8')
  return {match[1]: match[0] == '■' for match in pattern.findall(output)}
