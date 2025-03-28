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

"""Helper classes for Bumble GATT."""

from typing import Self, Type
from bumble import gatt_client
from navi.utils import pyee_extensions


class MutableCharacteristicState(
    pyee_extensions.EventTriggeredValueObserver[bytes]
):
  """Mutable characteristic state."""

  def __init__(self, characteristic: gatt_client.CharacteristicProxy):
    self._characteristic = characteristic
    self.value = b""

    # Register first so that the value is updated before the observer is
    # triggered.
    characteristic.on("update", self._on_update)
    super().__init__(
        emitter=self._characteristic,
        event="update",
        value_producer=lambda: self.value,
    )

  def _on_update(self, value: bytes) -> None:
    self.value = value

  @classmethod
  async def create(
      cls: Type[Self], characteristic: gatt_client.CharacteristicProxy
  ) -> Self:
    instance = cls(characteristic)
    instance.value = await instance._characteristic.read_value()
    await instance._characteristic.subscribe()
    return instance
