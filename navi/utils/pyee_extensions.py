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

"""Extended helpers for pyee module."""

import asyncio
from collections.abc import Callable
import functools
from typing import Any, Generic, Self, TypeVar
import bumble.utils
import pyee


class EventWatcher(bumble.utils.EventWatcher):
  """pyee EventWatcher wrapped as context manager."""

  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type: Any, exc_value: Any, exc_traceback: Any) -> None:
    self.close()

  def async_monitor(
      self,
      emitter: pyee.EventEmitter,
      event: str,
      predicate: Callable[..., bool] | None = None,
  ) -> asyncio.Queue[None]:
    """Monitor events as an async queue putting items on expected events emitted.

    Args:
      emitter: Event emitter.
      event: Name of event.
      predicate: Function determining whether an event should be monitored. If
        not passed, all events will be emitted.

    Returns:
      An async queue spawning None when events happened.
    """
    queue = asyncio.Queue[None]()
    predicate = predicate or (lambda *args, **kwargs: True)

    def handler(
        queue: asyncio.Queue[None],
        predicate: Callable[..., bool],
        *args,
        **kwargs,
    ) -> None:
      if predicate(*args, **kwargs):
        queue.put_nowait(None)

    self.on(
        emitter,
        event,
        functools.partial(handler, queue, predicate),
    )

    return queue


_T = TypeVar("_T")


class EventTriggeredValueObserver(Generic[_T]):
  """Observer for a value that is triggered by an event."""

  def __init__(
      self,
      emitter: pyee.EventEmitter,
      event: str,
      value_producer: Callable[[], _T],
  ) -> None:
    self._value_producer = value_producer
    self.value = self._value_producer()
    self._condition = asyncio.Condition()
    self.emitter = emitter
    self.event = event

    self.emitter.on(self.event, self._on_event)

  @bumble.utils.AsyncRunner.run_in_task()
  async def _on_event(self, *args, **kwargs) -> None:
    del args, kwargs
    self.value = self._value_producer()
    async with self._condition:
      self._condition.notify_all()

  async def wait_for_target_value(
      self, target_value_or_predicate: _T | Callable[[_T], bool]
  ) -> None:
    """Waits for the value to be changed."""
    if callable(target_value_or_predicate):
      predicate = target_value_or_predicate
    else:
      predicate = lambda value: value == target_value_or_predicate
    async with self._condition:
      await self._condition.wait_for(lambda: predicate(self.value))

  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type: Any, exc_value: Any, exc_traceback: Any) -> None:
    self.emitter.remove_listener(self.event, self._on_event)
