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

"""Tests HFP AG and HF coexistence scenarios."""

from __future__ import annotations

import asyncio
from unittest import mock

from bumble import device
from bumble import hfp
from bumble import rfcomm
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import hfp as hfp_ext
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants

_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0
_HFP_HF_SDP_HANDLE = 4
_HFP_AG_SDP_HANDLE = 5
_CALLER_NAME = "Pixel Bluetooth"
_CALLER_NUMBER = "123456789"


# pylint: disable=g-async-test-method-unrunnable
class HfpCoexTest(navi_test_base.MultiDevicesTestBase):

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if len(self.refs) < 2:
      raise signals.TestAbortClass("This test requires at least 2 REF devices.")

    for i, ref in enumerate(self.refs):
      self.logger.info(
          "[REF-%d] Disable CTKD over Classic to avoid blocking SDP.", i
      )
      ref.config.classic_smp_enabled = False

    if self.dut.getprop(android_constants.Property.HFP_AG_ENABLED) != "true":
      raise signals.TestAbortClass("HFP(AG) is not enabled on DUT.")

    if self.dut.getprop(android_constants.Property.HFP_HF_ENABLED) != "true":
      raise signals.TestAbortClass("HFP(HF) is not enabled on DUT.")

    if self.dut.getprop("bluetooth.hfp.reject_sco_if_hfpc_connected") != "true":
      raise signals.TestAbortClass(
          "bluetooth.hfp.reject_sco_if_hfpc_connected is not enabled on DUT."
      )

  async def test_answer_incoming_call_no_sco_to_headset(self) -> None:
    """Tests HFP AG does not initiate SCO to headset during call lifecycle.

    Verifies that HFP AG does not initiate SCO to headset during both incoming
    (ringing) and active (answered) call phases when HFP HF client is connected.

    Test steps:
      1. Setup HFP AG on REF_1 (refs[0]).
      2. Setup HFP HF on REF_2 (refs[1]).
      3. Connect DUT (HF) to REF_1 (AG).
      4. Connect DUT (AG) to REF_2 (HF).
      5. Trigger incoming call from REF_1 (AG).
      6. Verify REF_2 (headset) receives the ring alert from DUT (AG).
      7. Assert REF_2 (HF) does not receive SCO connection during ringing.
      8. Answer call on DUT.
      9. Verify call is answered on REF_1 (AG).
      10. Assert REF_2 (HF) does not receive SCO connection after answering.
    """
    ref_ag = self.refs[0]
    ref_hf = self.refs[1]

    # 1. Setup HFP AG on REF_1
    ref_ag_protocols = asyncio.Queue[hfp.AgProtocol]()

    def on_dlc_ag(dlc: rfcomm.DLC):
      ref_ag_protocols.put_nowait(
          hfp.AgProtocol(dlc, hfp_ext.make_ag_configuration())
      )

    ref_ag.device.sdp_service_records = {
        _HFP_AG_SDP_HANDLE: (
            hfp_ext.AudioGatewaySdpRecord(
                service_record_handle=_HFP_AG_SDP_HANDLE,
                rfcomm_channel=rfcomm.Server(ref_ag.device).listen(on_dlc_ag),
                version=hfp.ProfileVersion.V1_8,
                supported_features=hfp_ext.make_ag_sdp_features(
                    hfp_ext.make_ag_configuration()
                ),
            ).to_service_attributes()
        )
    }

    # 2. Setup HFP HF on REF_2
    ref_hf_protocol_queue = hfp_ext.HfProtocol.setup_server(
        ref_hf.device,
        sdp_handle=_HFP_HF_SDP_HANDLE,
        configuration=hfp_ext.make_hf_configuration(),
    )

    # Register callbacks on DUT
    dut_ag_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HFP_AG)
    dut_hf_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HFP_HF)
    self.test_case_context.push(dut_ag_cb)
    self.test_case_context.push(dut_hf_cb)

    # 3. Connect DUT (HF) to REF_1 (AG)
    self.logger.info("[DUT] Connect and pair REF_1 (AG).")
    await self.classic_connect_and_pair(ref_ag, connect_profiles=True)

    self.logger.info("[DUT] Wait for HFP HF connected.")
    await dut_hf_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=ref_ag.address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    self.logger.info("[REF_1] Wait for AG protocol connected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_ag_protocol = await ref_ag_protocols.get()

    # 4. Connect DUT (AG) to REF_2 (HF)
    self.logger.info("[DUT] Connect and pair REF_2 (HF).")
    await self.classic_connect_and_pair(ref_hf, connect_profiles=True)

    self.logger.info("[DUT] Wait for HFP AG connected.")
    await dut_ag_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(ref_hf.address),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    self.logger.info("[REF_2] Wait for HF protocol connected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_hf_protocol = await ref_hf_protocol_queue.get()

    ref_hf_ring_event = asyncio.Event()
    ref_hf_protocol.on(hfp.HfProtocol.EVENT_RING, ref_hf_ring_event.set)

    # 5. Trigger incoming call from REF_1 (AG)
    self.logger.info("[REF_1] Trigger incoming call.")
    call_info = hfp.CallInfo(
        index=1,
        direction=hfp.CallInfoDirection.MOBILE_TERMINATED_CALL,
        status=hfp.CallInfoStatus.INCOMING,
        mode=hfp.CallInfoMode.VOICE,
        multi_party=hfp.CallInfoMultiParty.NOT_IN_CONFERENCE,
        number="+1234567890",
    )
    ref_ag_protocol.calls.append(call_info)
    ref_ag_protocol.update_ag_indicator(
        hfp.AgIndicator.CALL_SETUP,
        hfp.CallSetupAgIndicator.INCOMING_CALL_PROCESS,
    )
    ref_ag_protocol.send_ring()

    # 6. Verify REF_2 (headset) receives the ring alert from DUT (AG)
    self.logger.info("[REF_2] Wait for ringtone.")
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF_2] Wait for ringtone timeout.",
    ):
      await ref_hf_ring_event.wait()

    # 7. Assert REF_2 (HF) does not receive SCO connection during ringing
    self.logger.info("[REF_2] Check SCO is not connected during ringing.")
    async with self.assert_timeout(
        delay=3.0,
        msg="SCO should not be initiated to REF_2 during ringing",
    ):
      # We expect this to timeout because SCO should not connect.
      await dut_ag_cb.wait_for_event(
          bl4a_api.HfpAgAudioStateChanged(
              address=ref_hf.address,
              state=android_constants.ScoState.CONNECTED,
          )
      )

    # Also check sco_links on REF_2 during ringing
    self.assertEmpty(ref_hf.device.sco_links)

    # 8. Answer call on DUT
    answered = asyncio.Event()
    ref_ag_protocol.once(ref_ag_protocol.EVENT_ANSWER, answered.set)

    self.logger.info("[DUT] Answer call.")
    self.dut.shell("input keyevent KEYCODE_CALL")

    # 9. Verify call is answered on REF_1 (AG)
    self.logger.info("[REF_1] Wait for call answered.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await answered.wait()

    # Update AG call state to active on REF_1
    call_info.status = hfp.CallInfoStatus.ACTIVE
    ref_ag_protocol.update_ag_indicator(hfp.AgIndicator.CALL, 1)
    ref_ag_protocol.update_ag_indicator(
        hfp.AgIndicator.CALL_SETUP,
        hfp.CallSetupAgIndicator.NOT_IN_CALL_SETUP,
    )

    # 10. Assert REF_2 (HF) does not receive SCO connection after answering
    self.logger.info("[REF_2] Check SCO is not connected after answering.")
    async with self.assert_timeout(
        delay=3.0,
        msg="SCO should not be initiated to REF_2 after answering",
    ):
      await dut_ag_cb.wait_for_event(
          bl4a_api.HfpAgAudioStateChanged(
              address=ref_hf.address,
              state=android_constants.ScoState.CONNECTED,
          )
      )

    # Also check sco_links on REF_2 after answering
    self.assertEmpty(ref_hf.device.sco_links)

  async def test_multipoint_ringtone(self) -> None:
    """Tests phone call, ringtone is played on both REF-HF and DUT.

    Test steps:
      1. Setup HFP HF on REF-HF.
      2. Setup HFP AG on REF-AG.
      3. Connect and pair DUT to REF-HF.
      4. Connect and pair DUT to REF-AG.
      5. Make a phone call from REF-AG.
    """
    if self.dut.getprop(android_constants.Property.HFP_HF_ENABLED) != "true":
      self.skipTest("DUT does not have HFP HF enabled.")

    if self.dut.getprop(android_constants.Property.HFP_AG_ENABLED) != "true":
      self.skipTest("DUT does not have HFP AG enabled.")

    ref_hf_protocol_queue = hfp_ext.HfProtocol.setup_server(
        self.refs[0].device,
        sdp_handle=_HFP_HF_SDP_HANDLE,
        configuration=hfp_ext.make_hf_configuration(),
    )

    ref_ag_protocols = asyncio.Queue[hfp.AgProtocol]()

    def on_dlc(dlc: rfcomm.DLC):
      ref_ag_protocols.put_nowait(
          hfp.AgProtocol(dlc, hfp_ext.make_ag_configuration())
      )

    self.refs[1].device.sdp_service_records = {
        _HFP_AG_SDP_HANDLE: (
            hfp_ext.AudioGatewaySdpRecord(
                service_record_handle=_HFP_AG_SDP_HANDLE,
                rfcomm_channel=rfcomm.Server(self.refs[1].device).listen(
                    on_dlc
                ),
                version=hfp.ProfileVersion.V1_8,
                supported_features=hfp_ext.make_ag_sdp_features(
                    hfp_ext.make_ag_configuration()
                ),
            ).to_service_attributes()
        )
    }

    dut_ag_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HFP_AG)
    dut_hf_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HFP_HF)
    dut_telecom_cb = self.dut.bl4a.register_callback(bl4a_api.Module.TELECOM)
    self.test_case_context.push(dut_ag_cb)
    self.test_case_context.push(dut_hf_cb)
    self.test_case_context.push(dut_telecom_cb)

    await self.classic_connect_and_pair(self.refs[0], connect_profiles=True)

    self.logger.info("[DUT] Wait for HFP AG connected on REF-HF.")
    await dut_ag_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.refs[0].address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
    )

    self.logger.info("[REF-HF] Wait for HF protocol connected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_hf_protocol = await ref_hf_protocol_queue.get()

    ref_hf_ring_event = asyncio.Event()
    ref_hf_protocol.on(hfp.HfProtocol.EVENT_RING, ref_hf_ring_event.set)

    await self.classic_connect_and_pair(self.refs[1], connect_profiles=True)

    self.logger.info("[DUT] Wait for HFP HF connected on REF-AG.")
    await dut_hf_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.refs[1].address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
    )

    self.logger.info("[REF-AG] Wait for AG protocol connected.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      ref_ag_protocol = await ref_ag_protocols.get()

    self.logger.info("[REF-AG] Update call state.")
    call_info = hfp.CallInfo(
        index=1,
        direction=hfp.CallInfoDirection.MOBILE_TERMINATED_CALL,
        status=hfp.CallInfoStatus.INCOMING,
        mode=hfp.CallInfoMode.VOICE,
        multi_party=hfp.CallInfoMultiParty.NOT_IN_CONFERENCE,
        number="+1234567890",
    )
    ref_ag_protocol.calls.append(call_info)
    ref_ag_protocol.update_ag_indicator(
        hfp.AgIndicator.CALL_SETUP,
        hfp.CallSetupAgIndicator.INCOMING_CALL_PROCESS,
    )

    self.logger.info("[DUT] Wait for call ringing.")
    await dut_telecom_cb.wait_for_event(
        bl4a_api.CallStateChanged(
            handle=mock.ANY,
            name=mock.ANY,
            state=android_constants.CallState.RINGING,
        )
    )

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[REF-HF] Wait for ringtone.",
    ):
      await ref_hf_ring_event.wait()

  async def test_multipoint_call(self) -> None:
    """Tests phone call, SCO connection is only connected to REF-AG.

    Test steps:
      1. Setup HFP HF on REF-HF.
      2. Setup HFP AG on REF-AG.
      3. Connect and pair DUT to REF-HF.
      4. Connect and pair DUT to REF-AG.
      5. Make a phone call from REF-AG.
      6. Answer the call on DUT.
      7. Wait for SCO connected only on REF-AG.
    """
    await self.test_multipoint_ringtone()

    sco_link_hf = asyncio.Queue[device.ScoLink]()
    self.refs[0].device.on(
        self.refs[0].device.EVENT_SCO_CONNECTION, sco_link_hf.put_nowait
    )

    self.logger.info("[DUT] Answer call.")
    self.dut.shell("input keyevent KEYCODE_CALL")

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF-HF] Wait for SCO connected.")
      await sco_link_hf.get()

    self.logger.info("[REF-AG] Check SCO is not connected.")
    self.assertEmpty(self.refs[1].device.sco_links)

  async def test_multidevice_hf_switch(self) -> None:
    """Tests DUT switch active hfp devices.

    Test steps:
      1. Setup two HFP HF devices.
      2. DUT pair with REF0.
      3. DUT pair with REF1.
      4. DUT make outgoing call.
      5. DUT answer the call.
      6. DUT switch active device to REF0.
      7. DUT switch active device to REF1.
    """
    if self.dut.bt.maxConnectedAudioDevices() < 2:
      self.skipTest("[DUT] Multi-device HF is not supported.")

    with self.dut.bl4a.register_callback(bl4a_api.Module.HFP_AG) as dut_hfp_cb:
      for i, ref in enumerate(self.refs):
        self.logger.info("[REF-%d] Setup HFP HF", i)
        hfp_ext.HfProtocol.setup_server(
            ref.device,
            sdp_handle=_HFP_HF_SDP_HANDLE,
            configuration=hfp_ext.make_hf_configuration(),
        )

        await self.classic_connect_and_pair(ref, connect_profiles=True)

        self.logger.info("[DUT] Wait for HFP connected to REF-%d", i)
        await dut_hfp_cb.wait_for_event(
            bl4a_api.ProfileActiveDeviceChanged(address=ref.address),
        )

    with (
        self.dut.bl4a.register_callback(
            bl4a_api.Module.TELECOM
        ) as dut_telecom_cb,
        self.dut.bl4a.make_phone_call(
            _CALLER_NAME,
            _CALLER_NUMBER,
            constants.Direction.OUTGOING,
        ) as call,
    ):
      self.logger.info("[DUT] Wait for call dialing.")
      await dut_telecom_cb.wait_for_event(
          bl4a_api.CallStateChanged(
              handle=mock.ANY,
              name=mock.ANY,
              state=android_constants.CallState.DIALING,
          ),
      )

      self.logger.info("[DUT] Answer call.")
      call.answer()

      self.logger.info("[DUT] Wait for call active.")
      await dut_telecom_cb.wait_for_event(
          bl4a_api.CallStateChanged(
              handle=mock.ANY,
              name=mock.ANY,
              state=android_constants.CallState.ACTIVE,
          ),
      )

      self.logger.info("[DUT] Start streaming.")
      self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)
      await asyncio.to_thread(self.dut.bt.audioPlaySine)

      # The default route should be REF1.
      for i, ref in enumerate(self.refs):
        with self.dut.bl4a.register_callback(
            bl4a_api.Module.HFP_AG
        ) as dut_hfp_cb:
          self.assertNotEqual(
              self.dut.bt.hfpAgGetAudioState(ref.address),
              android_constants.ScoState.CONNECTED,
              f"SCO is already connected to REF{i}.",
          )

          self.logger.info("[DUT] Switch to REF-%d", i)
          await asyncio.to_thread(
              self.dut.bt.setActiveDevice,
              ref.address,
              android_constants.ActiveDeviceUse.PHONE_CALL,
          )

          self.logger.info("[DUT] Wait for HFP connected to REF-%d", i)
          await dut_hfp_cb.wait_for_event(
              bl4a_api.ProfileActiveDeviceChanged(ref.address)
          )

          self.logger.info("[DUT] Wait for SCO connected to REF-%d", i)
          await dut_hfp_cb.wait_for_event(
              event=bl4a_api.HfpAgAudioStateChanged(
                  address=ref.address,
                  state=android_constants.ScoState.CONNECTED,
              ),
          )

      self.logger.info("[DUT] Terminate call.")
      call.close()


if __name__ == "__main__":
  test_runner.main()
