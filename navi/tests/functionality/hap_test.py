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

from __future__ import annotations

import asyncio
import copy
import secrets
from typing import TypeVar
from unittest import mock

from bumble import device
from bumble import gatt
from bumble import hci
from bumble.profiles import cap
from bumble.profiles import csip
from bumble.profiles import hap
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api

_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_FOO_PRESET = hap.PresetRecord(1, "foo preset")
_BAR_PRESET = hap.PresetRecord(50, "bar preset")
_UNAVAILABLE_PRESET = hap.PresetRecord(
    7,
    "bad preset",
    hap.PresetRecord.Property(
        hap.PresetRecord.Property.Writable.CANNOT_BE_WRITTEN,
        hap.PresetRecord.Property.IsAvailable.IS_UNAVAILABLE,
    ),
)

_Module = bl4a_api.Module
_Property = android_constants.Property
_HaType = hap.HearingAidType
_Service = TypeVar("_Service", bound=gatt.Service)


def _get_service_from_device(
    bumble_device: device.Device, service_type: type[_Service]
) -> _Service:
  return next(
      service
      for service in bumble_device.gatt_server.services
      if isinstance(service, service_type)
  )


class _HearingAccessService(hap.HearingAccessService):

  def __init__(
      self,
      device: device.Device,  # pylint: disable=redefined-outer-name
      features: hap.HearingAidFeatures,
      presets: list[hap.PresetRecord],
  ) -> None:
    super().__init__(device, features, presets)
    self.condition = asyncio.Condition()

  @override
  async def notify_active_preset(self) -> None:
    await super().notify_active_preset()
    async with self.condition:
      self.condition.notify_all()

  async def wait_for_active_preset_index(self, index: int) -> None:
    async with self.condition:
      await self.condition.wait_for(lambda: self.active_preset_index == index)


class HapTest(navi_test_base.MultiDevicesTestBase):
  """Tests LE Hearing Aid profile."""

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if self.dut.device.is_emulator:
      # Force enable HAP client on the emulator.
      self.dut.shell(["setprop", _Property.HAP_CLIENT_ENABLED, "true"])
      self.dut.shell(
          ["setprop", _Property.CSIP_SET_COORDINATOR_ENABLED, "true"]
      )

    if self.dut.getprop(_Property.HAP_CLIENT_ENABLED) != "true":
      raise signals.TestAbortClass("HAP Client is not enabled on DUT.")

  def _setup_hap_servers(
      self,
      hearing_aid_type: _HaType = _HaType.MONAURAL_HEARING_AID,
      preset_synchronization_support: hap.PresetSynchronizationSupport = hap.PresetSynchronizationSupport.PRESET_SYNCHRONIZATION_IS_NOT_SUPPORTED,
  ) -> None:
    features = hap.HearingAidFeatures(
        hearing_aid_type=hearing_aid_type,
        preset_synchronization_support=preset_synchronization_support,
        independent_presets=hap.IndependentPresets.IDENTICAL_PRESET_RECORD,
        dynamic_presets=hap.DynamicPresets.PRESET_RECORDS_MAY_CHANGE,
        writable_presets_support=hap.WritablePresetsSupport.WRITABLE_PRESET_RECORDS_SUPPORTED,
    )
    if hearing_aid_type == _HaType.BINAURAL_HEARING_AID:
      sirk = secrets.token_bytes(csip.SET_IDENTITY_RESOLVING_KEY_LENGTH)
      for ref in self.refs:
        ref.device.add_services([
            _HearingAccessService(
                device=ref.device,
                features=features,
                presets=[
                    copy.copy(_FOO_PRESET),
                    copy.copy(_BAR_PRESET),
                    copy.copy(_UNAVAILABLE_PRESET),
                ],
            ),
            cap.CommonAudioServiceService(
                csip.CoordinatedSetIdentificationService(
                    set_identity_resolving_key=sirk,
                    set_identity_resolving_key_type=csip.SirkType.PLAINTEXT,
                    coordinated_set_size=2,
                )
            ),
        ])
    else:
      self.refs[0].device.add_service(
          _HearingAccessService(
              device=self.refs[0].device,
              features=features,
              presets=[
                  copy.copy(_FOO_PRESET),
                  copy.copy(_BAR_PRESET),
                  copy.copy(_UNAVAILABLE_PRESET),
              ],
          )
      )

  async def _setup_connections(self, hearing_aid_type: _HaType) -> None:
    with self.dut.bl4a.register_callback(_Module.HAP_CLIENT) as dut_hap_cb:
      for i, ref in enumerate(
          self.refs
          if hearing_aid_type == _HaType.BINAURAL_HEARING_AID
          else [self.refs[0]]
      ):
        self.logger.info("[DUT] Connect to REF-%d", i)
        await self.le_connect_and_pair(
            ref_address_type=hci.OwnAddressType.RANDOM,
            ref=ref,
            connect_profiles=True,
        )
        self.dut.bt.setHapConnectionPolicy(
            ref.random_address, android_constants.ConnectionPolicy.ALLOWED
        )
        self.logger.info("[DUT] Wait for HAP connected to REF-%d", i)
        await dut_hap_cb.wait_for_event(
            bl4a_api.ProfileConnectionStateChanged(
                address=ref.random_address,
                state=android_constants.ConnectionState.CONNECTED,
            ),
            timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
        )
        self.logger.info("[DUT] Set MTU to 517 for REF-%d", i)
        gatt_client = await self.dut.bl4a.connect_gatt_client(
            ref.random_address, transport=android_constants.Transport.LE
        )
        await gatt_client.request_mtu(517)

  @navi_test_base.named_parameterized(
      ("binaural", _HaType.BINAURAL_HEARING_AID),
      ("monaural", _HaType.MONAURAL_HEARING_AID),
  )
  async def test_remove_preset(self, hearing_aid_type: _HaType) -> None:
    """Test removing a preset from the REF.

    Test Steps:
      1. Setup HAP servers and connections.
      2. Remove a preset from the REF.
      3. Verify the DUT receives the update and the preset info is changed.

    Args:
      hearing_aid_type: Whether the test is for binaural or monaural.
    """
    self._setup_hap_servers(hearing_aid_type)
    await self._setup_connections(hearing_aid_type)

    has = _get_service_from_device(self.refs[0].device, _HearingAccessService)

    dut_hap_callback = self.dut.bl4a.register_callback(_Module.HAP_CLIENT)
    self.test_case_context.enter_context(dut_hap_callback)

    self.logger.info("[REF] Delete preset")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await has.delete_preset(index=_UNAVAILABLE_PRESET.index)

    self.logger.info("[DUT] Wait for preset info changed")
    await dut_hap_callback.wait_for_event(
        bl4a_api.PresetInfoChanged(
            address=self.refs[0].random_address,
            reason=mock.ANY,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.assertDictEqual(
        {rec.index: rec.name for rec in has.preset_records.values()},
        self.dut.bl4a.get_all_hap_preset_info(self.refs[0].random_address),
    )

  @navi_test_base.named_parameterized(
      ("binaural", _HaType.BINAURAL_HEARING_AID),
      ("monaural", _HaType.MONAURAL_HEARING_AID),
  )
  async def test_add_preset(self, hearing_aid_type: _HaType) -> None:
    """Test adding a preset from the REF.

    Test Steps:
      1. Setup HAP servers and connections.
      2. Add a preset from the REF.
      3. Verify the DUT receives the update and the preset info is changed.

    Args:
      hearing_aid_type: Whether the test is for binaural or monaural.
    """
    self._setup_hap_servers(hearing_aid_type)
    await self._setup_connections(hearing_aid_type)

    has = _get_service_from_device(self.refs[0].device, _HearingAccessService)

    dut_hap_callback = self.dut.bl4a.register_callback(_Module.HAP_CLIENT)
    self.test_case_context.enter_context(dut_hap_callback)

    self.logger.info("[REF] Add preset")
    added_preset = hap.PresetRecord(_BAR_PRESET.index + 3, "added_preset")
    has.preset_records[added_preset.index] = added_preset
    await has.generic_update(
        hap.PresetChangedOperation(
            hap.PresetChangedOperation.ChangeId.GENERIC_UPDATE,
            hap.PresetChangedOperation.Generic(_BAR_PRESET.index, added_preset),
        )
    )

    self.logger.info("[DUT] Wait for preset info changed")
    await dut_hap_callback.wait_for_event(
        bl4a_api.PresetInfoChanged(
            address=self.refs[0].random_address,
            reason=mock.ANY,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.assertDictEqual(
        {rec.index: rec.name for rec in has.preset_records.values()},
        self.dut.bl4a.get_all_hap_preset_info(self.refs[0].random_address),
    )

  @navi_test_base.named_parameterized(
      ("binaural", _HaType.BINAURAL_HEARING_AID),
      ("monaural", _HaType.MONAURAL_HEARING_AID),
  )
  async def test_update_preset(self, hearing_aid_type: _HaType) -> None:
    """Test updating a preset from the REF.

    Test Steps:
      1. Setup HAP servers and connections.
      2. Update a preset from the REF.
      3. Verify the DUT receives the update and the preset info is changed.

    Args:
      hearing_aid_type: Whether the test is for binaural or monaural.
    """

    self._setup_hap_servers(hearing_aid_type)
    await self._setup_connections(hearing_aid_type)

    has = _get_service_from_device(self.refs[0].device, _HearingAccessService)

    dut_hap_callback = self.dut.bl4a.register_callback(_Module.HAP_CLIENT)
    self.test_case_context.enter_context(dut_hap_callback)

    self.logger.info("[REF] Update preset")
    has.preset_records[_FOO_PRESET.index].name = "Very nice name"
    await has.generic_update(
        hap.PresetChangedOperation(
            hap.PresetChangedOperation.ChangeId.GENERIC_UPDATE,
            hap.PresetChangedOperation.Generic(
                _FOO_PRESET.index, has.preset_records[_FOO_PRESET.index]
            ),
        )
    )

    self.logger.info("[DUT] Wait for preset info changed")
    await dut_hap_callback.wait_for_event(
        bl4a_api.PresetInfoChanged(
            address=self.refs[0].random_address,
            reason=mock.ANY,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    self.assertDictEqual(
        {rec.index: rec.name for rec in has.preset_records.values()},
        self.dut.bl4a.get_all_hap_preset_info(self.refs[0].random_address),
    )

  @navi_test_base.named_parameterized(
      ("binaural", _HaType.BINAURAL_HEARING_AID),
      ("monaural", _HaType.MONAURAL_HEARING_AID),
  )
  async def test_set_active_preset(self, hearing_aid_type: _HaType) -> None:
    """Test setting a preset as active, the active preset should be changed.

    Test Steps:
      1. Setup HAP servers and connections.
      2. Set a preset as active.
      3. Verify the active preset is changed.

    Args:
      hearing_aid_type: Whether the test is for binaural or monaural.
    """
    self._setup_hap_servers(hearing_aid_type)
    await self._setup_connections(hearing_aid_type)

    for preset in (_BAR_PRESET, _FOO_PRESET):
      self.logger.info("[DUT] Set active preset to %d", preset.index)
      self.dut.bt.selectHapPreset(self.refs[0].random_address, preset.index)

      self.logger.info("[REF] Verify active preset")
      has = _get_service_from_device(self.refs[0].device, _HearingAccessService)
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await has.wait_for_active_preset_index(preset.index)

  async def test_set_active_preset_for_group(self) -> None:
    self._setup_hap_servers(hearing_aid_type=_HaType.BINAURAL_HEARING_AID)
    await self._setup_connections(hearing_aid_type=_HaType.BINAURAL_HEARING_AID)

    self.logger.info("[DUT] Set active preset")
    group_id = self.dut.bt.getHapGroup(self.refs[0].random_address)
    self.assertGreater(group_id, 0, "Group ID is not greater than 0")
    self.dut.bt.selectHapPresetForGroup(group_id, _BAR_PRESET.index)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      for i, ref in enumerate(self.refs):
        self.logger.info("[REF-%d] Verify active preset", i)
        has = _get_service_from_device(ref.device, _HearingAccessService)
        await has.wait_for_active_preset_index(_BAR_PRESET.index)

  async def test_set_non_existing_preset_as_active(self) -> None:
    """Test setting a non-existing preset as available, the preset should be ignored.

    Test Steps:
      1. Setup HAP servers and connections.
      2. Notify a non-existing preset to the DUT via the REF.
      3. Verify the DUT ignores the notification and the active preset is not
         changed.
    """
    self._setup_hap_servers(hearing_aid_type=_HaType.MONAURAL_HEARING_AID)
    await self._setup_connections(hearing_aid_type=_HaType.MONAURAL_HEARING_AID)

    has = _get_service_from_device(self.refs[0].device, _HearingAccessService)
    self.logger.info("[REF] Notify active preset to non existing index")
    has.active_preset_index = 79
    await has.notify_active_preset()

    # Operation should be ignored, so the active preset should not be changed.
    self.assertEqual(
        self.dut.bt.getActiveHapPresetIndex(self.refs[0].random_address),
        _FOO_PRESET.index,
    )

  async def test_set_non_existing_preset_as_available(self) -> None:
    """Test setting a non-existing preset as available should not crash.

    Test Steps:
      1. Setup HAP servers and connections.
      2. Notify a non-existing preset to the DUT via the REF.
      3. Verify the DUT ignores the notification and the active preset is not
         changed.
    """
    self._setup_hap_servers(hearing_aid_type=_HaType.MONAURAL_HEARING_AID)
    await self._setup_connections(hearing_aid_type=_HaType.MONAURAL_HEARING_AID)

    has = _get_service_from_device(self.refs[0].device, _HearingAccessService)
    self.logger.info("[REF] Notify available preset to non existing index")
    await has.generic_update(hap.PresetChangedOperationAvailable(79))

    # Not related, but make an RPC call to make sure BT is still alive.
    self.assertEqual(
        self.dut.bt.getActiveHapPresetIndex(self.refs[0].random_address),
        _FOO_PRESET.index,
    )

  async def test_synchronize_operation_failed_when_selecting_preset_can_recover(
      self,
  ) -> None:
    """Test synchronize operation failed when selecting preset can recover.

    Test Steps:
      1. Setup HAP servers and connections.
      2. Set a preset as active.
      3. Verify the active preset is changed.
      4. Set a preset as active again with sync enabled.
      5. Verify the active preset is changed.
    """
    self._setup_hap_servers(
        hearing_aid_type=_HaType.BINAURAL_HEARING_AID,
        preset_synchronization_support=hap.PresetSynchronizationSupport.PRESET_SYNCHRONIZATION_IS_SUPPORTED,
    )
    await self._setup_connections(hearing_aid_type=_HaType.BINAURAL_HEARING_AID)

    ha_services = [
        _get_service_from_device(ref.device, _HearingAccessService)
        for ref in self.refs
    ]
    self.assertEqual(ha_services[0].active_preset_index, _FOO_PRESET.index)
    self.assertEqual(ha_services[1].active_preset_index, _FOO_PRESET.index)

    self.logger.info("[DUT] Set active preset for group to BAR preset.")
    group_id = self.dut.bt.getHapGroup(self.refs[0].random_address)
    self.assertGreater(group_id, 0, "Group ID is not greater than 0")
    self.dut.bt.selectHapPresetForGroup(group_id, _BAR_PRESET.index)

    self.logger.info("Wait for 11 seconds to let the operation timeout.")
    await asyncio.sleep(11)

    # As expected, only left preset has been updated
    self.assertEqual(ha_services[0].active_preset_index, _BAR_PRESET.index)
    self.assertEqual(ha_services[1].active_preset_index, _FOO_PRESET.index)

    self.logger.info("[REF] Enable preset synchronization")
    ha_services[0].other_server_in_binaural_set = ha_services[1]
    ha_services[1].other_server_in_binaural_set = ha_services[0]

    self.logger.info("[DUT] Set active preset for group again.")
    self.dut.bt.selectHapPresetForGroup(group_id, _BAR_PRESET.index)
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      for i, has in enumerate(ha_services):
        self.logger.info("[REF-%d] Verify active preset", i)
        await has.wait_for_active_preset_index(_BAR_PRESET.index)


if __name__ == "__main__":
  test_runner.main()
