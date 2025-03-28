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

"""Utils for logcat."""

import asyncio
from collections.abc import AsyncGenerator
import contextlib
import dataclasses
import logging
import logging.handlers
import re
import subprocess
from typing import Self, cast
from mobly.controllers import android_device
from mobly.controllers.android_device_lib.services import base_service
from typing_extensions import override


class LogcatForwardingService(base_service.BaseService):
  """A service forwarding Python logging to the device logcat."""

  @dataclasses.dataclass(frozen=True)
  class Config:
    tag: str = "NaviTest"

  _process: subprocess.Popen[str] | None = None

  def __init__(
      self, device: android_device.AndroidDevice, configs: Config | None = None
  ) -> None:
    self.device = device
    self.configs = configs or LogcatForwardingService.Config()
    self._is_alive = False

  @property
  def is_alive(self) -> bool:
    return self._is_alive

  @override
  def start(self) -> None:
    device_cmd = f"log -t {self.configs.tag}"
    adb_cmd = ["adb", "-s", self.device.serial, "shell", device_cmd]
    self._process = subprocess.Popen(
        adb_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )

    if not self._process.stdin:
      raise RuntimeError("Failed to start logcat forwarding service.")

    self._logcat_handler = logging.StreamHandler(self._process.stdin)
    self._logcat_handler.setLevel(logging.INFO)
    logging.root.addHandler(self._logcat_handler)
    self._is_alive = True

  @override
  def stop(self) -> None:
    if self._logcat_handler is not None:
      logging.root.removeHandler(self._logcat_handler)
      self._logcat_handler = None

    if self._process is not None:
      self._process.kill()
      self._process.wait()
      self._process = None
    self._is_alive = False

  @classmethod
  def register(
      cls,
      device: android_device.AndroidDevice,
      configs: Config | None = None,
      alias: str = "logcat_forwarding",
      start_service: bool = True,
  ) -> Self:
    """Registers the logcat forwarding service to the device.

    Args:
      device: Android device to register the logcat forwarding service to.
      configs: Configuration for the logcat forwarding service.
      alias: Alias of the logcat forwarding service.
      start_service: Whether to start the logcat forwarding service.

    Returns:
      The registered logcat forwarding service.
    """
    device.services.register(alias, cls, configs, start_service)
    return cast(LogcatForwardingService, getattr(device.services, alias))


@contextlib.asynccontextmanager
async def subscribe_logcat(
    device: android_device.AndroidDevice, pattern: str | re.Pattern[str]
) -> AsyncGenerator[asyncio.Future[str], None]:
  """Subscribes to logcat and yields lines that match the pattern.

  Args:
    device: Android device to subscribe to logcat.
    pattern: Pattern to match against.

  Yields:
    A future that resolves to the first line that matches the pattern.

  Raises:
    RuntimeError: If failed to start logcat pubsub.
  """
  cmds = ["adb", "-s", device.serial, "logcat", "-T", "1"]
  process = await asyncio.create_subprocess_exec(
      *cmds,
      stdin=asyncio.subprocess.PIPE,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
  )
  task = None
  if not (stdout := process.stdout):
    raise RuntimeError("Failed to start logcat pubsub.")
  if isinstance(pattern, str):
    pattern = re.compile(pattern)
  try:
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def reader() -> None:
      while True:
        line = (await stdout.readline()).decode("utf-8")
        if not line:
          break
        if pattern.search(line):
          future.set_result(line)
          break

    task = asyncio.create_task(reader())
    yield future

  finally:
    # Clean up the process.
    process.kill()
    await process.wait()
    if task and not task.done():
      task.cancel()
