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

import asyncio
import datetime
from unittest import mock

from bumble import core
from bumble import hci
from bumble import sdp
from mobly import test_runner

from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import pyee_extensions

_Profile = android_constants.Profile
_DEFAULT_DISCOVER_TIMEOUT = 15
_DEFAULT_ACCEPT_TIMEOUT = 15.0
_DEFAULT_TIMEOUT = 10.0
_PROFILE_ID_TO_UUIDS: dict[int, set[core.UUID]] = {
    _Profile.HEADSET: {
        core.BT_HANDSFREE_AUDIO_GATEWAY_SERVICE,
        core.BT_GENERIC_AUDIO_SERVICE,
    },
    _Profile.HEADSET_CLIENT: {
        core.BT_HANDSFREE_SERVICE,
        core.BT_GENERIC_AUDIO_SERVICE,
    },
    _Profile.A2DP: {core.BT_AUDIO_SOURCE_SERVICE},
    _Profile.A2DP_SINK: {core.BT_AUDIO_SINK_SERVICE},
    _Profile.AVRCP: {core.BT_AV_REMOTE_CONTROL_TARGET_SERVICE},
    _Profile.AVRCP_CONTROLLER: {
        core.BT_AV_REMOTE_CONTROL_SERVICE,
        core.BT_AV_REMOTE_CONTROL_CONTROLLER_SERVICE,
    },
    _Profile.MAP: {core.BT_MESSAGE_ACCESS_SERVER_SERVICE},
    _Profile.PBAP: {core.BT_PHONEBOOK_ACCESS_PSE_SERVICE},
    _Profile.OPP: {core.BT_OBEX_OBJECT_PUSH_SERVICE},
    _Profile.PAN: {
        core.BT_PANU_SERVICE,
        core.BT_NAP_SERVICE,
    },
    _Profile.SAP: {core.BT_SIM_ACCESS_SERVICE},
}


_CLASSIC_PROFILES = (
    core.BT_AUDIO_SOURCE_SERVICE,  # A2DP
    core.BT_AUDIO_SINK_SERVICE,  # A2DP Sink
    # AVRCP (Android doesn't broadcast CT/TG specific UUIDs).
    core.BT_AV_REMOTE_CONTROL_SERVICE,
    core.BT_HEADSET_SERVICE,  # HSP
    core.BT_HEADSET_AUDIO_GATEWAY_SERVICE,  # HSP AG
    core.BT_HANDSFREE_SERVICE,  # HFP
    core.BT_HANDSFREE_AUDIO_GATEWAY_SERVICE,  # HFP AG
    core.BT_OBEX_OBJECT_PUSH_SERVICE,  # OPP
    core.BT_PANU_SERVICE,  # PANU
    core.BT_NAP_SERVICE,  # NAP
    core.BT_GN_SERVICE,  # GN
    core.BT_SIM_ACCESS_SERVICE,  # SAP
    core.BT_HUMAN_INTERFACE_DEVICE_SERVICE,  # HID
    core.BT_PHONEBOOK_ACCESS_PSE_SERVICE,  # PBAP PSE
    core.BT_PHONEBOOK_ACCESS_PCE_SERVICE,  # PBAP PCE
    core.BT_MESSAGE_ACCESS_SERVER_SERVICE,  # MAS
    core.BT_MESSAGE_NOTIFICATION_SERVER_SERVICE,  # MNS
)


def _make_sdp_service_record(
    profile_uuid: core.UUID,
) -> list[sdp.ServiceAttribute]:
  return [
      sdp.ServiceAttribute(
          sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([sdp.DataElement.uuid(profile_uuid)]),
      ),
      sdp.ServiceAttribute(
          sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
          sdp.DataElement.sequence([
              sdp.DataElement.sequence([
                  sdp.DataElement.uuid(core.BT_L2CAP_PROTOCOL_ID),
              ]),
          ]),
      ),
  ]


class ClassicHostTest(navi_test_base.TwoDevicesTestBase):

  @navi_test_base.retry(max_count=2)
  async def test_outgoing_classic_acl(self) -> None:
    """Test outgoing Classic ACL connection.

    Test steps:
      1. Create bond from DUT.
      2. Accept connection from REF.
      3. Wait for ACL connected on DUT.
      4. Cancel bond from DUT.
      5. Wait for ACL disconnected on DUT.
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      self.logger.info("[DUT] Create bond.")
      self.dut.bt.createBond(
          self.ref.address, android_constants.Transport.CLASSIC
      )

      self.logger.info("[REF] Accept connection.")
      await self.ref.device.accept(
          f"{self.dut.address}/P",
          timeout=_DEFAULT_ACCEPT_TIMEOUT,
      )

      self.logger.info("[DUT] Wait for ACL connected.")
      await dut_cb.wait_for_event(
          bl4a_api.AclConnected(
              address=self.ref.address,
              transport=android_constants.Transport.CLASSIC,
          ),
      )
      # disconnect() doesn"t work, because it only remove profile connections.
      self.logger.info("[DUT] Cancel bond.")
      self.dut.bt.cancelBond(self.ref.address)

      self.logger.info("[DUT] Wait for ACL disconnected.")
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected(
              address=self.ref.address,
              transport=android_constants.Transport.CLASSIC,
          ),
          timeout=datetime.timedelta(seconds=30),
      )

  @navi_test_base.retry(max_count=2)
  async def test_incoming_classic_acl(self) -> None:
    """Test incoming Classic ACL connection.

    Test steps:
      1. Create connection from REF.
      2. Wait for ACL connected on DUT.
      3. Disconnect from REF.
      4. Wait for ACL disconnected DUT.
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      self.logger.info("[REF] Create connection.")
      ref_dut_acl = await self.ref.device.connect(
          f"{self.dut.address}/P", transport=core.BT_BR_EDR_TRANSPORT
      )

      self.logger.info("[DUT] Wait for ACL connected.")
      await dut_cb.wait_for_event(
          bl4a_api.AclConnected(
              address=self.ref.address,
              transport=android_constants.Transport.CLASSIC,
          ),
      )

      self.logger.info("[REF] Disconnect.")
      await ref_dut_acl.disconnect()

      self.logger.info("[DUT] Wait for ACL disconnected.")
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected(
              address=self.ref.address,
              transport=android_constants.Transport.CLASSIC,
          ),
      )

  @navi_test_base.retry(max_count=2)
  async def test_inquiry(self) -> None:
    """Test inquiry.

    Test steps:
      1. Set REF in discoverable mode.
      2. Start discovery on DUT.
      3. Wait for DUT discovered.
    """
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      self.logger.info("[REF] Set discoverable.")
      await self.ref.device.set_discoverable(True)

      self.logger.info("[DUT] Start inquiry.")
      self.dut.bt.startInquiry()

      self.logger.info("[DUT] Wait for DUT discovered.")
      await dut_cb.wait_for_event(
          bl4a_api.DeviceFound(
              address=self.ref.address,
              name=mock.ANY,
              rssi=mock.ANY,
          )
      )

  async def test_discoverable(self) -> None:
    """Test ref discover DUT.

    Test steps:
      1. Set DUT in discoverable mode.
      2. Start discovery on REF.
      3. Wait for DUT discovered.
    """
    self.logger.info("[DUT] Set scan mode to CONNECTABLE_DISCOVERABLE.")
    self.dut.bt.setScanMode(android_constants.ScanMode.CONNECTABLE_DISCOVERABLE)

    with pyee_extensions.EventWatcher() as watcher:
      inquiry = asyncio.Event()

      @watcher.on(self.ref.device, "inquiry_result")
      def on_inquiry_result(address: hci.Address, *_) -> None:
        if address == hci.Address(f"{self.dut.address}/P"):
          inquiry.set()

      self.logger.info("[REF] Start discovery.")
      await self.ref.device.start_discovery()

      self.logger.info("[REF] Wait for DUT discover timeout.")
      async with self.assert_not_timeout(_DEFAULT_DISCOVER_TIMEOUT):
        await inquiry.wait()

  async def test_not_discoverable(self) -> None:
    """Test ref can not discover DUT.

    Test steps:
      1. Set DUT scan mode to NONE.
      2. Start discovery on REF.
      3. Wait for DUT discovered timeout.
    """
    self.logger.info("[DUT] Set scan mode to NONE.")
    self.dut.bt.setScanMode(android_constants.ScanMode.NONE)

    with pyee_extensions.EventWatcher() as watcher:
      inquiry = asyncio.Event()

      @watcher.on(self.ref.device, "inquiry_result")
      def on_inquiry_result(address: hci.Address, *_) -> None:
        if address == hci.Address(f"{self.dut.address}/P"):
          inquiry.set()

      self.logger.info("[REF] Start discovery.")
      await self.ref.device.start_discovery()

      self.logger.info("[REF] Wait for DUT discover timeout.")
      async with self.assert_timeout(_DEFAULT_DISCOVER_TIMEOUT):
        await inquiry.wait()

  @navi_test_base.retry(max_count=2)
  async def test_sdp_discovery_from_ref(self) -> None:
    """Test SDP discovery from REF.

    This is basic validation of:
      1. SDP can be connected.
      2. All declared profiles can be found in SDP.
      3. SDP attributes are the same as the ones returned by
      ServiceAttributeRequest.

    Further validation is performed in profile specific tests.

    Test steps:
      1. Connect and pair Classic ACL from DUT.
      2. Connect SDP client from REF.
      3. Search services and attributes from REF.
    """

    self.logger.info("[DUT] Start pairing.")
    pair_task = asyncio.create_task(self.classic_connect_and_pair())
    self.logger.info("[REF] Wait for ACL connection.")
    ref_dut_acl = await self.ref.device.accept(
        f"{self.dut.address}/P",
        timeout=_DEFAULT_ACCEPT_TIMEOUT,
    )

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT):
      sdp_client = sdp.Client(ref_dut_acl)
      self.logger.info("[REF] Connect SDP client to DUT.")
      await sdp_client.connect()

    self.logger.info("[DUT] Wait for pairing to complete.")
    await pair_task

    expected_profile_uuids = set().union(*[
        profile_uuids
        for profile_id in self.dut.bt.getSupportedProfiles()
        if (profile_uuids := _PROFILE_ID_TO_UUIDS.get(profile_id))
    ])

    # Validate that all declared profiles can be found in SDP.
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT):
      self.logger.info("[REF] Search for profiles.")
      attributes_list = await sdp_client.search_attributes(
          [core.BT_L2CAP_PROTOCOL_ID],
          [sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID],
      )
      actual_profile_uuids = set()
      for attributes in attributes_list:
        for attribute in attributes:
          for element in attribute.value.value:
            actual_profile_uuids.add(element.value)
      self.assertContainsSubset(
          expected_profile_uuids,
          actual_profile_uuids,
          "Service class ID list attributes are not the same as expected.",
      )

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT):
      self.logger.info("[REF] Search services.")
      handles = await sdp_client.search_services([core.BT_L2CAP_PROTOCOL_ID])
      self.assertNotEmpty(handles, "No handles found for L2CAP.")
      self.logger.info("[REF] Get attributes for handles: %s", handles)
      attributes_list = [
          await sdp_client.get_attributes(
              handle, [sdp.SDP_ALL_ATTRIBUTES_RANGE]
          )
          for handle in handles
      ]
      # Make sure that the attributes are the same as the ones returned by
      # ServiceAttributeRequest.
      self.logger.info("[REF] Validate attributes.")
      self.assertCountEqual(
          attributes_list,
          await sdp_client.search_attributes(
              [core.BT_L2CAP_PROTOCOL_ID], [sdp.SDP_ALL_ATTRIBUTES_RANGE]
          ),
          "Service attributes are not the same as returned by"
          " ServiceAttributeRequest.",
      )

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT):
      self.logger.info("[REF] Disconnect SDP client.")
      await sdp_client.disconnect()

  async def test_is_discovering_state(self) -> None:
    """Test isDiscovering API.

    Test steps:
      1. Verify isDiscovering returns False.
      2. Start inquiry.
      3. Verify isDiscovering returns True.
      4. Stop inquiry.
      5. Verify isDiscovering returns False.
    """
    self.dut.bt.stopInquiry()
    self.assertFalse(
        self.dut.bt.isDiscovering(), "DUT should not be discovering initially."
    )

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      self.dut.bt.startInquiry()
      try:
        await dut_cb.wait_for_event(
            bl4a_api.DiscoveryStarted(),
            timeout=_DEFAULT_TIMEOUT,
        )
        self.assertTrue(
            self.dut.bt.isDiscovering(), "DUT should be discovering."
        )
      finally:
        self.dut.bt.stopInquiry()
        await dut_cb.wait_for_event(
            bl4a_api.DiscoveryFinished(),
            timeout=_DEFAULT_TIMEOUT,
        )
      self.assertFalse(
          self.dut.bt.isDiscovering(), "DUT should stop discovering."
      )

  async def test_sdp_discovery_from_dut(self) -> None:
    """Test SDP discovery from DUT."""

    self.ref.device.sdp_service_records = {
        i + 1: _make_sdp_service_record(profile_uuid)
        for i, profile_uuid in enumerate(_CLASSIC_PROFILES)
    }

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      await self.classic_connect_and_pair()

      self.logger.info("[DUT] Wait for UUID changed.")
      event = await dut_cb.wait_for_event(
          bl4a_api.UuidChanged(
              address=self.ref.address,
              uuids=mock.ANY,
          ),
      )
      self.assertCountEqual(
          [core.UUID(uuid) for uuid in event.uuids or []],
          _CLASSIC_PROFILES,
          "Service UUIDs are not the same as expected.",
      )


if __name__ == "__main__":
  test_runner.main()
