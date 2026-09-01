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

"""Base classes for Bluetooth tests."""

import abc
import asyncio
import collections
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterator, Sequence
import contextlib
import dataclasses
import datetime
import enum
import functools
import importlib
import inspect
import json
import logging
import pathlib
import re
import secrets
import sys
import textwrap
from typing import Any, ClassVar, Generic, Never, TypeAlias, TypeVar, cast, final
import uuid

from absl.testing import absltest
from bumble import pairing
import bumble.core
import bumble.device
import bumble.hci
from mobly import base_test
from mobly import expects
from mobly import records
from mobly import runtime_test_info
from mobly import signals
from mobly.controllers import android_device
from mobly.controllers.android_device_lib import adb
from mobly.controllers.android_device_lib import apk_utils
import mobly.snippet.errors
from snippet_uiautomator import uiautomator
from typing_extensions import override

from navi.bumble_ext import crown
from navi.utils import adb_snippets
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import errors
from navi.utils import logcat
from navi.utils import matcher
from navi.utils import retry as retry_lib
from navi.utils import snippet_stub
from navi.utils import usb_device

_NAVI_TIMEOUT = "_NAVI_TIMEOUT"
_NAVI_PARAMETERIZED = "_NAVI_PARAMETERIZED"
_NAVI_REQUIRE_FLAG = "_NAVI_REQUIRE_FLAG"
_SETUP_TIMEOUT_SECONDS = 15.0
# 100 * 0.625ms = 62.5ms
_DEFAULT_ADVERTISING_INTERVAL = 100
RECORD_FULL_DATA = "record_full_data"
DUMP_CROWN_LOG_ON_FAIL = "dump_crown_log_on_fail"
CUSTOM_TEST_SESSION = "custom_test_session"
PROPERTY_OVERRIDES = "property_overrides"
# The delay in seconds for CrownDevice (currently only for PASS_THROUGH and
# USB) to sleep between reset, mostly for settlement.
CROWN_RESET_DELAY = "crown_reset_delay"
_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
# The original method name of the test case. This is used to record the test
# method name in Sponge.
_ORIGINAL_METHOD_NAME = "_original_method_name"

_FUNC = TypeVar("_FUNC", bound=Callable[..., Any])


class CrownDriver(enum.StrEnum):
  ANDROID = "android"
  PASSTHROUGH = "passthrough"
  USB = "usb"
  CF_ROOTCANAL = "cf_rootcanal"


@dataclasses.dataclass
class RecordData:
  """Wrapper of data to be uploaded to Sponge."""

  properties: dict[str, Any]
  test_name: str | None = None
  test_class: str | None = None


@dataclasses.dataclass
class AFlag:
  name: str
  enabled: bool
  writable: bool


_TC = TypeVar("_TC", bound=base_test.BaseTestClass)


class CustomTestSession(Generic[_TC], abc.ABC):
  """Custom test session interface."""

  def __init__(self, test_class: _TC) -> None:
    self.test_class = test_class

  def setup_test(self) -> None:
    """Sets up the test.

    This method is called before each test case is executed, after the original
    setup_test method in the test class.
    """

  def teardown_test(self) -> None:
    """Tears down the test.

    This method is called after each test case is executed, before the original
    teardown_test method in the test class.
    """

  def setup_class(self) -> None:
    """Sets up the class.

    This method is called before any test in the class is executed, after the
    original setup_class method in the test class.
    """

  def teardown_class(self) -> None:
    """Tears down the class.

    This method is called after all tests in the class are executed, before the
    original teardown_class method in the test class.
    """


class AndroidSnippetDeviceWrapper:
  """Wrapper for Android device under test."""

  device: android_device.AndroidDevice
  _SNIPPET_NAME: ClassVar[str] = "bt"
  _UI_AUTOMATOR_NAME: ClassVar[str] = "ui"

  @retry_lib.retry_on_exception(initial_delay_sec=1, num_retries=3)
  def __init__(self, device: android_device.AndroidDevice) -> None:
    self.device = device
    # Enable debug features if applicable.
    self.enable_debug()
    # Load Bluetooth Snippet.
    self.device.load_snippet(
        self._SNIPPET_NAME, android_constants.PACKAGE_NAME_BLUETOOTH_SNIPPET
    )
    self.bl4a = bl4a_api.SnippetWrapper(self.bt)
    # Register UI Automation service.
    custom_snippet = uiautomator.Snippet(
        package_name=android_constants.PACKAGE_NAME_BLUETOOTH_SNIPPET,
        ui_public_service_name=self._UI_AUTOMATOR_NAME,
        custom_service_name=self._SNIPPET_NAME,
    )
    self.device.services.register(
        uiautomator.ANDROID_SERVICE_NAME,
        uiautomator.UiAutomatorService,
        uiautomator.UiAutomatorConfigs(
            snippet=custom_snippet, skip_installing=True
        ),
    )
    self.ui = cast(uiautomator.UiDevice, self.device.ui)

    # Skip OOBE.
    with contextlib.suppress(adb.AdbError):
      if self.is_watch:
        self.shell(
            "am broadcast -a com.google.android.clockwork.action.TEST_MODE"
        )
      else:
        self.shell("am start -a com.android.setupwizard.FOUR_CORNER_EXIT")

  def enable_debug(self) -> None:
    """Enables debug features if the build is userdebug or eng."""
    if self.device.is_rootable:
      self.device.root_adb()
      # Sync time.
      adb_snippets.sync_time(self.device)
      # Disable Selinux.
      self.shell("setenforce 0")
      # Enable BT Snoop.
      adb_snippets.enable_btsnoop(self.device)
      # Enable BT verbose logging.
      self.shell("setprop persist.log.tag.bluetooth VERBOSE")
    else:
      logging.warning(
          "Device is not rootable. Skipping automatic debug configuration:\n"
          "1. Time synchronization was not performed (ensure DUT time matches "
          "host to prevent log timestamp issues).\n"
          "2. Bluetooth Snoop logs and verbose logging were not enabled "
          "(can be manually enabled in Developer Options)."
      )

  @property
  def adb(self) -> adb.AdbProxy:
    return self.device.adb

  @property
  def bt(self) -> snippet_stub.BluetoothSnippet:
    return self.device.bt

  @property
  def is_le_audio_supported(self) -> bool:
    """Checks if the device supports LE Audio."""
    if (
        self.getprop(android_constants.Property.BAP_UNICAST_CLIENT_ENABLED)
        != "true"
    ):
      return False
    if (
        self.getprop(
            android_constants.Property.LEAUDIO_LE_AUDIO_CONNECTION_BY_DEFAULT
        )
        == "false"
    ):
      return False

    if (
        self.bt.getSdkVersion() >= 35
        and android_constants.AudioDeviceType.BLE_HEADSET
        not in self.bt.getSupportedAudioDeviceTypes(
            android_constants.AudioDeviceRole.OUTPUT
        )
    ):
      return False
    return True

  @functools.cached_property
  def address(self) -> str:
    return self.device.bt.getAddress()

  @functools.cached_property
  def is_watch(self) -> bool:
    return "watch" in self.device.build_info["build_characteristics"]

  def reload_snippet(self) -> None:
    """Unloads and reloads the snippet."""
    self.device.unload_snippet(self._SNIPPET_NAME)
    self.device.load_snippet(
        self._SNIPPET_NAME, android_constants.PACKAGE_NAME_BLUETOOTH_SNIPPET
    )
    # Reload BL4A.
    self.bl4a = bl4a_api.SnippetWrapper(self.bt)

  def shell(self, args: str | Sequence[str]) -> str:
    """Executes a shell command and returns the output."""
    return self.adb.shell(args).decode("utf-8").strip()

  def getprop(self, prop_name: str) -> str:
    """Gets a property of the device."""
    return self.adb.getprop(prop_name)

  def get_int_prop(self, prop_name: str) -> int | None:
    """Gets a property of the device."""
    text = self.getprop(prop_name)
    if not text:  # Empty string or None
      return None
    elif text.isdigit():
      return int(text)
    elif text.startswith("0x"):
      return int(text, 16)
    else:
      raise ValueError(f"Invalid property value: {text}")

  def setprop(self, prop_name: str, prop_value: str) -> None:
    """Sets a property of the device."""
    self.adb.shell(["setprop", prop_name, f"'{prop_value}'"])

  @property
  @retry_lib.retry_on_exception(initial_delay_sec=1, num_retries=3)
  def firmware_version(self) -> str | None:
    """Version of the Bluetooth firmware.

    Note: This might fail if HAL changes its output format.

    Raises:
      TestFailure: If Bluetooth cannot be enabled.
    """
    # Bluetooth must be on to get firmware version.
    if not self.bt.enable():
      self.bt.waitForAdapterState(android_constants.AdapterState.ON)
      raise signals.TestFailure("Failed to enable Bluetooth")
    with contextlib.suppress(adb.AdbError):
      response = self.shell(
          "dumpsys android.hardware.bluetooth.IBluetoothHci/default | "
          "egrep -i 'Controller Firmware Information' -A 2"
      )
      lines = response.splitlines()
      if len(lines) >= 3:
        return lines[2]
    return None

  @property
  def bluetooth_mainline_version(self) -> int:
    """Version of the Bluetooth prebuilt."""
    with contextlib.suppress(adb.AdbError):
      response = self.shell(
          "pm list packages --apex-only --show-versioncode | egrep -i"
          " android.bt"
      )
      if m := re.search(r"versionCode:(\d+)", response):
        return int(m.group(1))
    return 0

  def get_flags(self) -> dict[str, AFlag]:
    """Gets all aflags."""
    lines = [
        line.split() for line in self.shell(["aflags", "list"]).splitlines()
    ]
    return {
        flag_name: AFlag(
            name=flag_name,
            enabled="enabled" in value,
            writable="read-write" in permission,
        )
        for (
            flag_name,
            value,
            _,  # staged_value
            _,  # provenance
            permission,
            _,  # container
        ) in lines
    }

  def get_flag(self, flag_name: str) -> AFlag | None:
    """Gets a flag.

    If the flag is not found, returns None.

    Args:
      flag_name: The name of the flag to get.

    Returns:
      The flag if found, otherwise None.
    """
    if flag := self.get_flags().get(flag_name):
      return flag
    return None

  def set_flag(
      self, flag_name: str, value: bool | None, immediate: bool = True
  ) -> None:
    """Sets a flag.

    Args:
      flag_name: The name of the flag to set.
      value: The value of the flag to set.
      immediate: Whether to set the flag immediately. If false, the flag will be
        set when the device is rebooted.
    """
    self.shell(
        [
            "aflags",
            {
                True: "enable",
                False: "disable",
                None: "unset",
            }[value],
            flag_name,
        ]
        + (["-i"] if immediate else [])
    )


def parameterized(
    *args_sets,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
  """Parameterizes a test.

  Parameteried test cases will be extended automatially in
  pre_run().
  Parameterzied test cases will be named after `test_name(arg1, arg2, ...)`.

  Args:
    *args_sets: A series of argument sets passed to the test.

  Returns:
    A wrapper patching test cases. The wrapper will return the function patched
    with _NAVI_PARAMETERIZED field for pre_run.

  Raises:
    ValueError: If any of the argument sets is invalid.
  """

  def wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
    param_sets: dict[str, tuple[Sequence[Any], dict[str, Any]]] = {}
    signature = inspect.signature(func)
    for args in args_sets:
      args = args if isinstance(args, Sequence) else (args,)
      # Verify the parameters are valid.
      try:
        signature.bind(None, *args)
      except TypeError as e:
        raise ValueError(
            f"Invalid args: {args}. The signature is {signature}"
        ) from e
      testcase_name = ", ".join(
          [getattr(arg, "name", None) or str(arg) for arg in args]
      )
      # Replace reserved characters with their Unicode equivalents.
      if sys.platform == "win32":
        testcase_name = (
            testcase_name.replace(">", "＞")
            .replace("<", "＜")
            .replace(":", "：")
            .replace('"', "'")
            .replace("/", "／")
            .replace("\\", "＼")
            .replace("|", "｜")
            .replace("?", "？")
            .replace("*", "＊")
        )
      param_sets[testcase_name] = (args, {})
    setattr(func, _NAVI_PARAMETERIZED, (False, param_sets))
    return func

  return wrapper


def named_parameterized(
    *args_sets, **kwargs_sets
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
  """Parameterizes a test with named arguments.

  In args_sets, if the argument is a:
  * Dict, a `testcase_name` key must be present. The test name will be
    `test_name_<testcase_name>`.
  * Sequence, the first element will be used as the test name.

  In kwargs_sets, the key will be used as the test name.

  Args:
    *args_sets: A series of argument sets passed to the test.
    **kwargs_sets: A series of arguments sets passed to the test, key will be
      used as testcase name.

  Returns:
    A wrapper patching test cases. The wrapper will return the function patched
    with _NAVI_PARAMETERIZED field for pre_run.

  Raises:
    ValueError: If any of the argument sets is invalid.
  """

  def wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
    signature = inspect.signature(func)
    param_sets: dict[str, tuple[Sequence[Any], dict[str, Any]]] = {}
    for testcase_name, args in kwargs_sets.items():
      if isinstance(args, dict):
        param_sets[testcase_name] = ((), args)
      elif isinstance(args, Sequence):
        param_sets[testcase_name] = (args, {})
      else:
        param_sets[testcase_name] = ((args,), {})

    for args in args_sets:
      if isinstance(args, dict):
        test_case_name = args.pop("testcase_name")
        param_sets[test_case_name] = ((), args)
      elif isinstance(args, Sequence):
        test_case_name = args[0]
        param_sets[test_case_name] = (args[1:], {})
      else:
        raise ValueError(f"Invalid args: {args}")

    # Verify the parameters are valid.
    for testcase_name, (args, kwargs) in param_sets.items():
      try:
        signature.bind(None, *args, **kwargs)
      except TypeError as e:
        raise ValueError(f"Invalid testcase: {testcase_name}") from e
    setattr(func, _NAVI_PARAMETERIZED, (True, param_sets))
    return func

  return wrapper


def repeat(
    count: int, max_consecutive_error: int | None = None
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
  """A modified version of `mobly.base_test.repeat`, supporting async.

  Args:
    count: the total number of times to execute the decorated test case.
    max_consecutive_error: the maximum number of consecutively failed iterations
      allowed. If reached, the remaining iterations are abandoned. By default
      this is not enabled.

  Returns:
    A wrapper patching test cases.
  """

  def wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
    setattr(func, base_test.ATTR_REPEAT_CNT, count)
    setattr(func, base_test.ATTR_MAX_CONSEC_ERROR, max_consecutive_error)
    return func

  return wrapper


def retry(max_count: int) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
  """A modified version of `mobly.base_test.retry`, supporting async.

  Args:
    max_count: the maximum number of times to execute the decorated test case.

  Returns:
    A wrapper patching test cases.
  """

  def wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
    setattr(func, base_test.ATTR_MAX_RETRY_CNT, max_count)
    return func

  return wrapper


def skip(reason: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
  """A modified version of `unittest.skip`, supporting Mobly.

  Args:
    reason: Reason to skip the test.

  Returns:
    A wrapper patching test cases.
  """

  def wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
    del func

    def skip_func(*args, **kwargs):
      del args, kwargs
      raise signals.TestSkip(reason)

    return skip_func

  return wrapper


def timeout(
    duration: float | datetime.timedelta,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
  """Sets the async timeout for a test.

  Args:
    duration: Timeout duration.

  Returns:
    A wrapper patching test cases.
  """

  def wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
    setattr(func, _NAVI_TIMEOUT, duration)
    return func

  return wrapper


class BaseTestBase(base_test.BaseTestClass, absltest.TestCase):
  """Base class for all test base classes. Should not be used directly."""

  # This attribute is defined by base_test.BaseTestClass and allowed to be None
  # in a very short period after test complete, but it's very inconvenient to
  # handle None case and unlikely to happen in our case. So we just declare it
  # as non-optional here.
  current_test_info: runtime_test_info.RuntimeTestInfo
  user_params: dict[str, Any]
  current_test_method: Callable[[], Any]
  custom_test_session: CustomTestSession | None = None
  TEST_TIMEOUT_SECONDS: ClassVar[float] = 60.0

  def __init__(self, *args, **kwargs) -> None:
    super().__init__(*args, **kwargs)
    self.logger = logging.getLogger(__name__)
    self.loop = asyncio.new_event_loop()
    self.test_case_context = contextlib.AsyncExitStack()
    self.test_class_context = contextlib.AsyncExitStack()
    if custom_test_session_path := self.user_params.get(CUSTOM_TEST_SESSION):
      if not isinstance(custom_test_session_path, str):
        raise ValueError(
            "Custom test class path must be a string, got"
            f" {type(custom_test_session_path)}"
        )
      module_name, class_name = custom_test_session_path.rsplit(".", 1)
      custom_test_session_module = importlib.import_module(module_name)
      custom_test_session_class = getattr(
          custom_test_session_module, class_name
      )
      if not issubclass(custom_test_session_class, CustomTestSession):
        raise ValueError(
            f"Custom test class {custom_test_session_class} must be a"
            f" subclass of {CustomTestSession.__name__}"
        )
      self.custom_test_session = custom_test_session_class(self)

  def _async_test_wrapper(
      self,
      coro_factory: Callable[[], Coroutine[Any, Any, Any]],
      timeout_sec: float,
  ) -> Any:
    try:
      self.loop.run_until_complete(
          asyncio.wait_for(coro_factory(), timeout_sec)
      )
    except (
        TimeoutError,
        bumble.core.TimeoutError,
        bumble.core.CommandTimeoutError,
    ) as e:
      raise errors.AsyncTimeoutError from e
    except bumble.core.BaseError as e:
      raise errors.BumbleError from e
    except asyncio.exceptions.CancelledError as e:
      raise errors.CancelledError from e

  @classmethod
  def require_flag(cls, *args: str) -> Callable[[_FUNC], _FUNC]:
    """Decorator to require one or more flags to be set in the test case.

    If the flag is not present or not writable, the test case will be skipped.

    Args:
      *args: The flags to require.
    """

    def wrapper(func: _FUNC) -> _FUNC:
      setattr(func, _NAVI_REQUIRE_FLAG, args)
      return func

    return wrapper

  def _make_sync_test(
      self,
      test_method: Callable[..., Any],
      *args,
      **kwargs,
  ) -> Callable[..., Any]:
    """Make sure test_method could be executed synchronouly.

    Args:
      test_method: Test method to be wrapped as sync method.
      *args: Arguments to be passed to test_method.
      **kwargs: Keyword arguments to be passed to test_method.

    Returns:
      Synchroized test method if test_method is coroutine function, otherwise
      test_method itself.
    """
    partial_method = functools.partial(test_method, *args, **kwargs)
    if inspect.iscoroutinefunction(test_method):
      timeout_duration = getattr(
          test_method,
          _NAVI_TIMEOUT,
          self.TEST_TIMEOUT_SECONDS,
      )
      if isinstance(timeout_duration, datetime.timedelta):
        timeout_seconds = timeout_duration.total_seconds()
      else:
        timeout_seconds = float(timeout_duration)
      synced_func = functools.partial(
          self._async_test_wrapper, partial_method, timeout_seconds
      )
    else:
      synced_func = partial_method

    # Copy decorative attributes to the wrapped function.
    for attr_name in (
        base_test.ATTR_MAX_RETRY_CNT,
        base_test.ATTR_MAX_CONSEC_ERROR,
        base_test.ATTR_REPEAT_CNT,
        _NAVI_REQUIRE_FLAG,
    ):
      if attr_value := getattr(test_method, attr_name, None):
        setattr(synced_func, attr_name, attr_value)
    # Stores the original function name before wrapping in functools.partial
    # (which lacks __name__).
    if method_name := getattr(test_method, "__name__", None):
      setattr(synced_func, _ORIGINAL_METHOD_NAME, method_name)
    return synced_func

  @override
  def _get_test_methods(
      self, test_names: list[str]
  ) -> list[tuple[str, Callable[..., Any]]]:
    methods = collections.OrderedDict[str, Callable[..., Any]]()
    for test_name in test_names:
      if not test_name.startswith(("test_", "r:", "shard:")):
        raise base_test.Error(
            f"Test method name {test_name} does not follow naming convention"
            " test_* or regex matcher r:(regex pattern), abort."
        )

      if test_name.startswith("r:"):
        test_name_pattern = re.compile(test_name[2:])
        methods.update(
            (test_name, test_method)
            for test_name, test_method in self._generated_test_table.items()
            if test_name_pattern.fullmatch(test_name)
        )
      elif test_name.startswith("shard:"):
        m = re.fullmatch(r"(\d+)/(\d+)", test_name[6:])
        if not m:
          raise ValueError(f"Invalid shard format: {test_name}")
        shard_index = int(m.group(1)) - 1  # 1-index to 0-index
        shard_count = int(m.group(2))
        test_count = len(self._generated_test_table)
        test_per_shard = test_count // shard_count + min(
            test_count % shard_count, 1
        )
        methods.update({
            test_name: test_method
            for test_name, test_method in list(
                self._generated_test_table.items()
            )[test_per_shard * shard_index : test_per_shard * (shard_index + 1)]
        })
      elif test_method := self._generated_test_table.get(test_name):
        methods[test_name] = test_method

    return list(methods.items())

  @override
  def get_existing_test_names(self) -> list[str]:
    # All tests are generated in pre_run().
    return list(self._generated_test_table.keys())

  @override
  def pre_run(self) -> None:
    # Generate parameterized tests.
    for test_name, func in inspect.getmembers(self, callable):
      if not test_name.startswith("test_"):
        continue
      # Having the fields means that's patched by `@parameterized`.
      if hasattr(func, _NAVI_PARAMETERIZED):
        is_named, param_sets = getattr(func, _NAVI_PARAMETERIZED)
        param_sets = cast(
            dict[str, tuple[Sequence[Any], dict[str, Any]]], param_sets
        )
      else:
        # Not a parameterized test, just make it sync.
        self._generated_test_table[test_name] = self._make_sync_test(func)
        continue

      for testcase_name, (args, kwargs) in param_sets.items():
        if is_named:
          new_test_name = f"{test_name}_{testcase_name}"
        else:
          new_test_name = f"{test_name}({testcase_name})"

        self._generated_test_table[new_test_name] = self._make_sync_test(
            func, *args, **kwargs
        )

    # Overwrite max_retry_count for all test methods.
    if max_retry_count := self.user_params.get("max_retry_count"):
      self.logger.info(
          "Setting max_retry_count to %s for all test methods.",
          max_retry_count,
      )
      for test_method in self._generated_test_table.values():
        setattr(
            test_method,
            base_test.ATTR_MAX_RETRY_CNT,
            int(max_retry_count),
        )

  def _get_android_controllers(
      self, counts: int = 1
  ) -> list[android_device.AndroidDevice]:
    """Gets a list of Android controllers for the test."""
    controllers = self.register_controller(android_device, min_number=counts)
    # (Only in Local mode) Returns devices with given serial numbers.
    if serials := self.user_params.get("device_serials", ""):
      controller_maps: dict[str, android_device.AndroidDevice] = {
          controller.serial: controller for controller in controllers
      }
      # If not all serials can be found in the list, an error should be raised.
      return [controller_maps[serial] for serial in serials.split(",")]
    return controllers[:counts]

  @override
  def record_data(self, content: dict[str, Any] | RecordData) -> None:
    content_dict: dict[str, Any]
    if isinstance(content, RecordData):
      content_dict = {
          "properties": content.properties,
      }
      if content.test_name is not None:
        content_dict["Test Name"] = content.test_name
      if content.test_class is not None:
        content_dict["Test Class"] = content.test_class
    else:
      content_dict = content
    super().record_data(content_dict)

  async def async_setup_test(self) -> None:
    """Async test setup stage called before each test, after setup_test()."""

  async def async_teardown_test(self) -> None:
    """Async test teardown stage called after each test, and teardown_test()."""
    await self.test_case_context.aclose()

  async def async_setup_class(self) -> None:
    """Async class setup stage called before all tests, and setup_class()."""

  async def async_teardown_class(self) -> None:
    """Async class teardown stage called after all tests, and teardown_class()."""
    await self.test_class_context.aclose()

  @override
  def skipTest(self, reason: str) -> Never:
    # Mobly doesn't recognize unittest.SkipTest, so we need to raise
    # signals.TestSkip to make it work.
    raise signals.TestSkip(reason)

  @final
  @override
  def setup_test(self) -> None:
    self.loop.run_until_complete(self.async_setup_test())
    if self.custom_test_session:
      with self._log_test_stage("custom_setup_test"):
        self.custom_test_session.setup_test()

  @final
  @override
  def teardown_test(self) -> None:
    if self.custom_test_session:
      with self._log_test_stage("custom_teardown_test"):
        self.custom_test_session.teardown_test()
    self.loop.run_until_complete(self.async_teardown_test())

  @final
  @override
  def setup_class(self) -> None:
    self.loop.run_until_complete(self.async_setup_class())
    if self.custom_test_session:
      with self._log_test_stage("custom_setup_class"):
        self.custom_test_session.setup_class()

  @final
  @override
  def teardown_class(self) -> None:
    if self.custom_test_session:
      with self._log_test_stage("custom_teardown_class"):
        self.custom_test_session.teardown_class()
    self.loop.run_until_complete(self.async_teardown_class())

  @override
  def exec_one_test(self, test_name, test_method, record=None):
    # Save the test method for later use.
    self.current_test_method = cast(Callable[[], Any], test_method)
    original_method_name = getattr(
        test_method,
        _ORIGINAL_METHOD_NAME,
        getattr(test_method, "__name__", None),
    )
    if original_method_name:
      self.record_data(
          RecordData(
              test_name=test_name,
              test_class=self.TAG,
              properties={"test_method": original_method_name},
          )
      )
    return super().exec_one_test(test_name, test_method, record)

  @contextlib.asynccontextmanager
  async def assert_not_timeout(
      self,
      delay: float | None = None,
      msg: str | None = None,
      with_log: bool = True,
  ) -> AsyncGenerator[asyncio.timeouts.Timeout, None]:
    """Create a asyncio.timeout context manager that asserts that the code inside it doesn't timeout.

    This could be used to provide a more readable message for asyncio.timeout or
    asyncio.wait_for.

    Args:
      delay: The timeout delay in seconds.
      msg: The error message to be shown if the code inside the context manager
        times out.
      with_log: Whether to log msg at the beginning of the context manager.

    Yields:
      The raw timeout context manager.
    """
    if with_log and msg is not None:
      self.logger.info(msg)
    try:
      async with asyncio.timeout(delay) as raw_context:
        yield raw_context
    except TimeoutError as e:
      raise self.failureException(
          self._formatMessage(msg, f"Timeout after {delay} seconds")
      ) from e

  @contextlib.asynccontextmanager
  async def assert_timeout(
      self,
      delay: float | None = None,
      msg: str | None = None,
      with_log: bool = True,
  ) -> AsyncGenerator[asyncio.timeouts.Timeout, None]:
    """Create a asyncio.timeout context manager that asserts that the code inside it timeout.

    This could be used to assert some events not happening, for example:
    ```
    some_event = asyncio.Event()
    async with self.assertTimeout(1.0):
      await some_event.wait()
    # some_event is not triggered after 1 second.
    ```

    Args:
      delay: The timeout delay in seconds.
      msg: The error message to be shown if the code inside the context manager
        doesn't timeout.
      with_log: Whether to log msg at the beginning of the context manager.

    Yields:
      The raw timeout context manager.
    """
    if with_log and msg is not None:
      self.logger.info(msg)
    try:
      async with asyncio.timeout(delay) as raw_context:
        yield raw_context
    except TimeoutError:
      return
    raise self.failureException(self._formatMessage(msg, "Timeout not reached"))

  @override
  @contextlib.contextmanager
  def subTest(self, msg: str | None = None, **kwargs: Any) -> Iterator[None]:
    """Provides a context manager to run subtests.

    This implementation wraps `mobly.expects.expect_no_raises` to provide
    `unittest.subTest`-like behavior in a Mobly environment.

    Args:
      msg: A description of the subtest.
      **kwargs: Additional keyword arguments for the subtest.
    """
    del self  # Unused.
    with expects.expect_no_raises(
        message=f"SubTest {msg} failed" if msg else None, extras=kwargs
    ):
      yield


class AndroidBumbleTestBase(BaseTestBase):
  """Base class for Bluetooth tests containing 1 Android and 0 or more Bumble device."""

  dut: AndroidSnippetDeviceWrapper
  dut_wrapper_factory: Callable[
      [android_device.AndroidDevice], AndroidSnippetDeviceWrapper
  ] = AndroidSnippetDeviceWrapper  # pyrefly: ignore[bad-assignment]
  _refs: Sequence[crown.CrownDevice] = ()
  NUM_REF_DEVICES: int
  test_case_log_handler: logging.FileHandler | None = None

  def _get_passthrough_hci_specs(self) -> list[str]:
    hci_specs = self.user_params.get("crown_driver_specs", [])
    if isinstance(hci_specs, str):
      hci_specs = hci_specs.split(",")
    return hci_specs

  @override
  async def async_setup_class(self) -> None:
    def make_config() -> bumble.device.DeviceConfiguration:
      return bumble.device.DeviceConfiguration(
          # Enable BR/EDR mode for Bumble devices.
          classic_enabled=True,
          # Enable interlaced scan for Bumble devices.
          classic_interlaced_scan_enabled=True,
          # Enable Address resolution fffloading for Bumble devices.
          address_resolution_offload=True,
          # Enable LE subrating for Bumble devices.
          le_subrate_enabled=True,
          # Enable EATT.
          eatt_enabled=True,
          # Set a random IRK.
          irk=secrets.token_bytes(16),
          # Set a default class of device of UNCATEGORIZED.
          class_of_device=0x1F00,
          # Set a random static address.
          address=bumble.hci.Address.generate_static_address(),
          # Set a default advertising interval.
          advertising_interval_min=_DEFAULT_ADVERTISING_INTERVAL,
          advertising_interval_max=_DEFAULT_ADVERTISING_INTERVAL,
      )

    match cast(str, self.user_params.get("crown_driver", CrownDriver.ANDROID)):
      case CrownDriver.ANDROID:
        controllers = self._get_android_controllers(self.NUM_REF_DEVICES + 1)
        self.dut = self.dut_wrapper_factory(controllers[0])
        self._refs = [
            await crown.CrownDevice.from_android_device(
                controller,
                make_config(),
            )
            for controller in controllers[1:]
        ]
      case CrownDriver.PASSTHROUGH:
        reset_delay = float(
            self.user_params.get(CROWN_RESET_DELAY, 0.0) or 0.0
        )
        controllers = self._get_android_controllers(1)
        self.dut = self.dut_wrapper_factory(controllers[0])
        self._refs = [
            await crown.CrownDevice.create(
                crown.CrownAdapter(hci_spec),
                make_config(),
                reset_delay=reset_delay,
            )
            for hci_spec in self._get_passthrough_hci_specs()
        ]
      case CrownDriver.USB:
        controllers = self._get_android_controllers(1)
        self.dut = self.dut_wrapper_factory(controllers[0])
        reset_delay = float(
            self.user_params.get(CROWN_RESET_DELAY, 0.0)
            or 0.0
        )
        usb_devices = cast(
            list[usb_device.UsbDevice],
            self.register_controller(
                usb_device, min_number=self.NUM_REF_DEVICES
            ),
        )
        self._refs = [
            await crown.CrownDevice.create(
                crown.CrownAdapter(device.hci_spec),
                make_config(),
                reset_delay=reset_delay,
            )
            for device in usb_devices
        ]
      case CrownDriver.CF_ROOTCANAL:
        controllers = self._get_android_controllers(1)
        self.dut = self.dut_wrapper_factory(controllers[0])
        wifi_mac_prefix = self.dut.get_int_prop("ro.boot.wifi_mac_prefix")
        port_offset = wifi_mac_prefix - 5554 if wifi_mac_prefix else 0
        vsock_port = 7300 + port_offset
        netsim_port = int(
            self.dut.adb.forward(["tcp:0", f"vsock:2:{vsock_port}"])
        )
        self.test_class_context.callback(
            lambda: self.dut.adb.forward(["--remove", f"tcp:{netsim_port}"])
        )
        self._refs = [
            await crown.CrownDevice.create(
                crown.CrownAdapter(f"tcp-client:localhost:{netsim_port}"),
                make_config(),
            )
            for _ in range(self.NUM_REF_DEVICES)
        ]
      case _:
        raise ValueError("Unsupported Crown driver")

    if property_overrides := self.user_params.get(PROPERTY_OVERRIDES, {}):
      if isinstance(property_overrides, str):
        property_overrides = json.loads(property_overrides)
      else:
        property_overrides = dict(property_overrides)
      for key, value in property_overrides.items():
        if self.dut.getprop(key) != value:
          self.setprop_for_class_context(key, value)

    # Register logcat forwarding service for DUT.
    logcat.LogcatForwardingService.register(
        self.dut.device,
        configs=logcat.LogcatForwardingService.Config(
            tag=self.TAG or self.__class__.__name__
        ),
    )

    # Record firmware and prebuilt version to sponge properties.
    self.record_data(
        RecordData(
            test_class=self.TAG,
            properties={
                "bt_fw_version": self.dut.firmware_version,
                "bt_prebuilt_version": self.dut.bluetooth_mainline_version,
            },
        )
    )
    # Record suite name and manufacturer/model data to suite-level properties.
    manufacturer = self.dut.getprop("ro.product.manufacturer")
    self.record_data(
        RecordData(
            properties={
                "suite_name": "NaviTest",
                "run_identifier": f"[{manufacturer}-{self.dut.device.model}]",
            }
        )
    )

  def write_test_output_data(
      self, filename: str, data: bytes | bytearray | memoryview
  ) -> None:
    """Writes the data to a file in the test output directory.

    Args:
      filename: The name of the file to save the data to.
      data: The data to save.
    """
    self.logger.info("Saving data to %s.", filename)
    file_path = pathlib.Path(self.current_test_info.output_path, filename)
    with open(file_path, "wb") as f:
      f.write(data)

  def _get_btsnoop_and_dumpsys(self) -> None:
    try:
      adb_snippets.download_btsnoop(
          device=self.dut.device,
          destination_base_path=self.current_test_info.output_path,
      )
      adb_snippets.cleanup_btsnoop(device=self.dut.device)
      adb_snippets.download_dumpsys(
          device=self.dut.device,
          destination_base_path=self.current_test_info.output_path,
      )
      for ref in self._refs:
        address_str = ref.address.replace(":", "-")
        with open(
            pathlib.Path(
                self.current_test_info.output_path,
                f"bumble_{address_str}_btsnoop.log",
            ),
            "wb",
        ) as f:
          f.write(ref.snoop_buffer.getbuffer())
        if isinstance(ref.adapter, crown.AndroidCrownAdapter):
          adb_snippets.download_btsnoop(
              device=ref.adapter.ad,
              destination_base_path=self.current_test_info.output_path,
              filename_prefix="bumble",
          )
          adb_snippets.cleanup_btsnoop(device=ref.adapter.ad)
    except (adb.Error, FileNotFoundError):
      if sys.platform == "win32":
        self.logger.exception(
            "Failed to get btsnoop and dumpsys, probably because the file path"
            " is too long (Windows limit is 260 characters)"
        )
      else:
        self.logger.exception("Failed to get btsnoop and dumpsys")

  def _get_test_script(self) -> None:
    """Writes the test script to a file in the test output directory."""
    try:
      target = self.current_test_method
      while isinstance(target, functools.partial):
        if getattr(target.func, "__name__", "") == "_async_test_wrapper":
          target = target.args[0]
        else:
          target = target.func
      with open(
          pathlib.Path(
              self.current_test_info.output_path,
              f"{target.__name__}.py",
          ),
          "w",
      ) as f:
        f.write(textwrap.dedent(inspect.getsource(target)))
    except (OSError, FileNotFoundError):
      self.logger.exception("Failed to get test script")

  @retry_lib.retry_on_exception()
  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()

    # Check if snippet is broken.
    try:
      self.dut.bt.ping()
    except (BrokenPipeError, mobly.snippet.errors.Error) as e:
      if self.dut.device.is_adb_detectable():
        self.logger.exception("Snippet is broken, reloading")
        self.dut.reload_snippet()
      else:
        raise signals.TestAbortAll(
            "DUT is disconnected, cannot continue the test."
        ) from e

    # Bumble logger.
    self.test_case_log_handler = logging.FileHandler(
        pathlib.Path(self.current_test_info.output_path, "test_log.DEBUG")
    )
    self.test_case_log_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs).03d %(levelname)s %(message)s",
            "%m-%d %H:%M:%S",
        )
    )
    self.test_case_log_handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(self.test_case_log_handler)

    # Check required flags.
    for flag_name in getattr(self.current_test_method, _NAVI_REQUIRE_FLAG, []):
      flag = self.dut.get_flag(flag_name)
      if not flag:
        self.skipTest(f"Flag {flag_name} is not present.")
      if not flag.enabled:
        self.skipTest(f"Flag {flag_name} is not enabled.")

    # Make sure Bluetooth is enabled before factory reset.
    self.assertTrue(self.dut.bt.enable())
    self.dut.bt.waitForAdapterState(android_constants.AdapterState.ON)

    # Clean GATT cache - or it may skip GATT service discovery and break some
    # LE profile tests.
    with contextlib.suppress(adb.AdbError):
      self.dut.adb.shell("rm /data/misc/bluetooth/gatt_*")

    # Remove all bonded devices first to avoid reconnection.
    for bonded_device in self.dut.bt.getBondedDevices():
      self.dut.bt.removeBond(bonded_device)

    async with self.assert_not_timeout(_SETUP_TIMEOUT_SECONDS):
      await asyncio.gather(
          asyncio.to_thread(self.dut.bt.factoryReset),
          *[ref.reset() for ref in self._refs],
      )

    # Make sure Bluetooth is enabled after factory reset.
    self.assertTrue(self.dut.bt.enable())
    self.dut.bt.waitForAdapterState(android_constants.AdapterState.ON)

  @override
  async def async_teardown_test(self) -> None:
    # Collect logcat.
    await asyncio.to_thread(
        lambda: self.dut.device.services.create_output_excerpts_all(
            self.current_test_info
        )
    )
    # Collect btsnoop and dumpsys logs.
    await asyncio.to_thread(self._get_btsnoop_and_dumpsys)
    # Close test case log handler.
    if self.test_case_log_handler is not None:
      self.test_case_log_handler.close()
      logging.getLogger().removeHandler(self.test_case_log_handler)
      self.test_case_log_handler = None

    await super().async_teardown_test()

  @override
  def on_fail(self, record: records.TestResultRecord) -> None:
    self._get_test_script()

  @override
  def on_pass(self, record: records.TestResultRecord) -> None:
    if self.user_params.get(RECORD_FULL_DATA):
      self._get_test_script()

  @override
  async def async_teardown_class(self) -> None:
    has_error_or_fail = self.results.failed or self.results.error
    if has_error_or_fail or self.user_params.get(RECORD_FULL_DATA):
      self.dut.device.take_bug_report()
    await super().async_teardown_class()
    for ref in self._refs:
      await ref.close()
      ref.adapter.stop()
      if self.user_params.get(DUMP_CROWN_LOG_ON_FAIL) and has_error_or_fail:
        ref.adapter.dump_debug_logs(self.log_path)

  @retry_lib.retry_on_exception(initial_delay_sec=1, num_retries=3)
  async def classic_connect_and_pair(
      self,
      ref: crown.CrownDevice | None = None,
      direction: constants.Direction = constants.Direction.OUTGOING,
      connect_profiles: bool = False,
  ) -> bumble.device.Connection:
    """Connects and creates bond from DUT over BR/EDR.

    This is the most common pairing scenario between Android and headsets,
    speakers, or carkits, where REF is in passive pairing mode, and user
    triggers pairing procedure from Settings.
    If bond state is BONDED, skip pairing. If bond state is BONDING, the process
    will be cancelled and restarted.

    Args:
      ref: The Bumble device to pair with. If None, first Bumble device will be
        used.
      direction: The direction of the pairing.
      connect_profiles: Whether to connect profiles after pairing. This may
        fails if REF has no known service UUIDs.

    Returns:
      REF->DUT ACL connection instance.
    """
    if ref is None:
      ref = self._refs[0]

    auth_task: asyncio.Task[None] | None = None

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      match self.dut.bt.getBondState(ref.address):
        case android_constants.BondState.BONDED:
          self.logger.info("[DUT] Bond already exists.")
          connection = ref.device.find_connection_by_bd_addr(
              bumble.hci.Address(self.dut.address),
              transport=bumble.core.PhysicalTransport.BR_EDR,
          )
          if not connection:
            self.fail("Failed to find ACL connection between DUT and REF.")
          return connection

        case android_constants.BondState.BONDING:
          self.logger.info("[DUT] Already in bonding state, cancelling.")
          self.assertTrue(self.dut.bt.cancelBond(ref.address))
          await dut_cb.wait_for_event(
              bl4a_api.BondStateChanged(
                  address=ref.address, state=android_constants.BondState.NONE
              ),
              timeout=_SETUP_TIMEOUT_SECONDS,
          )

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      if direction == constants.Direction.OUTGOING:
        self.assertTrue(
            self.dut.bt.createBond(
                ref.address, android_constants.Transport.CLASSIC
            ),
            "Failed to create bond.",
        )
      else:
        self.logger.info("[REF] Connect to DUT.")
        ref_dut = await ref.device.connect(
            f"{self.dut.address}/P",
            transport=bumble.core.BT_BR_EDR_TRANSPORT,
            timeout=_SETUP_TIMEOUT_SECONDS,
        )
        self.logger.info("[REF] Create bond.")
        auth_task = asyncio.tasks.create_task(ref_dut.authenticate())

      self.logger.info("[DUT] Wait for pairing request.")
      await dut_cb.wait_for_event(
          bl4a_api.PairingRequest(
              address=ref.address, variant=matcher.ANY, pin=matcher.ANY
          ),
      )

      self.logger.info("[DUT] Handle pairing confirmation.")
      self.assertTrue(self.dut.bt.setPairingConfirmation(ref.address, True))

      if auth_task is not None:
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          await auth_task

      self.logger.info("[DUT] Wait for bond state change.")
      BondState: TypeAlias = android_constants.BondState
      pairing_complete_event = await dut_cb.wait_for_event(
          bl4a_api.BondStateChanged(
              address=ref.address,
              state=matcher.any_of(BondState.BONDED, BondState.NONE),
          ),
          timeout=_SETUP_TIMEOUT_SECONDS,
      )
      self.assertEqual(pairing_complete_event.state, BondState.BONDED)
      self.logger.info("[DUT] Pairing complete.")

      if not (
          ref_dut_acl := ref.device.find_connection_by_bd_addr(
              bumble.hci.Address(self.dut.address),
              transport=bumble.core.PhysicalTransport.BR_EDR,
          )
      ):
        self.fail("Failed to find ACL connection between DUT and REF.")

      if connect_profiles:
        self.logger.info("[DUT] Wait for UUID changed.")
        await dut_cb.wait_for_event(
            bl4a_api.UuidChanged(address=ref.address, uuids=matcher.ANY),
            timeout=_SETUP_TIMEOUT_SECONDS,
        )
        # Trigger profile connections.
        await asyncio.to_thread(self.dut.bt.connect, ref.address)
      return ref_dut_acl

  @retry_lib.retry_on_exception(initial_delay_sec=1, num_retries=3)
  async def connect_le_from_ref(
      self,
      dut_address_type: (
          android_constants.AddressTypeStatus | int
      ) = android_constants.AddressTypeStatus.RANDOM,
      ref_address_type: (
          bumble.hci.OwnAddressType
      ) = bumble.hci.OwnAddressType.RANDOM,
      ref: crown.CrownDevice | None = None,
      wait_for_dut_connected: bool = True,
      timeout: float = _SETUP_TIMEOUT_SECONDS,  # pylint: disable=redefined-outer-name
  ) -> bumble.device.Connection:
    """Connects to DUT from REF over LE.

    Starts legacy advertiser on DUT, connects from REF to DUT over LE, and
    retries on failure.

    Args:
      dut_address_type: OwnAddressType advertised by DUT.
      ref_address_type: OwnAddressType used by REF.
      ref: The Bumble device to connect from. If None, first Bumble device will
        be used.
      wait_for_dut_connected: Whether to wait for AclConnected on DUT.
      timeout: Timeout in seconds for setup steps.

    Returns:
      REF->DUT LE ACL connection instance.
    """
    if ref is None:
      ref = self._refs[0]

    match ref_address_type:
      case bumble.hci.OwnAddressType.PUBLIC:
        ref_addr = ref.address
      case bumble.hci.OwnAddressType.RANDOM:
        ref_addr = ref.random_address
      case _:
        ref_addr = ref.address

    service_uuid = str(uuid.uuid4())
    advertiser = await self.dut.bl4a.start_legacy_advertiser(
        bl4a_api.LegacyAdvertiseSettings(
            own_address_type=dut_address_type,
            advertise_mode=android_constants.LegacyAdvertiseMode.LOW_LATENCY,
            connectable=True,
        ),
        advertising_data=bl4a_api.AdvertisingData(service_uuids=[service_uuid]),
    )
    with advertiser:
      async with self.assert_not_timeout(timeout):
        if dut_address_type == android_constants.AddressTypeStatus.PUBLIC:
          dut_addr = bumble.hci.Address(
              self.dut.address, bumble.hci.AddressType.PUBLIC_DEVICE
          )
        else:
          advertisements = asyncio.Queue[bumble.device.Advertisement]()

          def on_advertisement(
              advertisement: bumble.device.Advertisement,
          ) -> None:
            if (
                service_uuids := advertisement.data.get(
                    bumble.core.AdvertisingData.Type.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS
                )
            ) and (service_uuid in service_uuids):
              advertisements.put_nowait(advertisement)

          ref.device.on(ref.device.EVENT_ADVERTISEMENT, on_advertisement)
          try:
            self.logger.info("[REF] Start scanning.")
            await ref.device.start_scanning()
            self.logger.info("[REF] Wait for finding DUT.")
            advertisement = await advertisements.get()
          finally:
            self.logger.info("[REF] Stop scanning.")
            await ref.device.stop_scanning()
            ref.device.remove_listener(
                ref.device.EVENT_ADVERTISEMENT, on_advertisement
            )

          dut_addr = advertisement.address
          if dut_addr.is_random:
            dut_addr.address_type = bumble.hci.AddressType.RANDOM_DEVICE
          else:
            dut_addr.address_type = bumble.hci.AddressType.PUBLIC_DEVICE

        self.logger.info("[REF] Connect to DUT.")
        with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
          ref_dut_acl = await ref.device.connect_le(
              dut_addr,
              own_address_type=ref_address_type,
          )
          await ref_dut_acl.get_remote_le_features()
          if wait_for_dut_connected:
            await dut_cb.wait_for_event(
                bl4a_api.AclConnected(
                    address=ref_addr,
                    transport=android_constants.Transport.LE,
                ),
            )
        return ref_dut_acl

  @retry_lib.retry_on_exception(initial_delay_sec=1, num_retries=3)
  async def le_connect_and_pair(
      self,
      ref_address_type: bumble.hci.OwnAddressType,
      ref: crown.CrownDevice | None = None,
      direction: constants.Direction = constants.Direction.OUTGOING,
      connect_profiles: bool = False,
      delegate: pairing.PairingDelegate | None = None,
  ) -> None:
    """Connects and creates bond from DUT over LE.

    ACL connection might be kept after pairing.
    If bond state is BONDED, skip pairing. If bond state is BONDING, the process
    will be cancelled and restarted.

    Args:
      ref_address_type: OwnAddressType advertised by ref.
      ref: The Bumble device to pair with. If None, first Bumble device will be
        used.
      direction: The direction of the pairing.
      connect_profiles: Whether to connect profiles after pairing. This may
        fails if REF has no known service UUIDs.
      delegate: The pairing delegate to use for pairing.

    Returns:
      None.
    """
    if ref is None:
      ref = self._refs[0]

    if delegate is None:
      delegate = pairing.PairingDelegate(
          io_capability=pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
      )

    match ref_address_type:
      case bumble.hci.OwnAddressType.PUBLIC:
        ref_addr = ref.address
        dut_scan_type = android_constants.AddressTypeStatus.PUBLIC
        identity_address_type = pairing.PairingConfig.AddressType.PUBLIC
      case bumble.hci.OwnAddressType.RANDOM:
        ref_addr = ref.random_address
        dut_scan_type = android_constants.AddressTypeStatus.RANDOM
        identity_address_type = pairing.PairingConfig.AddressType.RANDOM
      case _:
        raise ValueError(f"Unsupported address type: {ref_address_type}")

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      match self.dut.bt.getBondState(ref_addr):
        case android_constants.BondState.BONDED:
          self.logger.info("[DUT] Bond already exists.")
          return
        case android_constants.BondState.BONDING:
          self.logger.info("[DUT] Already in bonding state, cancelling.")
          self.assertTrue(self.dut.bt.cancelBond(ref_addr))
          await dut_cb.wait_for_event(
              bl4a_api.BondStateChanged(
                  address=ref_addr, state=android_constants.BondState.NONE
              ),
              timeout=_SETUP_TIMEOUT_SECONDS,
          )

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      # Override pairing config factory to set identity address type and
      # io capability.
      ref.device.pairing_config_factory = lambda _: pairing.PairingConfig(
          identity_address_type=identity_address_type,
          delegate=delegate,
      )

      pair_task: asyncio.Task[None] | None = None
      if direction == constants.Direction.OUTGOING:
        async with self.assert_not_timeout(_SETUP_TIMEOUT_SECONDS):
          await ref.device.start_advertising(
              own_address_type=ref_address_type, auto_restart=False
          )

        self.assertTrue(
            self.dut.bt.createBond(
                ref_addr, android_constants.Transport.LE, dut_scan_type
            )
        )
      else:
        ref_dut_acl = await self.connect_le_from_ref(
            dut_address_type=android_constants.AddressTypeStatus.RANDOM,
            ref_address_type=ref_address_type,
            ref=ref,
            wait_for_dut_connected=False,
            timeout=_SETUP_TIMEOUT_SECONDS,
        )
        pair_task = asyncio.create_task(ref_dut_acl.pair())

      self.logger.info("[DUT] Wait for pairing request.")
      await dut_cb.wait_for_event(
          bl4a_api.PairingRequest(
              address=ref_addr, variant=matcher.ANY, pin=matcher.ANY
          ),
          timeout=_SETUP_TIMEOUT_SECONDS,
      )

      self.assertTrue(self.dut.bt.setPairingConfirmation(ref_addr, True))
      self.logger.info("[DUT] Wait for bond state change.")
      BondState: TypeAlias = android_constants.BondState
      pairing_complete_event = await dut_cb.wait_for_event(
          bl4a_api.BondStateChanged(
              address=ref_addr,
              state=matcher.any_of(BondState.BONDED, BondState.NONE),
          ),
          timeout=_SETUP_TIMEOUT_SECONDS,
      )
      self.assertEqual(pairing_complete_event.state, BondState.BONDED)
      self.logger.info("[DUT] Pairing complete.")
      async with self.assert_not_timeout(_SETUP_TIMEOUT_SECONDS):
        await ref.device.stop_advertising()

      if pair_task:
        async with self.assert_not_timeout(_SETUP_TIMEOUT_SECONDS):
          self.logger.info("[REF] Wait for pairing complete.")
          await pair_task

      if connect_profiles:
        self.logger.info("[DUT] Wait for UUID changed.")
        await dut_cb.wait_for_event(
            bl4a_api.UuidChanged(address=ref_addr, uuids=matcher.ANY),
            timeout=_SETUP_TIMEOUT_SECONDS,
        )
        # Trigger profile connections.
        await asyncio.to_thread(self.dut.bt.connect, ref_addr)

  async def disconnect_with_check(
      self,
      ref_addr: str,
      transport: android_constants.Transport,
      ref: crown.CrownDevice | None = None,
  ) -> None:
    """Disconnect from DUT and checks if the connection is closed on both sides.

    Args:
      ref_addr: The address of the device to disconnect from.
      transport: The transport type of the connection.
      ref: The Bumble device to disconnect from. If None, first Bumble device
        will be used.
    """
    if ref is None:
      ref = self._refs[0]

    match transport:
      case android_constants.Transport.CLASSIC:
        bumble_transport = bumble.core.PhysicalTransport.BR_EDR
      case android_constants.Transport.LE:
        bumble_transport = bumble.core.PhysicalTransport.LE
      case _:
        raise ValueError(f"Unsupported transport: {transport}")

    ref_dut_acl = ref.device.find_connection_by_bd_addr(
        bumble.hci.Address(self.dut.address),
        transport=bumble_transport,
    )
    if not ref_dut_acl:
      self.logger.info("[REF] Failed to find ACL connection to DUT.")
      return

    disconnection_event: asyncio.Event = asyncio.Event()

    def on_disconnection(reason: int) -> None:
      del reason  # Unused.
      disconnection_event.set()

    ref_dut_acl.on(
        bumble.device.Connection.EVENT_DISCONNECTION, on_disconnection
    )
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as adapter_cb:
      self.logger.info("[DUT] Disconnect from REF.")
      self.dut.bt.disconnect(ref_addr)

      self.logger.info("[DUT] Wait for ACL disconnection.")
      await adapter_cb.wait_for_event(
          event=bl4a_api.AclDisconnected(
              address=ref_addr,
              transport=transport,
          ),
      )

      self.logger.info("[REF] Wait for ACL disconnection.")
      await disconnection_event.wait()

  def setprop_for_class_context(self, prop: str, tmp_value: str) -> None:
    """Sets a property and registers a callback to revert it.

    If the property is already set to the tmp_value, do nothing.

    Args:
      prop: The property to set.
      tmp_value: The value to set the property to.
    """
    current_value = self.dut.getprop(prop)
    if current_value == tmp_value:
      return

    self.logger.info("[DUT] Setting %s to %s", prop, tmp_value)
    self.dut.setprop(prop, tmp_value)

    self.test_class_context.callback(
        lambda: self.dut.setprop(prop, current_value)
    )

  def setflag_for_class_context(self, flag_name: str, tmp_value: bool) -> bool:
    """Sets a flag and registers a callback to revert it.

    If the flag is already set to the tmp_value, do nothing.

    Args:
      flag_name: The name of the flag to set.
      tmp_value: The value to set the flag to.

    Returns:
      True if the flag is set successfully, False otherwise.
    """
    flag = self.dut.get_flag(flag_name)
    if not flag:
      self.logger.error("Flag %s is not present.", flag_name)
      return False
    if flag.enabled == tmp_value:
      self.logger.info(
          "Flag %s is already %s.",
          flag_name,
          "enabled" if tmp_value else "disabled",
      )
      return True
    if not flag.writable:
      self.logger.error("Flag %s is not writable.", flag_name)
      return False

    self.logger.info("[DUT] Setting %s to %s", flag_name, tmp_value)

    self.dut.set_flag(flag_name, value=tmp_value)
    self.test_class_context.callback(
        lambda: self.dut.set_flag(flag_name, value=flag.enabled)
    )
    return True


class OneDeviceTestBase(AndroidBumbleTestBase):
  """Base class for Bluetooth tests containing 1 device."""

  dut: AndroidSnippetDeviceWrapper
  NUM_REF_DEVICES = 0


class TwoDevicesTestBase(AndroidBumbleTestBase):
  """Base class for Bluetooth tests containing 2 devices."""

  dut: AndroidSnippetDeviceWrapper
  ref: crown.CrownDevice
  NUM_REF_DEVICES = 1

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    self.ref = self._refs[0]


class MultiDevicesTestBase(AndroidBumbleTestBase):
  """Base class for Bluetooth tests multiple devices."""

  dut: AndroidSnippetDeviceWrapper
  refs: list[crown.CrownDevice]
  # Tests may override this value to use more than 2 Bumble devices.
  NUM_REF_DEVICES = 2

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    self.refs = list(self._refs)
