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

"""Tests for HID Headtracker implementation on Android."""

import asyncio
import struct
from unittest import mock

from bumble import att
from bumble import core
from bumble import gatt
from bumble import hci
from bumble.profiles import bap
from bumble.profiles import le_audio
from bumble.profiles import pacs
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import ascs
from navi.bumble_ext import hid
from navi.bumble_ext import pacs as pacs_ext
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api

_VENDOR_COMPANY_ID_GOOGLE = 0x00E0
_HEADTRACKER_METADATA_LENGTH = 1
_HEADTRACKER_METADATA_TYPE_VALUE = 1

_AndroidProperty = android_constants.Property


class HidHeadtrackerTest(navi_test_base.TwoDevicesTestBase):

  def _setup_lea_services(self) -> None:
    self.ref.device.add_service(
        pacs_ext.make_pacs(
            source_pacs=[
                pacs.PacRecord(
                    coding_format=hci.CodingFormat(hci.CodecID.LC3),
                    codec_specific_capabilities=bap.CodecSpecificCapabilities(
                        supported_sampling_frequencies=(
                            bap.SupportedSamplingFrequency.FREQ_16000
                            | bap.SupportedSamplingFrequency.FREQ_32000
                            | bap.SupportedSamplingFrequency.FREQ_48000
                        ),
                        supported_frame_durations=(
                            bap.SupportedFrameDuration.DURATION_7500_US_SUPPORTED
                            | bap.SupportedFrameDuration.DURATION_10000_US_SUPPORTED
                        ),
                        supported_audio_channel_count=[1],
                        min_octets_per_codec_frame=13,
                        max_octets_per_codec_frame=120,
                        supported_max_codec_frames_per_sdu=1,
                    ),
                ),
                pacs.PacRecord(
                    coding_format=hci.CodingFormat(
                        codec_id=hci.CodecID.VENDOR_SPECIFIC,
                        company_id=_VENDOR_COMPANY_ID_GOOGLE,
                        vendor_specific_codec_id=0x0002,
                    ),
                    codec_specific_capabilities=bap.CodecSpecificCapabilities(
                        supported_sampling_frequencies=bap.SupportedSamplingFrequency.FREQ_48000,
                        supported_frame_durations=(
                            bap.SupportedFrameDuration.DURATION_7500_US_SUPPORTED
                            | bap.SupportedFrameDuration.DURATION_10000_US_SUPPORTED
                        ),
                        supported_audio_channel_count=[1],
                        min_octets_per_codec_frame=13,
                        max_octets_per_codec_frame=120,
                        supported_max_codec_frames_per_sdu=1,
                    ),
                    metadata=le_audio.Metadata([
                        le_audio.Metadata.Entry(
                            le_audio.Metadata.Tag.VENDOR_SPECIFIC,
                            data=struct.pack(
                                "<HBBB",
                                _VENDOR_COMPANY_ID_GOOGLE,
                                _HEADTRACKER_METADATA_LENGTH,
                                _HEADTRACKER_METADATA_TYPE_VALUE,
                                hid.HeadtrackerTransport.ACL.value,
                            ),
                        )
                    ]),
                ),
            ],
        )
    )
    self.ref.device.add_service(
        ascs.AudioStreamControlService(
            self.ref.device,
            sink_ase_id=[1],
            source_ase_id=[2],
        )
    )

  def _setup_hogp_service(self) -> None:
    hogp_service = gatt.Service(
        gatt.GATT_HUMAN_INTERFACE_DEVICE_SERVICE,
        [
            gatt.Characteristic(
                gatt.GATT_PROTOCOL_MODE_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ,
                gatt.Characteristic.READABLE,
                bytes([hid.ProtocolMode.REPORT_PROTOCOL.value]),
            ),
            gatt.Characteristic(
                gatt.GATT_HID_INFORMATION_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ,
                gatt.Characteristic.READABLE,
                bytes([0x11, 0x01, 0x00, 0x03]),
            ),
            gatt.Characteristic(
                gatt.GATT_HID_CONTROL_POINT_CHARACTERISTIC,
                gatt.Characteristic.WRITE_WITHOUT_RESPONSE,
                gatt.Characteristic.WRITEABLE,
            ),
            gatt.Characteristic(
                gatt.GATT_REPORT_MAP_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ,
                gatt.Characteristic.READABLE,
                hid.DEFAULT_REPORT_MAP,
            ),
            gatt.Characteristic(
                gatt.GATT_REPORT_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ
                | gatt.Characteristic.Properties.WRITE
                | gatt.Characteristic.Properties.NOTIFY,
                gatt.Characteristic.READABLE | gatt.Characteristic.WRITEABLE,
                bytes(8),
                [
                    gatt.Descriptor(
                        gatt.GATT_REPORT_REFERENCE_DESCRIPTOR,
                        gatt.Descriptor.READABLE,
                        bytes([0x01, hid.ReportType.INPUT_REPORT.value]),
                    )
                ],
            ),
        ],
    )
    self.ref.device.add_service(hogp_service)

  async def _connect_headtracker(self) -> hid.HeadtrackerService:
    ht_service = hid.HeadtrackerService(self.ref.device)
    self.ref.device.add_service(ht_service)
    condition = asyncio.Condition()
    report_char_subscribers: list[att.Bearer | None] = [None]

    @ht_service.report_characteristic.on(gatt.Characteristic.EVENT_SUBSCRIPTION)
    async def on_subscription(
        bearer: att.Bearer, notify_enabled: bool, indicate_enabled: bool
    ) -> None:
      del indicate_enabled
      async with condition:
        report_char_subscribers[0] = bearer if notify_enabled else None
        condition.notify_all()

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      self.logger.info("[DUT] Pair with REF (Head Tracker)")
      await self.le_connect_and_pair(
          hci.OwnAddressType.RANDOM, connect_profiles=True
      )
      self.logger.info("[DUT] Wait for Head Tracker connected over LE")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.random_address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )

    self.logger.info(
        "[DUT] Wait for Head Tracker report characteristic subscription"
    )
    async with condition:
      await condition.wait_for(lambda: report_char_subscribers[0] is not None)
    return ht_service

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.device.adb.getprop(hid.PROPERTY_HID_HOST_SUPPORTED) != "true":
      raise signals.TestAbortClass("HID host is not supported on DUT")

    self.ref.config.cis_enabled = True
    self.ref.device.cis_enabled = True

    self.setprop_for_class_context(
        _AndroidProperty.LEAUDIO_BYPASS_ALLOW_LIST, "true"
    )

  @override
  async def async_teardown_test(self) -> None:
    self.dut.bt.clearCompatibleSpatizlierDevices()
    await super().async_teardown_test()

  async def test_enable_headtracker(self) -> None:
    """Tests enabling headtracker.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Verify the HID connection is established.
      3. Verify the LE Audio connection is established.
      4. Add compatible spatizlier device.
      5. Verify the compatible spatizlier device is added.
      6. Enable headtracker.
      7. Verify the headtracker is enabled.
    """
    if not self.dut.is_le_audio_supported:
      raise signals.TestSkip("[DUT] Unicast client is not enabled")
    if self.dut.getprop("ro.audio.spatializer_enabled") != "true":
      raise signals.TestSkip("Spatializer is not enabled")

    self.dut.bt.setSpatializerEnabled(True)
    self.ref.device.add_service(hid.HeadtrackerService(self.ref.device))
    self._setup_lea_services()
    dut_hid_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    dut_lea_cb = self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO)
    dut_ht_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HEADTRACKER)
    self.test_case_context.enter_context(dut_hid_cb)
    self.test_case_context.enter_context(dut_lea_cb)
    self.test_case_context.enter_context(dut_ht_cb)

    self.logger.info("[DUT] Pair with REF")
    await self.le_connect_and_pair(
        hci.OwnAddressType.RANDOM, connect_profiles=True
    )
    self.logger.info("[DUT] Wait for HID connected")
    await dut_hid_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.random_address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
    )
    self.logger.info("[DUT] Wait for LE Audio active device changed")
    await dut_lea_cb.wait_for_event(
        bl4a_api.ProfileActiveDeviceChanged(address=self.ref.random_address),
    )

    self.logger.info("[DUT] Add compatible spatizlier device")
    self.dut.bt.addCompatibleSpatizlierDevice(
        android_constants.AudioDeviceRole.OUTPUT,
        android_constants.AudioDeviceType.BLE_HEADSET,
        self.ref.random_address,
    )

    compatible_spatizlier_devices = self.dut.bt.getCompatibleSpatizlierDevices()
    self.logger.info(
        "[DUT] Compatible Spatizlier devices: %s",
        compatible_spatizlier_devices,
    )
    self.assertIn(self.ref.random_address, compatible_spatizlier_devices)

    self.logger.info("[DUT] Set headtracker enabled")
    self.dut.bt.setHeadtrackerEnabled(
        android_constants.AudioDeviceRole.OUTPUT,
        android_constants.AudioDeviceType.BLE_HEADSET,
        self.ref.random_address,
        True,
    )

    # Wait for headtracker to be available (dynamic sensor registration)
    self.logger.info("[DUT] Wait for headtracker to be available")
    await dut_ht_cb.wait_for_event(
        bl4a_api.HeadTrackerAvailableChanged(available=True)
    )

    # TODO: Re-enable this check once the bug is fixed.
    # is_headtracker_enabled = self.dut.bt.getHeadtrackerEnabled(
    #     android_constants.AudioDeviceRole.OUTPUT,
    #     android_constants.AudioDeviceType.BLE_HEADSET,
    #     self.ref.random_address,
    # )
    # self.logger.info("[DUT] Is headtracker enabled: %s",
    # is_headtracker_enabled)
    # self.assertTrue(is_headtracker_enabled)

  async def test_connect_without_hid_service(self) -> None:
    """Tests connecting HID Head Tracker when no standard HID/HOGP service is registered on REF.

    Test steps:
      1. Setup ONLY the Head Tracker GATT service on REF.
      2. Pair and connect over LE.
      3. Verify HID_HOST connects over LE automatically.
      4. Disconnect and restart advertising to verify reconnection.
    """
    await self._connect_headtracker()
    dut_hid_cb = self.test_case_context.enter_context(
        self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    )
    ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
        hci.Address(self.dut.address)
    )
    assert ref_dut_acl is not None
    self.logger.info("[REF] Disconnect")
    await ref_dut_acl.disconnect()

    self.logger.info("[REF] Restart advertising")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM,
    )
    self.logger.info("[DUT] Wait for reconnected over LE")
    await dut_hid_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.random_address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
    )

  async def test_connect_with_hid_service(self) -> None:
    """Tests Head Tracker connection and transport preference switch from Classic to LE when both services exist.

    Test steps:
      1. Setup both Classic HID and Head Tracker GATT services on REF.
      2. Pair over LE with dual-mode key distribution and discover services.
      3. Connect from DUT and verify initial connection happens over Classic.
      4. Set preferred transport to LE.
      5. Verify transport switch (Classic disconnects, Head Tracker over LE
      connects).
    """
    self._setup_hogp_service()
    self.ref.device.add_service(hid.HeadtrackerService(self.ref.device))
    ref_classic_hid_device = hid.Device(self.ref.device, delegate=None)
    self.ref.device.sdp_service_records = {
        1: hid.make_device_sdp_record(1, hid.DEFAULT_REPORT_MAP)
    }
    condition = asyncio.Condition()

    @ref_classic_hid_device.on(hid.Device.EVENT_CONNECTION)
    @ref_classic_hid_device.on(hid.Device.EVENT_DISCONNECTION)
    async def on_ref_hid_connection_state_changed() -> None:
      async with condition:
        condition.notify_all()

    dut_hid_cb = self.test_case_context.enter_context(
        self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    )
    adapter_cb = self.test_case_context.enter_context(
        self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER)
    )

    self.logger.info("[DUT] Pair over Classic BR/EDR")
    await self.classic_connect_and_pair(self.ref, connect_profiles=False)
    self.logger.info("[DUT] Connect HID over Classic")
    self.dut.bt.connect(self.ref.address)

    self.logger.info("[DUT] Wait for HID connected over Classic")
    await dut_hid_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
    )

    initial_transport = self.dut.bt.getHidHostPreferredTransport(
        self.ref.address
    )
    self.logger.info("[DUT] Initial preferred transport: %s", initial_transport)
    self.assertEqual(
        initial_transport,
        android_constants.Transport.CLASSIC,
        msg="Initial preferred transport should be CLASSIC",
    )
    async with condition:
      await condition.wait_for(
          lambda: ref_classic_hid_device.control_channel is not None
      )

    self.logger.info("[REF] Start advertising on LE before transport switch")
    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.PUBLIC, auto_restart=True
    )
    self.logger.info(
        "[DUT] Open LE GATT client so TRANSPORT_LE handle is active and"
        " populate RemoteDevices.mUuidsLe"
    )
    gatt_client = await self.dut.bl4a.connect_gatt_client(
        self.ref.address,
        transport=android_constants.Transport.LE,
        address_type=android_constants.AddressTypeStatus.PUBLIC,
    )
    await gatt_client.get_services()
    self.dut.bt.fetchUuidsWithSdp(self.ref.address)
    discovered_uuids = set[core.UUID]()
    while not discovered_uuids.intersection({
        gatt.GATT_HUMAN_INTERFACE_DEVICE_SERVICE,
        hid.HeadtrackerService.UUID,
    }):
      uuid_changed_event = await adapter_cb.wait_for_event(
          bl4a_api.UuidChanged(address=self.ref.address, uuids=mock.ANY)
      )
      discovered_uuids.update(map(core.UUID, uuid_changed_event.uuids or []))
      self.logger.info(
          "[DUT] Discovered uuids while LE connected: %r", discovered_uuids
      )

    await gatt_client.disconnect()
    gatt_client.close()

    self.logger.info("[DUT] Set preferred transport to LE")
    self.assertTrue(
        self.dut.bt.setHidHostPreferredTransport(
            self.ref.address, android_constants.Transport.LE
        ),
        msg="Failed to set preferred transport to LE",
    )

    self.logger.info("[DUT] Wait for transport switch (DISCONNECTED Classic)")
    await dut_hid_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.DISCONNECTED,
        ),
    )
    async with condition:
      await condition.wait_for(
          lambda: ref_classic_hid_device.control_channel is None
      )

    self.logger.info("[DUT] Wait for transport switch (CONNECTED LE)")
    await dut_hid_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
    )

    updated_transport = self.dut.bt.getHidHostPreferredTransport(
        self.ref.address
    )
    self.logger.info("[DUT] Updated preferred transport: %s", updated_transport)
    self.assertEqual(
        updated_transport,
        android_constants.Transport.LE,
        msg="Updated preferred transport should be LE",
    )

  async def test_get_version_report(self) -> None:
    """Tests reading the Head Tracker version string via HID Feature Report ID 2."""
    await self._connect_headtracker()

    report_id = 2
    dut_hid_cb = self.test_case_context.enter_context(
        self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    )
    self.logger.info(
        "[DUT] Get Head Tracker version report (Feature Report ID 2)"
    )
    self.dut.bt.getHidHostReport(
        self.ref.random_address,  # address
        hid.ReportType.FEATURE_REPORT,  # report_type
        report_id,  # report_id
        0,  # buffer_size
    )

    self.logger.info("[DUT] Wait for HidHostReport event")
    event = await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostReport,
        predicate=lambda e: bool(e.report) and e.report[0] == report_id,
    )

    self.logger.info("[DUT] Verify report data")
    self.assertEqual(event.address, self.ref.random_address)
    self.assertEqual(event.report[0], report_id)
    self.assertIn(b"#AndroidHeadTracker", bytes(event.report))

  async def test_control_report(self) -> None:
    """Tests getting and setting the Head Tracker control report via HID Feature Report ID 1."""
    ht_service = await self._connect_headtracker()

    report_id = 1
    dut_hid_cb = self.test_case_context.enter_context(
        self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    )
    self.logger.info(
        "[DUT] Get Head Tracker control report (Feature Report ID 1)"
    )
    self.dut.bt.getHidHostReport(
        self.ref.random_address,  # address
        hid.ReportType.FEATURE_REPORT,  # report_type
        report_id,  # report_id
        0,  # buffer_size
    )

    self.logger.info("[DUT] Wait for initial HidHostReport event")
    event = await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostReport,
        predicate=lambda e: bool(e.report) and e.report[0] == report_id,
    )
    self.assertEqual(event.address, self.ref.random_address)
    self.assertEqual(event.report[0], report_id)

    new_report = hid.HeadtrackerReport(
        reporting_state=False,
        power_state=True,
        report_interval_ms=20,
        transport=hid.HeadtrackerReport.Transport.ACL,
    )
    report_hex = bytes([report_id] + list(bytes(new_report))).hex()

    self.logger.info(
        "[DUT] Set Head Tracker control report (Feature Report ID 1)"
    )
    self.dut.bt.setHidHostReport(
        self.ref.random_address,  # address
        hid.ReportType.FEATURE_REPORT,  # report_type
        report_hex,  # report_hex
    )

    self.logger.info("[DUT] Wait for handshake")
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostHandshake(
            address=self.ref.random_address,
            status=hid.HandshakeMessage.ResultCode.SUCCESSFUL,
        )
    )

    self.logger.info("[REF] Verify characteristic value updated on Bumble")
    self.assertEqual(ht_service.control_characteristic.value, new_report)

  async def test_sensor_data_report(self) -> None:
    """Tests receiving Head Tracker sensor data notifications via HID Input Report ID 1."""
    ht_service = await self._connect_headtracker()

    report_id = 1
    sensor_payload = bytes([0x01, 0x10, 0x20, 0x30, 0x40])
    dut_hid_cb = self.test_case_context.enter_context(
        self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    )
    self.logger.info(
        "[REF] Notify Head Tracker sensor data on report characteristic"
    )
    ht_service.report_characteristic.value = sensor_payload
    await self.ref.device.notify_subscribers(ht_service.report_characteristic)

    self.logger.info(
        "[DUT] Get sensor data report explicitly via Input Report ID 1"
    )
    self.dut.bt.getHidHostReport(
        self.ref.random_address,
        hid.ReportType.INPUT_REPORT,  # report_type
        report_id,  # report_id
        0,  # buffer_size
    )

    self.logger.info("[DUT] Wait for sensor data report (Input Report ID 1)")
    event = await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostReport,
        predicate=lambda e: (
            len(e.report) > 1
            and e.report[0] == report_id
            and bytes(e.report[1:]) == sensor_payload
        ),
    )

    self.logger.info("[DUT] Verify sensor data report")
    self.assertEqual(event.address, self.ref.random_address)
    self.assertEqual(event.report[0], report_id)
    self.assertSequenceEqual(event.report[1:], sensor_payload)


if __name__ == "__main__":
  test_runner.main()
