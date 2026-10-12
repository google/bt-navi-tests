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
import secrets

from bumble import hci
from bumble.profiles import aics
from bumble.profiles import bap
from bumble.profiles import cap
from bumble.profiles import csip
from bumble.profiles import vcs
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import aics as aics_ext
from navi.bumble_ext import vocs
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api

_Property = android_constants.Property
_TIMEOUT = 10.0


class VcpTest(navi_test_base.TwoDevicesTestBase):
  """Tests for LE Audio Volume Control Profile."""

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.device.is_emulator:
      # Force enable VCP controller and CSIP coordinator on the emulator.
      self.dut.shell(["setprop", _Property.VCP_CONTROLLER_ENABLED, "true"])
      self.dut.shell(
          ["setprop", _Property.CSIP_SET_COORDINATOR_ENABLED, "true"]
      )
    if self.dut.getprop(_Property.VCP_CONTROLLER_ENABLED) != "true":
      raise signals.TestAbortClass("VCP Controller is not enabled on DUT.")

    if not self.dut.is_le_audio_supported:
      raise signals.TestAbortClass("[DUT] Device does not support LE Audio.")

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    # Make sure VOLUME_CONTROL profile is connected after Bluetooth restart,
    # or registerVolumeControlCallback will silently fail while service is null.
    self.dut.bt.waitForProfileReady(android_constants.Profile.VOLUME_CONTROL)

  async def _check_default_aics_properties(
      self, aics_cb: bl4a_api.AudioInputControl
  ) -> None:
    """Checks default AICS properties."""
    async with self.assert_not_timeout(_TIMEOUT):
      self.logger.info(
          "is_writable: %s", await aics_cb.is_description_writable()
      )
      self.logger.info("description: %s", await aics_cb.get_description())
      self.logger.info(
          "gain_setting_unit: %d", await aics_cb.get_gain_setting_unit()
      )
      self.logger.info(
          "gain_setting_min: %d", await aics_cb.get_gain_setting_min()
      )
      self.logger.info(
          "gain_setting_max: %d", await aics_cb.get_gain_setting_max()
      )
      self.logger.info("gain_setting: %d", await aics_cb.get_gain_setting())
      self.logger.info("mute: %s", await aics_cb.get_mute())
      self.logger.info("gain_mode: %s", await aics_cb.get_gain_mode())

  async def _setup_writable_aics_and_connect(
      self,
  ) -> bl4a_api.AudioInputControl:
    """Sets up VCS with one writable AICS and connects."""
    aics_service = aics_ext.AudioInputControlService(
        audio_input_description=aics.AudioInputDescription(
            audio_input_description="Init"
        ),
    )

    volume_control_service = vcs.VolumeControlService(
        included_services=[aics_service],
        volume_flags=vcs.VolumeFlags.VOLUME_SETTING_PERSISTED,
    )
    sirk = secrets.token_bytes(csip.SET_IDENTITY_RESOLVING_KEY_LENGTH)
    self.ref.device.add_services([
        volume_control_service,
        cap.CommonAudioServiceService(
            csip.CoordinatedSetIdentificationService(
                set_identity_resolving_key=sirk,
                set_identity_resolving_key_type=csip.SirkType.PLAINTEXT,
                coordinated_set_size=1,
            )
        ),
    ])

    self.logger.info("[DUT] Create bond with REF")
    with self.dut.bl4a.register_callback(
        bl4a_api.Module.VOLUME_CONTROL
    ) as vcp_cb:
      self.logger.info("[DUT] Setting VCP connection policy...")
      await self.le_connect_and_pair(hci.OwnAddressType.RANDOM, self.ref)
      self.logger.info("[DUT] Set MTU to 517 for REF")
      gatt_client = await self.dut.bl4a.connect_gatt_client(
          self.ref.random_address, transport=android_constants.Transport.LE
      )
      await gatt_client.request_mtu(517)
      self.dut.bt.vcpSetConnectionPolicy(
          self.ref.random_address, android_constants.ConnectionPolicy.ALLOWED
      )
      self.logger.info("[DUT] Waiting for VCP connection...")
      await vcp_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.random_address,
              state=android_constants.ConnectionState.CONNECTED,
          )
      )

    self.logger.info("[DUT] Getting AICS...")
    aics_cb = self.dut.bl4a.get_aics(self.ref.random_address, 0)
    self.test_case_context.enter_context(aics_cb)

    if await aics_cb.get_gain_setting() != 0:
      await aics_cb.wait_for_event(
          bl4a_api.AicsGainSettingChanged(gain_setting=0), timeout=5
      )
    if await aics_cb.get_mute() != aics.Mute.NOT_MUTED:
      await aics_cb.wait_for_event(
          bl4a_api.AicsMuteChanged(mute=aics.Mute.NOT_MUTED), timeout=5
      )
    if await aics_cb.get_gain_mode() != aics.GainMode.MANUAL:
      await aics_cb.wait_for_event(
          bl4a_api.AicsGainModeChanged(gain_mode=aics.GainMode.MANUAL),
          timeout=5,
      )
    if await aics_cb.get_description() != "Init":
      await aics_cb.wait_for_event(
          bl4a_api.AicsDescriptionChanged(description="Init"), timeout=5
      )
    await self._check_default_aics_properties(aics_cb)
    return aics_cb

  async def test_aics_set_description(self) -> None:
    """Tests that AICS description can be set."""
    aics_cb = await self._setup_writable_aics_and_connect()
    async with self.assert_not_timeout(_TIMEOUT):
      is_writable = await aics_cb.is_description_writable()
      self.assertTrue(is_writable)
      self.assertTrue(await aics_cb.set_description("New Description"))
      await aics_cb.wait_for_event(
          bl4a_api.AicsDescriptionChanged(description="New Description")
      )
      self.assertEqual(await aics_cb.get_description(), "New Description")

  async def test_aics_set_gain_setting(self) -> None:
    """Tests that AICS gain setting can be set."""
    aics_cb = await self._setup_writable_aics_and_connect()

    async with self.assert_not_timeout(_TIMEOUT):
      self.assertTrue(await aics_cb.set_gain_setting(100))
      await aics_cb.wait_for_event(
          bl4a_api.AicsGainSettingChanged(gain_setting=100)
      )
      self.assertEqual(await aics_cb.get_gain_setting(), 100)

  async def test_aics_set_mute(self) -> None:
    """Tests that AICS mute state can be set."""
    aics_cb = await self._setup_writable_aics_and_connect()

    async with self.assert_not_timeout(_TIMEOUT):
      self.assertTrue(await aics_cb.set_mute(aics.Mute.MUTED))
      await aics_cb.wait_for_event(
          bl4a_api.AicsMuteChanged(mute=aics.Mute.MUTED)
      )
      self.assertEqual(await aics_cb.get_mute(), aics.Mute.MUTED)

  async def test_aics_set_gain_mode(self) -> None:
    """Tests that AICS gain mode can be set."""
    aics_cb = await self._setup_writable_aics_and_connect()

    async with self.assert_not_timeout(_TIMEOUT):
      self.assertTrue(await aics_cb.set_gain_mode(aics.GainMode.AUTOMATIC))
      await aics_cb.wait_for_event(
          bl4a_api.AicsGainModeChanged(gain_mode=aics.GainMode.AUTOMATIC)
      )
      self.assertEqual(
          await aics_cb.get_gain_mode(),
          aics.GainMode.AUTOMATIC,
      )

  async def _setup_vocs_and_connect(
      self,
  ) -> bl4a_api.CallbackHandler:
    """Sets up VCS with one VOCS and connects."""
    volume_control_service = vcs.VolumeControlService(
        included_services=[
            vocs.VolumeOffsetControlService(
                change_counter=0,
                volume_offset=0,
                audio_location=bap.AudioLocation.FRONT_LEFT,
            )
        ]
    )
    sirk = secrets.token_bytes(csip.SET_IDENTITY_RESOLVING_KEY_LENGTH)
    self.ref.device.add_services([
        volume_control_service,
        cap.CommonAudioServiceService(
            csip.CoordinatedSetIdentificationService(
                set_identity_resolving_key=sirk,
                set_identity_resolving_key_type=csip.SirkType.PLAINTEXT,
                coordinated_set_size=1,
            )
        ),
    ])

    self.logger.info("[DUT] Create bond with REF")
    vcp_cb = self.dut.bl4a.register_callback(bl4a_api.Module.VOLUME_CONTROL)
    self.test_case_context.callback(vcp_cb.close)
    await self.le_connect_and_pair(hci.OwnAddressType.RANDOM, self.ref)
    self.logger.info("[DUT] Set MTU to 517 for REF")
    gatt_client = await self.dut.bl4a.connect_gatt_client(
        self.ref.random_address, transport=android_constants.Transport.LE
    )
    await gatt_client.request_mtu(517)
    self.logger.info("[DUT] Setting VCP connection policy...")
    self.dut.bt.vcpSetConnectionPolicy(
        self.ref.random_address, android_constants.ConnectionPolicy.ALLOWED
    )
    self.logger.info("[DUT] Waiting for VCP connection...")
    await vcp_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.random_address,
            state=android_constants.ConnectionState.CONNECTED,
        )
    )

    self.logger.info("[DUT] Getting VOCS...")

    self.logger.info("[DUT] Waiting for VOCS properties to be ready...")
    await vcp_cb.wait_for_event(
        bl4a_api.VocsOffsetStateChanged(
            address=self.ref.random_address,
            instance_id=1,
            offset=0,
        )
    )
    await vcp_cb.wait_for_event(
        bl4a_api.VocsAudioLocationChanged(
            address=self.ref.random_address,
            instance_id=1,
            audio_location=int(bap.AudioLocation.FRONT_LEFT),
        )
    )
    self.logger.info("[DUT] VOCS is ready.")
    return vcp_cb

  async def test_vocs_set_volume_offset(self) -> None:
    """Tests that VOCS volume offset can be set."""
    vcp_cb = await self._setup_vocs_and_connect()
    self.assertTrue(
        self.dut.bt.isVolumeOffsetAvailable(self.ref.random_address)
    )
    self.assertEqual(
        self.dut.bt.getNumberofVocsInstances(self.ref.random_address),
        1,
    )

    async with self.assert_not_timeout(_TIMEOUT):
      await asyncio.to_thread(
          self.dut.bt.setVolumeOffset, self.ref.random_address, 1, 100
      )
      self.logger.info("[DUT] Waiting for VOCS offset to be changed...")
      # we are getting the offset 256 times the value we pass
      await vcp_cb.wait_for_event(
          event=bl4a_api.VocsOffsetStateChanged(
              address=self.ref.random_address,
              instance_id=1,
              offset=25600,
          )
      )

  async def test_vocs_set_device_volume(self) -> None:
    """Tests that VOCS device volume can be set."""
    vcp_cb = await self._setup_vocs_and_connect()
    self.assertTrue(
        self.dut.bt.isVolumeOffsetAvailable(self.ref.random_address)
    )
    self.assertEqual(
        self.dut.bt.getNumberofVocsInstances(self.ref.random_address),
        1,
    )
    async with self.assert_not_timeout(_TIMEOUT):
      await asyncio.to_thread(
          self.dut.bt.vcpSetDeviceVolume, self.ref.random_address, 100, True
      )
      await vcp_cb.wait_for_event(
          bl4a_api.DeviceVolumeChanged(
              address=self.ref.random_address,
              volume=100,
          )
      )
