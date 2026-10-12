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

"""Tests for Bluetooth Quality Report."""

from collections.abc import Mapping
import enum
from unittest import mock

from bumble import core
from bumble import hci
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api


_DEFAULT_STEP_TIMEOUT_SECONDS = 5.0
_MASKED_ADDRESS = "FF:FF:FF:FF:FF:FF"

# BQR offload properties. When offload is enabled, the controller hands BQR
# events to the sensor hub, which buffers them and flushes them on the offload
# schedule instead of reporting them to the host as they occur. The offload
# configuration in use puts APPROACH_LSTO (event id 2) in a slot with a 300
# second timeout, so the event these tests assert on arrives minutes late: on
# ZP1A.260914.002 the controller captured it 164ms after the trigger but the
# host only saw it 76 seconds later, long past the step timeout.
#
# The properties are read by the Pixel HAL in `BqrOffload::OnBluetoothChipReady`
# (vendor/google/connectivity/bluetooth/bluetooth_hal/pixel/phone/client/
# bqr_offload.cc), which programs the sensor hub over the EFW transport. The
# sensor hub is not reset when the Bluetooth adapter is bounced, so clearing the
# properties only takes effect after a reboot.
_BQR_OFFLOAD_PROPERTIES = (
    "persist.bluetooth.bqr.offload.associate_eventid",
    "persist.bluetooth.bqr.offload.associate_eventmask",
    "persist.bluetooth.bqr.offload.eventid",
    "persist.bluetooth.bqr.offload.mode",
    "persist.bluetooth.bqr.offload.timeout_s",
    "persist.bluetooth.bqr.offload.weight",
)


class BqrEventMaskBitIndex(enum.IntFlag):
  """BQR event mask bit index."""
  QUALITY_MONITORING_MODE = 1 << 0
  APPROACH_LSTO = 1 << 1
  A2DP_AUDIO_CHOPPY_EVENT = 1 << 2
  SCO_VOICE_CHOPPY_EVENT = 1 << 3
  ROOT_INFLAMMATION_EVENT = 1 << 4
  ENERGY_MONITORING_MODE = 1 << 5
  LE_AUDIO_CHOPPY_EVENT = 1 << 6
  CONNECT_FAIL_EVENT = 1 << 7
  RF_STATS_MODE_EVENT_TRIGGER = 1 << 8
  RF_STATS_PERIODIC_REPORT = 1 << 9
  VENDOR_SPECIFIC_QUALITY_EVENTS = 1 << 15
  LMP_LL_MESSAGE_TRACE = 1 << 16
  BLUETOOTH_MULTI_LINK_COEX_SCHEDULING_TRACE = 1 << 17
  THE_CONTROLLER_DEBUG_INFORMATION_MECHANISM = 1 << 18


class BluetoothQualityReportTest(navi_test_base.TwoDevicesTestBase):
  """Tests Bluetooth Quality Report."""
  bqr_event_mask: int

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    self._disable_bqr_offload()

    bqr_event_mask_str = self.dut.getprop("persist.bluetooth.bqr.event_mask")
    if not bqr_event_mask_str:
      raise signals.TestAbortClass("BQR is not enabled on DUT.")
    self.bqr_event_mask = int(bqr_event_mask_str)

  def _disable_bqr_offload(self) -> None:
    """Clears the BQR offload properties for the duration of the class."""
    original_values = {
        prop: self.dut.getprop(prop) for prop in _BQR_OFFLOAD_PROPERTIES
    }
    if not any(original_values.values()):
      self.logger.info("[DUT] BQR offload is already disabled.")
      return

    # `persist.bluetooth.bqr.*` is not writable by the shell user.
    # `root_adb()` also waits for the device to come back, because restarting
    # adbd makes it temporarily disappear.
    self.dut.device.root_adb()
    self.test_class_context.callback(
        lambda: self._restore_bqr_offload(original_values)
    )

    for prop, original_value in original_values.items():
      if not original_value:
        continue
      self.logger.info("[DUT] Clearing %s (was %r)", prop, original_value)
      self.dut.setprop(prop, "")

    # A reboot is required rather than an adapter bounce: the buffered events
    # live on the sensor hub, which stays up across Bluetooth off/on, and the
    # HAL only skips `BqrOffloadInit` when the properties are empty, it never
    # tears down a configuration that is already programmed.
    self.logger.info("[DUT] Reboot to apply BQR offload settings.")
    self.dut.device.reboot()
    self.dut.reload_snippet()

    for prop in _BQR_OFFLOAD_PROPERTIES:
      self.assertFalse(
          self.dut.getprop(prop),
          f"Failed to clear {prop}, got {self.dut.getprop(prop)!r}.",
      )

  def _restore_bqr_offload(
      self, original_values: Mapping[str, str | None]
  ) -> None:
    """Restores the properties cleared by `_disable_bqr_offload`.

    The device is left with offload inactive until it is next rebooted, because
    the restored values are only picked up by the HAL at boot.

    Args:
      original_values: Property name to the value it held before it was cleared.
    """
    # Root is dropped by the reboot in `_disable_bqr_offload`.
    self.dut.device.root_adb()
    for prop, original_value in original_values.items():
      self.dut.setprop(prop, original_value)

  async def test_approach_lsto_classic_connection(self) -> None:
    """Tests classic connection approach LSTO."""

    bqr_cb = self.dut.bl4a.register_callback(bl4a_api.Module.BQR)
    adapter_cb = self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    self.test_case_context.push(bqr_cb)
    self.test_case_context.push(adapter_cb)

    self.logger.info("[REF] Connect to DUT.")
    ref_dut_acl = await self.ref.device.connect(
        self.dut.address,
        transport=core.PhysicalTransport.BR_EDR,
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    await adapter_cb.wait_for_event(
        bl4a_api.AclConnected(
            address=self.ref.address,
            transport=android_constants.Transport.CLASSIC,
        )
    )

    self.logger.info("[REF] Disconnect to trigger LSTO.")
    # Reason must be power off, or else LSTO will not be triggered.
    await ref_dut_acl.disconnect(
        reason=hci.HCI_REMOTE_DEVICE_TERMINATED_CONNECTION_DUE_TO_POWER_OFF_ERROR
    )

    if self.bqr_event_mask & BqrEventMaskBitIndex.APPROACH_LSTO:
      self.logger.info("[DUT] Wait for BQR event: APPROACH_LSTO.")
      await bqr_cb.wait_for_event(
          bl4a_api.BluetoothQualityReportReady(
              device=self.ref.address,
              quality_report_id=android_constants.BluetoothQualityReportId.APPROACH_LSTO,
              status=0,
              common=mock.ANY,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )
    if self.bqr_event_mask & BqrEventMaskBitIndex.RF_STATS_MODE_EVENT_TRIGGER:
      self.logger.info("[DUT] Wait for BQR event: RF_STATS.")
      await bqr_cb.wait_for_event(
          bl4a_api.BluetoothQualityReportReady(
              device=_MASKED_ADDRESS,
              quality_report_id=android_constants.BluetoothQualityReportId.RF_STATS,
              status=0,
              common=mock.ANY,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

  async def test_energy_monitoring_when_power_unplug(self) -> None:
    """Tests power unplug will trigger energy monitoring."""

    if self.dut.bt.getSdkVersion() < 36:
      self.skipTest(
          "Energy monitor event is not supported before SDK API level: 36."
      )
    if not self.bqr_event_mask & BqrEventMaskBitIndex.ENERGY_MONITORING_MODE:
      self.skipTest("Energy monitor event is not enabled on DUT.")

    bqr_cb = self.dut.bl4a.register_callback(bl4a_api.Module.BQR)
    self.test_case_context.push(bqr_cb)

    self.logger.info("[DUT] Set battery unplug and battery level to low.")
    self.dut.shell("cmd battery unplug")
    self.dut.shell("cmd battery set level 9")
    self.test_case_context.callback(lambda: self.dut.shell("cmd battery reset"))

    self.logger.info("[DUT] Wait for BQR event.")
    await bqr_cb.wait_for_event(
        bl4a_api.BluetoothQualityReportReady(
            device=_MASKED_ADDRESS,
            quality_report_id=android_constants.BluetoothQualityReportId.ENERGY_MONITOR,
            status=0,
            common=mock.ANY,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

if __name__ == "__main__":
  test_runner.main()
