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

"""Mobly controller module for a USB device."""

from __future__ import annotations

from collections.abc import Sequence
import dataclasses
from typing import Any

# Used in the config file.
MOBLY_CONTROLLER_CONFIG_NAME = "UsbDevice"


@dataclasses.dataclass(frozen=True)
class UsbDeviceConfig:
  """Configuration for a UsbDevice.

  Attributes:
    id: The unique identifier of the USB device.
  """

  id: str


def create(configs: Sequence[dict[str, Any]]) -> list[UsbDevice]:
  """Creates UsbDevice controller objects.

  Args:
    configs: A list of dicts, each representing a configuration for a USB
      device.

  Returns:
    A list of UsbDevice objects.
  """
  return [
      UsbDevice(UsbDeviceConfig(id=config.get("id", ""))) for config in configs
  ]


def destroy(devices: Sequence[UsbDevice]) -> None:
  """Destroys UsbDevice objects.

  Args:
    devices: A list of UsbDevice objects.
  """
  del devices


def get_info(devices: Sequence[UsbDevice]) -> list[dict[str, str]]:
  """Gets info from the UsbDevice objects.

  Args:
    devices: A list of UsbDevice objects.

  Returns:
    A list of dicts representing info for each device.
  """
  return [device.get_info() for device in devices]


def _parse_hci_spec_from_usb_id(usb_id: str) -> str:
  """Parses a HCI transport spec from a USB ID.

  Mobile Harness USB ID can be a local path ("usb:<bus>-<port>"), a remote
  path ("<hostname>:usb:<bus>-<port>"), or a unique serial number
  ("<serial>"). For paths, strip the hostname prefix to get a valid local
  HCI spec.

  Args:
    usb_id: The USB ID to parse.

  Returns:
    The HCI transport spec.
  """
  if "usb:" in usb_id:
    return f"usb:{usb_id.split('usb:')[-1]}"
  else:
    # TODO: Handle raw serial string.
    return usb_id


class UsbDevice:
  """Mobly controller for a USB device."""

  config: UsbDeviceConfig
  hci_spec: str

  def __init__(self, config: UsbDeviceConfig) -> None:
    """Initializes the instance.

    Args:
      config: Represents the configuration for a USB device.
    """
    self.config = config
    self.hci_spec = _parse_hci_spec_from_usb_id(config.id)

  def get_info(self) -> dict[str, str]:
    """Gets the device info."""
    return {"id": self.config.id, "hci_spec": self.hci_spec}
