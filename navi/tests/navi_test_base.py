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

"""Base classes for Bluetooth tests."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
import contextlib
import dataclasses
import enum
import functools
import inspect
import logging
import pathlib
import re
import secrets
from typing import Any, ClassVar, Never, TypeAlias, cast, final

from absl.testing import absltest
from bumble import pairing
import bumble.core
import bumble.device
import bumble.hci
import grpc.aio
from mobly import base_test
from mobly import records
from mobly import runtime_test_info
from mobly import signals
from mobly.controllers import android_device
from mobly.controllers.android_device_lib import adb
from mobly.controllers.android_device_lib import apk_utils
from snippet_uiautomator import uiautomator
from typing_extensions import override

from navi.bumble_ext import crown
from navi.utils import adb_snippets
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import errors
from navi.utils import logcat
from navi.utils import matcher
from navi.utils import retry as retry_lib
from navi.utils import snippet_stub

_NAVI_PARAMETERIZED = "_NAVI_PARAMETERIZED"
_SETUP_TIMEOUT_SECONDS = 10.0
# 100 * 0.625ms = 62.5ms
_DEFAULT_ADVERTISING_INTERVAL = 100


class CrownDriver(enum.StrEnum):
  ANDROID = "android"
  PASSTHROUGH = "passthrough"


@dataclasses.dataclass
class RecordData:
  """Wrapper of data to be uploaded to Sponge."""

  properties: dict[str, Any]
  test_name: str | None = None
  test_class: str | None = None


class AndroidSnippetDeviceWrapper:
  """Wrapper for Android device under test."""

  device: android_device.AndroidDevice
  _SNIPPET_NAME: ClassVar[str] = "bt"
  _UI_AUTOMATOR_NAME: ClassVar[str] = "ui"

  def __init__(self, device: android_device.AndroidDevice) -> None:
    self.device = device
    # Sync time.
    adb_snippets.sync_time(self.device)
    # Disable Selinux.
    self.adb.shell("setenforce 0")
    # Enable BT Snoop.
    adb_snippets.enable_btsnoop(self.device)
    # Enable BT verbose logging.
    self.adb.shell("setprop persist.log.tag.bluetooth VERBOSE")
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
    self.bluetooth_flags = adb_snippets.get_bluetooth_flags(self.device)

    # Skip OOBE.
    with contextlib.suppress(adb.AdbError):
      if self.is_watch:
        self.shell(
            "am broadcast -a com.google.android.clockwork.action.TEST_MODE"
        )
      else:
        self.shell("am start -a com.android.setupwizard.FOUR_CORNER_EXIT")

  @property
  def adb(self) -> adb.AdbProxy:
    return self.device.adb

  @property
  def bt(self) -> snippet_stub.BluetoothSnippet:
    return self.device.bt

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

  def setprop(self, prop_name: str, prop_value: str) -> None:
    """Sets a property of the device."""
    self.adb.shell(["setprop", prop_name, prop_value])

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
  def bluetooth_prebuilt_version(self) -> str | None:
    """Version of the Bluetooth prebuilt."""
    with contextlib.suppress(adb.AdbError):
      response = self.shell(
          "pm list packages --apex-only --show-versioncode | egrep -i bt"
      )
      if m := re.search(r"versionCode:(\d+)", response):
        return m.group(1)
    return None


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


class BaseTestBase(base_test.BaseTestClass, absltest.TestCase):
  """Base class for all test base classes. Should not be used directly."""

  # This attribute is defined by base_test.BaseTestClass and allowed to be None
  # in a very short period after test complete, but it's very inconvenient to
  # handle None case and unlikely to happen in our case. So we just declare it
  # as non-optional here.
  current_test_info: runtime_test_info.RuntimeTestInfo
  user_params: dict[str, Any]

  def __init__(self, *args, **kwargs) -> None:
    super().__init__(*args, **kwargs)
    self.logger = logging.getLogger(__name__)
    self.loop = asyncio.new_event_loop()
    self.test_case_context = contextlib.AsyncExitStack()
    self.test_class_context = contextlib.AsyncExitStack()

  def _async_test_wrapper(
      self,
      coro_factory: Callable[[], Coroutine[Any, Any, Any]],
  ) -> Any:
    try:
      self.loop.run_until_complete(coro_factory())
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
    if asyncio.coroutines.iscoroutinefunction(test_method):
      synced_func = functools.partial(self._async_test_wrapper, partial_method)
    else:
      synced_func = partial_method

    # Copy decorative attributes to the wrapped function.
    for attr_name in (
        base_test.ATTR_MAX_RETRY_CNT,
        base_test.ATTR_MAX_CONSEC_ERROR,
        base_test.ATTR_REPEAT_CNT,
    ):
      if attr_value := getattr(test_method, attr_name, None):
        setattr(synced_func, attr_name, attr_value)
    return synced_func

  @override
  def _get_test_methods(
      self, test_names: list[str]
  ) -> list[tuple[str, Callable[..., Any]]]:
    methods = list[tuple[str, Callable[..., Any]]]()
    for test_name in test_names:
      if not test_name.startswith("test_") and not test_name.startswith("r:"):
        raise base_test.Error(
            f"Test method name {test_name} does not follow naming convention"
            " test_* or regex matcher r:(regex pattern), abort."
        )

      if test_name.startswith("r:"):
        test_name_pattern = re.compile(test_name[2:])
        methods.extend(
            (test_name, test_method)
            for test_name, test_method in self._generated_test_table.items()
            if test_name_pattern.fullmatch(test_name)
        )
      elif test_method := self._generated_test_table.get(test_name):
        methods.append((test_name, test_method))

    return methods

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
        param_sets = cast(dict[str, Sequence[Any] | dict[str, Any]], param_sets)
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
      max_retry_count = int(max_retry_count)
      for test_method in self._generated_test_table.values():
        old_max_retry_count = getattr(
            test_method, base_test.ATTR_MAX_RETRY_CNT, 0
        )
        setattr(
            test_method,
            base_test.ATTR_MAX_RETRY_CNT,
            max(old_max_retry_count, max_retry_count),
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

  @final
  @override
  def teardown_test(self) -> None:
    self.loop.run_until_complete(self.async_teardown_test())

  @final
  @override
  def setup_class(self) -> None:
    self.loop.run_until_complete(self.async_setup_class())

  @final
  @override
  def teardown_class(self) -> None:
    self.loop.run_until_complete(self.async_teardown_class())

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


class AndroidBumbleTestBase(BaseTestBase):
  """Base class for Bluetooth tests containing 1 Android and 0 or more Bumble device."""

  dut: AndroidSnippetDeviceWrapper
  dut_wrapper_factory: Callable[
      [android_device.AndroidDevice], AndroidSnippetDeviceWrapper
  ] = AndroidSnippetDeviceWrapper
  _refs: Sequence[crown.CrownDevice] = ()
  NUM_REF_DEVICES: int

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
          # Set a random IRK.
          irk=secrets.token_bytes(16),
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
        controllers = self._get_android_controllers(1)
        self.dut = self.dut_wrapper_factory(controllers[0])
        self._refs = [
            await crown.CrownDevice.create(
                crown.CrownAdapter(hci_spec),
                make_config(),
            )
            for hci_spec in self._get_passthrough_hci_specs()
        ]
      case _:
        raise ValueError("Unsupported Crown driver")

    # Register logcat forwarding service for DUT.
    logcat.LogcatForwardingService.register(
        self.dut.device,
        configs=logcat.LogcatForwardingService.Config(
            tag=self.TAG or self.__class__.__name__
        ),
    )

    # Record suite name and manufacturer/model data to suite-level properties.
    manufacturer = self.dut.getprop('ro.product.manufacturer')
    self.record_data(
        RecordData(
            properties={
                "suite_name": "NaviTest",
                "run_identifier": f"[{manufacturer}-{self.dut.device.model}]"
            }
        )
    )

    # Record firmware and prebuilt version to sponge properties.
    self.record_data(
        RecordData(
            test_class=self.TAG,
            properties={
                "bt_fw_version": self.dut.firmware_version,
                "bt_prebuilt_version": self.dut.bluetooth_prebuilt_version,
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

  @override
  def on_fail(self, record: records.TestResultRecord) -> None:
    adb_snippets.download_btsnoop(
        device=self.dut.device,
        destination_base_path=self.current_test_info.output_path,
    )
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

  @retry_lib.retry_on_exception(initial_delay_sec=1, num_retries=3)
  @override
  async def async_setup_test(self) -> None:
    # Make sure Bluetooth is enabled before factory reset.
    self.assertTrue(self.dut.bt.enable())

    # Clean GATT cache - or it may skip GATT service discovery and break some
    # LE profile tests.
    with contextlib.suppress(adb.AdbError):
      self.dut.adb.shell("rm /data/misc/bluetooth/gatt_*")

    # Reset DUT first, because if REF is reset first, DUT may try to reconnect
    # or perform other stack behavior which may break the test flow.
    self.dut.bt.factoryReset()
    async with self.assert_not_timeout(_SETUP_TIMEOUT_SECONDS):
      await asyncio.gather(*[ref.reset() for ref in self._refs])

    # Make sure Bluetooth is enabled after factory reset.
    self.assertTrue(self.dut.bt.enable())

  @override
  async def async_teardown_class(self) -> None:
    await super().async_teardown_class()
    for ref in self._refs:
      ref.adapter.stop()
    if self.results.failed or self.results.error:
      self.dut.device.take_bug_report()

  @retry_lib.retry_on_exception(initial_delay_sec=1, num_retries=3)
  async def classic_connect_and_pair(
      self, ref: crown.CrownDevice | None = None
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

    Returns:
      REF->DUT ACL connection instance.
    """
    if ref is None:
      ref = self._refs[0]

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
      self.assertTrue(
          self.dut.bt.createBond(
              ref.address, android_constants.Transport.CLASSIC
          ),
          "Failed to create bond.",
      )
      self.logger.info("[DUT] Wait for pairing request.")
      await dut_cb.wait_for_event(
          bl4a_api.PairingRequest(
              address=ref.address, variant=matcher.ANY, pin=matcher.ANY
          ),
          timeout=_SETUP_TIMEOUT_SECONDS,
      )
      self.assertTrue(self.dut.bt.setPairingConfirmation(ref.address, True))
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
      return ref_dut_acl

  @retry_lib.retry_on_exception(initial_delay_sec=1, num_retries=3)
  async def le_connect_and_pair(
      self,
      ref_address_type: bumble.hci.OwnAddressType,
      ref: crown.CrownDevice | None = None,
  ) -> None:
    """Connects and creates bond from DUT over LE.

    ACL connection might be kept after pairing.
    If bond state is BONDED, skip pairing. If bond state is BONDING, the process
    will be cancelled and restarted.

    Args:
      ref_address_type: OwnAddressType advertised by ref.
      ref: The Bumble device to pair with. If None, first Bumble device will be
        used.

    Returns:
      None.
    """
    if ref is None:
      ref = self._refs[0]

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
          delegate=pairing.PairingDelegate(
              io_capability=pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT
          ),
      )

      async with self.assert_not_timeout(_SETUP_TIMEOUT_SECONDS):
        await ref.device.start_advertising(
            own_address_type=ref_address_type, auto_restart=False
        )

      self.assertTrue(
          self.dut.bt.createBond(
              ref_addr, android_constants.Transport.LE, dut_scan_type
          )
      )
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
