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

"""Extended Bluetooth Host Controller Interface (HCI) commands."""

from __future__ import annotations

from collections.abc import Callable
import dataclasses
from typing import Any, ClassVar, Self, Tuple, TypeVar

from bumble import hci
from typing_extensions import override


def parse_address_followed_by_type(
    data: bytes, offset: int = 0
) -> Tuple[int, hci.Address]:
  return offset + 7, hci.Address(data[offset : offset + 6], data[offset + 6])


class _HciPacket(hci.HCI_Packet):
  """Base extended HCI packet."""

  PARSE_OFFSET: ClassVar[int] = 0

  @override
  @classmethod
  def from_parameters(cls: type[Self], parameters: bytes) -> Self:
    offset = cls.PARSE_OFFSET
    values = []
    for field in dataclasses.fields(cls):
      value, size = hci.HCI_Object.parse_field(
          parameters, offset, field.type.__metadata__[0]
      )
      offset += size
      values.append(value)
    return cls(*values)

  @property
  def name(self) -> str:
    return self.__class__.__name__

  @property
  def parameters(self) -> bytes:
    return b"".join(
        hci.HCI_Object.serialize_field(getattr(self, field_name), field_type)
        for field_name, field_type in self.fields
    )

  @property
  def fields(self) -> list[Tuple[str, Any]]:
    return [
        (field.name, field.type.__metadata__[0])
        for field in dataclasses.fields(self)
    ]


class Command(_HciPacket, hci.HCI_Command):
  """Base extended HCI command."""


_VE = TypeVar("_VE", bound=hci.HCI_Vendor_Event)


class VendorEvent(_HciPacket, hci.HCI_Vendor_Event):
  """Base extended HCI Vendor Event."""

  PARSE_OFFSET = 1

  @classmethod
  def register(cls: type[Self]) -> Callable[[type[_VE]], type[_VE]]:
    """Registers the VendorEvent with the HCI module."""

    def decorator(clazz: type[_VE]) -> type[_VE]:
      hci.HCI_Event.add_vendor_factory(clazz.from_parameters)
      return clazz

    return decorator
