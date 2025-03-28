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

"""Exceptions raise when for internal errors, or extended assertion failures."""

# pylint: disable=W0622


class AsyncTimeoutError(AssertionError):
  """Raised when an asynchrounous operation timeout but expected not to timeout."""


class ConnectionError(AssertionError):
  """Raised when an operation fails due to a connection error."""


class BumbleError(AssertionError):
  """Raised when an operation fails due to a Bumble error."""


class NotFoundError(AssertionError):
  """Raised when some elements are not found."""


class CancelledError(AssertionError):
  """Raised when an operation is cancelled."""
