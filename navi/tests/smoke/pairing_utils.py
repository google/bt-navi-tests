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

"""Common pairing helper classes."""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import logging

from bumble import pairing
from mobly import asserts
from typing_extensions import override


class PairingVariant(enum.IntEnum):
  UNKNOWN = 0
  JUST_WORK = 1
  NUMERIC_COMPARISON = 2
  PASSKEY_ENTRY_REQUEST = 3
  PIN_CODE_REQUEST = 4
  PASSKEY_ENTRY_NOTIFICATION = 5


@dataclasses.dataclass
class PairingEvent:
  variant: PairingVariant
  arg: str | int | bool | None


class PairingDelegate(pairing.PairingDelegate):
  """A Bumble pairing delegator, providing pairing test flow control."""

  logger = logging.getLogger(__name__)

  def __init__(
      self,
      auto_accept: bool,
      io_capability: (
          pairing.PairingDelegate.IoCapability
      ) = pairing.PairingDelegate.NO_OUTPUT_NO_INPUT,
      local_initiator_key_distribution: (
          pairing.PairingDelegate.KeyDistribution
      ) = pairing.PairingDelegate.DEFAULT_KEY_DISTRIBUTION,
      local_responder_key_distribution: (
          pairing.PairingDelegate.KeyDistribution
      ) = pairing.PairingDelegate.DEFAULT_KEY_DISTRIBUTION,
  ) -> None:
    super().__init__(
        io_capability,
        local_initiator_key_distribution,
        local_responder_key_distribution,
    )
    self.auto_accept = auto_accept
    self.pairing_events = asyncio.Queue[PairingEvent]()
    self.pairing_answers = asyncio.Queue[PairingEvent]()
    self.acceptions = asyncio.Queue[bool]()

  @override
  async def accept(self) -> bool:
    return self.auto_accept or await self.acceptions.get()

  @override
  async def confirm(self, auto: bool = False) -> bool:
    self.logger.debug(
        'Pairing event: `just_works` (io_capability: %s)', self.io_capability
    )

    self.pairing_events.put_nowait(
        PairingEvent(variant=PairingVariant.JUST_WORK, arg=None)
    )
    answer = await self.pairing_answers.get()
    asserts.assert_equal(answer.variant, PairingVariant.JUST_WORK)
    assert isinstance(answer.arg, bool)
    return answer.arg

  @override
  async def compare_numbers(self, number: int, digits: int = 6) -> bool:
    self.logger.debug(
        'Pairing event: `numeric_comparison` (io_capability: %s)',
        self.io_capability,
    )

    self.pairing_events.put_nowait(
        PairingEvent(
            variant=PairingVariant.NUMERIC_COMPARISON,
            arg=number,
        )
    )
    answer = await self.pairing_answers.get()
    asserts.assert_equal(answer.variant, PairingVariant.NUMERIC_COMPARISON)
    assert isinstance(answer.arg, int)
    return answer.arg == number

  @override
  async def get_number(self) -> int | None:
    self.logger.debug(
        'Pairing event: `passkey_entry_request` (io_capability: %s)',
        self.io_capability,
    )

    self.pairing_events.put_nowait(
        PairingEvent(
            variant=PairingVariant.PASSKEY_ENTRY_REQUEST,
            arg=None,
        )
    )
    answer = await self.pairing_answers.get()
    asserts.assert_equal(answer.variant, PairingVariant.PASSKEY_ENTRY_REQUEST)
    assert isinstance(answer.arg, int | None)
    return answer.arg

  @override
  async def get_string(self, max_length: int) -> str | None:
    self.logger.debug(
        'Pairing event: `pin_code_request` (io_capability: %s)',
        self.io_capability,
    )

    self.pairing_events.put_nowait(
        PairingEvent(
            variant=PairingVariant.PIN_CODE_REQUEST,
            arg=None,
        )
    )
    answer = await self.pairing_answers.get()
    asserts.assert_equal(answer.variant, PairingVariant.PIN_CODE_REQUEST)
    assert isinstance(answer.arg, str | None)
    return answer.arg

  @override
  async def display_number(self, number: int, digits: int = 6) -> None:
    self.logger.debug(
        'Pairing event: `passkey_entry_notification` (io_capability: %s)',
        self.io_capability,
    )

    self.pairing_events.put_nowait(
        PairingEvent(
            variant=PairingVariant.PASSKEY_ENTRY_NOTIFICATION,
            arg=number,
        )
    )
