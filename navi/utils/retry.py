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

"""Retry decorators."""

import asyncio
import functools
import inspect
import logging
import math
import time
from typing import Any, Callable, TypeVar

_F = TypeVar('_F', bound=Callable[..., Any])


def retry_on_exception(
    initial_delay_sec: float = 1.0,
    num_retries: int = 3,
    max_delay_sec: float = math.inf,
    exception_type: type[Exception] = Exception,
    log_exception: bool = True,
) -> Callable[[_F], _F]:
  """Decorator to retry a function on exception.

  Args:
    initial_delay_sec: The initial delay in seconds.
    num_retries: The number of retries.
    max_delay_sec: The maximum delay in seconds.
    exception_type: The type of exception to retry on.
    log_exception: Whether to log the exception.

  Returns:
    The decorated function.
  """

  def decorator(func: _F) -> _F:
    if inspect.iscoroutinefunction(func):

      @functools.wraps(func)
      async def wrapper(*args, **kwargs):
        for i in range(num_retries + 1):
          try:
            return await func(*args, **kwargs)
          except exception_type:  # pylint: disable=broad-exception-caught
            if log_exception:
              logging.exception(
                  'Retrying function %s, attempt %d of %d',
                  func,
                  i + 1,
                  num_retries,
              )
            if i < num_retries:
              await asyncio.sleep(
                  min(initial_delay_sec * (2**i), max_delay_sec)
              )
            else:
              raise

    else:

      @functools.wraps(func)
      def wrapper(*args, **kwargs):
        for i in range(num_retries + 1):
          try:
            return func(*args, **kwargs)
          except exception_type:  # pylint: disable=broad-exception-caught
            if log_exception:
              logging.exception(
                  'Retrying function %s, attempt %d of %d',
                  func,
                  i + 1,
                  num_retries,
              )
            if i < num_retries:
              time.sleep(min(initial_delay_sec * (2**i), max_delay_sec))
            else:
              raise

    return wrapper

  return decorator
