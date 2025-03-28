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

"""Tests for LE Audio Broadcast and Broadcast Audio Scan Service (BASS)."""

import asyncio
import dataclasses
import random
from typing import Annotated, ClassVar, cast

from bumble import core
from bumble import device
from bumble import gatt
from bumble import hci
from bumble.profiles import ascs
from bumble.profiles import bap
from bumble.profiles import bass
from bumble.profiles import le_audio
from bumble.profiles import pacs
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import hci as hci_ext
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api

_PROPERTY_BROADCAST_SOURCE_ENABLED = (
    'bluetooth.profile.bap.broadcast.source.enabled'
)
_PROPERTY_BROADCAST_ASSIST_ENABLED = (
    'bluetooth.profile.bap.broadcast.assist.enabled'
)
_PROPERTY_BYPASS_ALLOW_LIST = 'persist.bluetooth.leaudio.bypass_allow_list'
_DEFAULT_TIMEOUT_SECEONDS = 10.0
_SUBGROUP_INDEX = 0


# TODO: Remove this when Bumble command is synced to G3.
@dataclasses.dataclass(frozen=True)
class _LeSetDefaultPeriodicAdvertisingSyncTransferParametersCommand(
    hci_ext.Command
):
  """See Bluetooth spec @ 7.8.92 LE Set Default Periodic Advertising Sync Transfer Parameters command."""

  op_code: ClassVar[int] = (
      hci.HCI_LE_SET_DEFAULT_PERIODIC_ADVERTISING_SYNC_TRANSFER_PARAMETERS_COMMAND
  )

  mode: Annotated[int, 1]
  skip: Annotated[int, 2]
  sync_timeout: Annotated[int, 2]
  cte_type: Annotated[int, 1]


def _make_basic_audio_announcement(
    sampling_frequency: bap.SamplingFrequency,
    frame_duration: bap.FrameDuration,
    octets_per_codec_frame: int,
) -> bap.BasicAudioAnnouncement:
  return bap.BasicAudioAnnouncement(
      presentation_delay=40000,
      subgroups=[
          bap.BasicAudioAnnouncement.Subgroup(
              codec_id=hci.CodingFormat(codec_id=hci.CodecID.LC3),
              codec_specific_configuration=bap.CodecSpecificConfiguration(
                  sampling_frequency=sampling_frequency,
                  frame_duration=frame_duration,
                  octets_per_codec_frame=octets_per_codec_frame,
              ),
              metadata=le_audio.Metadata([
                  le_audio.Metadata.Entry(
                      tag=le_audio.Metadata.Tag.LANGUAGE, data=b'eng'
                  ),
                  le_audio.Metadata.Entry(
                      tag=le_audio.Metadata.Tag.PROGRAM_INFO, data=b'Disco'
                  ),
              ]),
              bis=[
                  bap.BasicAudioAnnouncement.BIS(
                      index=1,
                      codec_specific_configuration=bap.CodecSpecificConfiguration(
                          audio_channel_allocation=bap.AudioLocation.FRONT_LEFT
                      ),
                  ),
                  bap.BasicAudioAnnouncement.BIS(
                      index=2,
                      codec_specific_configuration=bap.CodecSpecificConfiguration(
                          audio_channel_allocation=bap.AudioLocation.FRONT_RIGHT
                      ),
                  ),
              ],
          )
      ],
  )


class _BroadcastAudioScanService(gatt.TemplateService):
  UUID = gatt.GATT_BROADCAST_AUDIO_SCAN_SERVICE

  def __init__(self) -> None:
    self.operations = asyncio.Queue[bass.ControlPointOperation]()
    self.broadcast_audio_scan_control_point_characteristic = (
        gatt.Characteristic(
            uuid=gatt.GATT_BROADCAST_AUDIO_SCAN_CONTROL_POINT_CHARACTERISTIC,
            properties=gatt.Characteristic.Properties.WRITE
            | gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
            permissions=gatt.Characteristic.WRITEABLE,
            value=gatt.CharacteristicValue(
                write=lambda _connection, value: self.operations.put_nowait(
                    bass.ControlPointOperation.from_bytes(value)
                )
            ),
        )
    )
    self.broadcast_receive_state_characteristic = gatt.Characteristic(
        uuid=gatt.GATT_BROADCAST_RECEIVE_STATE_CHARACTERISTIC,
        properties=gatt.Characteristic.Properties.READ
        | gatt.Characteristic.Properties.NOTIFY,
        permissions=gatt.Characteristic.Permissions.READABLE
        | gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION,
    )
    super().__init__([
        self.broadcast_audio_scan_control_point_characteristic,
        self.broadcast_receive_state_characteristic,
    ])


class BroadcastTest(navi_test_base.TwoDevicesTestBase):
  _broadcast_enabled: bool = False
  _bass_enabled: bool = False
  _ref_bass_service: _BroadcastAudioScanService

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if self.dut.getprop(_PROPERTY_BROADCAST_SOURCE_ENABLED) == 'true':
      self._broadcast_enabled = True
    if self.dut.getprop(_PROPERTY_BROADCAST_ASSIST_ENABLED) == 'true':
      self._bass_enabled = True

    if not self._broadcast_enabled and not self._bass_enabled:
      raise signals.TestAbortClass('Broadcast source and BASS are not enabled.')

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      await self.ref.open()

    if not self.ref.device.supports_le_features(
        hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_RECIPIENT
        | hci.LeFeatureMask.PERIODIC_ADVERTISING_SYNC_TRANSFER_SENDER
        | hci.LeFeatureMask.LE_PERIODIC_ADVERTISING
        | hci.LeFeatureMask.ISOCHRONOUS_BROADCASTER
    ):
      raise signals.TestAbortClass('REF does not support LE features.')

    self.ref.config.cis_enabled = True
    # Disable the allow list to allow the connect LE Audio to Bumble.
    self.dut.setprop(_PROPERTY_BYPASS_ALLOW_LIST, 'true')

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    self._ref_bass_service = _BroadcastAudioScanService()

  @override
  async def async_teardown_test(self) -> None:
    await super().async_teardown_test()
    self.dut.bt.audioStop()

  async def _receive_broadcast_on_ref(
      self,
      broadcast_id: int,
      subgroup_index: int,
      broadcast_code: bytes | None = None,
  ) -> tuple[device.BigSync, bap.BasicAudioAnnouncement]:
    broadcast_advertisements = asyncio.Queue[device.Advertisement]()

    def on_advertisement(advertisement: device.Advertisement) -> None:
      if (
          (
              ads := advertisement.data.get_all(
                  core.AdvertisingData.SERVICE_DATA_16_BIT_UUID
              )
          )
          and (
              broadcast_audio_announcement := next(
                  (
                      ad[1]
                      for ad in ads
                      if isinstance(ad, tuple)
                      and ad[0]
                      == gatt.GATT_BROADCAST_AUDIO_ANNOUNCEMENT_SERVICE
                  ),
                  None,
              )
          )
          and (
              bap.BroadcastAudioAnnouncement.from_bytes(
                  broadcast_audio_announcement
              ).broadcast_id
              == broadcast_id
          )
      ):
        broadcast_advertisements.put_nowait(advertisement)

    self.ref.device.on('advertisement', on_advertisement)

    self.logger.info('[REF] Start scanning')
    await self.ref.device.start_scanning()

    self.logger.info('[REF] Wait for broadcast advertisement')
    broadcast_advertisement = await broadcast_advertisements.get()

    self.logger.info('[REF] Create periodic advertising sync')
    pa_sync = await self.ref.device.create_periodic_advertising_sync(
        advertiser_address=broadcast_advertisement.address,
        sid=broadcast_advertisement.sid,
    )

    basic_audio_announcements = asyncio.Queue[bap.BasicAudioAnnouncement]()
    big_info_advertisements = asyncio.Queue[device.BIGInfoAdvertisement]()

    def on_periodic_advertisement(
        advertisement: device.PeriodicAdvertisement,
    ) -> None:
      if (advertising_data := advertisement.data) is None:  # type: ignore[attribute-error]
        return

      for service_data in advertising_data.get_all(
          core.AdvertisingData.SERVICE_DATA
      ):
        service_uuid, data = cast(tuple[core.UUID, bytes], service_data)

        if service_uuid == gatt.GATT_BASIC_AUDIO_ANNOUNCEMENT_SERVICE:
          basic_audio_announcements.put_nowait(
              bap.BasicAudioAnnouncement.from_bytes(data)
          )
          break

    def on_biginfo_advertisement(
        advertisement: device.BIGInfoAdvertisement,
    ) -> None:
      big_info_advertisements.put_nowait(advertisement)

    pa_sync.on('periodic_advertisement', on_periodic_advertisement)
    pa_sync.on('biginfo_advertisement', on_biginfo_advertisement)

    self.logger.info('[REF] Wait for basic audio announcement')
    basic_audio_announcement = await basic_audio_announcements.get()
    subgroup = basic_audio_announcement.subgroups[subgroup_index]

    self.logger.info('[REF] Wait for BIG info advertisement')
    await big_info_advertisements.get()

    self.logger.info('[REF] Stop scanning')
    await self.ref.device.stop_scanning()

    self.logger.info('[REF] Sync with BIG')
    big_sync = await self.ref.device.create_big_sync(
        pa_sync,
        device.BigSyncParameters(
            big_sync_timeout=0x4000,
            bis=[bis.index for bis in subgroup.bis],
            broadcast_code=broadcast_code,
        ),
    )
    return big_sync, basic_audio_announcement

  async def _start_broadcast_on_ref(
      self,
      broadcast_id: int,
      broadcast_name: str,
      broadcast_code: bytes | None,
      sampling_frequency: bap.SamplingFrequency,
      frame_duration: bap.FrameDuration,
      octets_per_codec_frame: int,
  ) -> tuple[device.AdvertisingSet, device.Big]:
    broadcast_audio_announcement = bap.BroadcastAudioAnnouncement(broadcast_id)

    self.logger.info('[REF] Start Advertising')
    advertising_set = await self.ref.device.create_advertising_set(
        advertising_parameters=device.AdvertisingParameters(
            advertising_event_properties=device.AdvertisingEventProperties(
                is_connectable=False
            ),
            primary_advertising_interval_min=100,
            primary_advertising_interval_max=200,
        ),
        advertising_data=(
            broadcast_audio_announcement.get_advertising_data()
            + bytes(
                core.AdvertisingData([(
                    core.AdvertisingData.BROADCAST_NAME,
                    broadcast_name.encode('utf-8'),
                )])
            )
        ),
        periodic_advertising_parameters=device.PeriodicAdvertisingParameters(
            periodic_advertising_interval_min=80,
            periodic_advertising_interval_max=160,
        ),
        periodic_advertising_data=_make_basic_audio_announcement(
            sampling_frequency=sampling_frequency,
            frame_duration=frame_duration,
            octets_per_codec_frame=octets_per_codec_frame,
        ).get_advertising_data(),
        auto_restart=True,
        auto_start=True,
    )

    self.logger.info('[REF] Start Periodic Advertising')
    await advertising_set.start_periodic()

    self.logger.info('[REF] Create BIG')
    big = await self.ref.device.create_big(
        advertising_set,
        parameters=device.BigParameters(
            num_bis=2,
            sdu_interval=10000,
            max_sdu=100,
            max_transport_latency=65,
            rtn=4,
            broadcast_code=broadcast_code,
        ),
    )

    return advertising_set, big

  async def _prepare_paired_devices(self) -> None:
    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO) as dut_lea_cb,
        self.dut.bl4a.register_callback(bl4a_api.Module.BASS) as dut_bass_cb,
    ):
      # TODO: Currently, there must be another audio device to
      # enable broadcast, so we add a PACS and ASCS to make the Bumble device
      # look like an LEHS.
      self.ref.device.add_service(
          pacs.PublishedAudioCapabilitiesService(
              supported_source_context=bap.ContextType(0x0000),
              available_source_context=bap.ContextType(0x0000),
              supported_sink_context=bap.ContextType(0xFFFF),
              available_sink_context=bap.ContextType(0xFFFF),
              sink_audio_locations=(
                  bap.AudioLocation.FRONT_LEFT | bap.AudioLocation.FRONT_RIGHT
              ),
              sink_pac=[
                  pacs.PacRecord(
                      coding_format=hci.CodingFormat(hci.CodecID.LC3),
                      codec_specific_capabilities=bap.CodecSpecificCapabilities(
                          supported_sampling_frequencies=(
                              bap.SupportedSamplingFrequency.FREQ_16000
                              | bap.SupportedSamplingFrequency.FREQ_32000
                              | bap.SupportedSamplingFrequency.FREQ_48000
                          ),
                          supported_frame_durations=(
                              bap.SupportedFrameDuration.DURATION_10000_US_SUPPORTED
                          ),
                          supported_audio_channel_count=[1, 2],
                          min_octets_per_codec_frame=26,
                          max_octets_per_codec_frame=240,
                          supported_max_codec_frames_per_sdu=2,
                      ),
                  )
              ],
          )
      )
      self.ref.device.add_service(
          ascs.AudioStreamControlService(self.ref.device, sink_ase_id=[1])
      )
      self.ref.device.add_service(self._ref_bass_service)
      await self.le_connect_and_pair(hci.OwnAddressType.RANDOM)
      self.logger.info('[DUT] Wait for LE Audio connected')
      await dut_lea_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: e.address == self.ref.random_address
          and e.state == android_constants.ConnectionState.CONNECTED,
      )
      self.logger.info('[DUT] Wait for BASS connected')
      await dut_bass_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: e.address == self.ref.random_address
          and e.state == android_constants.ConnectionState.CONNECTED,
      )

  @navi_test_base.named_parameterized(
      standard_quality=dict(
          quality=android_constants.LeAudioBroadcastQuality.STANDARD,
          broadcast_code=None,
          expected_sampling_frequency=bap.SamplingFrequency.FREQ_24000,
      ),
      high_quality=dict(
          quality=android_constants.LeAudioBroadcastQuality.HIGH,
          broadcast_code=None,
          expected_sampling_frequency=bap.SamplingFrequency.FREQ_48000,
      ),
      high_quality_encrypted=dict(
          quality=android_constants.LeAudioBroadcastQuality.HIGH,
          broadcast_code=b'1234567890abcdef',
          expected_sampling_frequency=bap.SamplingFrequency.FREQ_48000,
      ),
  )
  async def test_broadcast_start_stop(
      self,
      quality: android_constants.LeAudioBroadcastQuality,
      broadcast_code: bytes | None,
      expected_sampling_frequency: bap.SamplingFrequency,
  ) -> None:
    """Tests broadcasting on DUT, and receiving on REF.

    Test steps:
      1. Start broadcasting on DUT.
      2. Wait for broadcast advertisement on REF.
      3. Create periodic advertising sync on REF.
      4. Wait for basic audio announcement on REF.
      5. Wait for BIG info advertisement on REF.
      6. Create BIG sync on REF.
      7. Stop broadcasting on DUT.
      8. Wait for BIG sync lost on REF.

    Args:
      quality: The quality of the broadcast.
      broadcast_code: The broadcast code of the broadcast.
      expected_sampling_frequency: The expected sampling frequency of the
        broadcast.
    """
    if not self._broadcast_enabled:
      self.skipTest('Broadcast source is not enabled.')

    # TODO: LEHS must be connected before starting broadcast.
    await self._prepare_paired_devices()

    self.logger.info('[DUT] Start broadcasting')
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      broadcast = await self.dut.bl4a.start_le_audio_broadcast(
          broadcast_code=broadcast_code,
          subgroups=[
              bl4a_api.LeAudioBroadcastSubgroupSettings(quality=quality)
          ],
      )

    self.logger.info('[DUT] Broadcast created, ID: %d', broadcast.broadcast_id)

    # Since 25Q1, broadcast will be started only during audio playback.
    self.logger.info('[DUT] Start audio playback')
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ALL)
    self.dut.bt.audioPlaySine()

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      big_sync, basic_audio_announcement = await self._receive_broadcast_on_ref(
          broadcast_id=broadcast.broadcast_id,
          subgroup_index=_SUBGROUP_INDEX,
          broadcast_code=broadcast_code,
      )

    self.assertEqual(
        basic_audio_announcement.subgroups[
            _SUBGROUP_INDEX
        ].codec_specific_configuration.sampling_frequency,
        expected_sampling_frequency,
    )

    sync_lost = asyncio.Event()
    big_sync.once(big_sync.Event.TERMINATION, lambda _: sync_lost.set())

    self.logger.info('[DUT] Stop broadcasting')
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      await broadcast.stop()

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      self.logger.info('[REF] Wait for sync lost')
      await sync_lost.wait()

  @navi_test_base.named_parameterized(
      dict(
          testcase_name='unencrypted',
          broadcast_code=None,
      ),
      dict(
          testcase_name='encrypted',
          broadcast_code=b'1234567890abcdef',
      ),
  )
  async def test_broadcast_assist_search(
      self, broadcast_code: bytes | None
  ) -> None:
    """Tests broadcasting on REF, and search from DUT.

    Test steps:
      1. Start broadcasting on REF.
      2. Search for broadcast on DUT.
      3. Verify the broadcast source found event.

    Args:
      broadcast_code: The broadcast code of the broadcast.
    """
    if not self._bass_enabled:
      self.skipTest('BASS is not enabled.')

    broadcast_id = random.randint(0, 0xFFFFFF)
    broadcast_name = 'Bumble Auracast'
    self.logger.info('[REF] Broadcast ID: %d', broadcast_id)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      await self._start_broadcast_on_ref(
          broadcast_id=broadcast_id,
          broadcast_name=broadcast_name,
          broadcast_code=broadcast_code,
          sampling_frequency=bap.SamplingFrequency.FREQ_48000,
          frame_duration=bap.FrameDuration.DURATION_10000_US,
          octets_per_codec_frame=100,
      )

    with self.dut.bl4a.register_callback(bl4a_api.Module.BASS) as bass_callback:
      self.dut.bt.bassStartSearching()
      self.logger.info('[DUT] Wait for broadcast source found')
      source_found_event = await bass_callback.wait_for_event(
          bl4a_api.BroadcastSourceFound,
          lambda e: (
              cast(bl4a_api.BroadcastSourceFound, e).source.broadcast_id
              == broadcast_id
          ),
      )
      self.dut.bt.bassStopSearching()
      self.assertEqual(source_found_event.source.num_subgroups, 1)
      self.assertEqual(source_found_event.source.sg_number_of_bises[0], 2)
      self.assertEqual(source_found_event.source.broadcast_name, broadcast_name)

  @navi_test_base.named_parameterized(
      dict(
          testcase_name='unencrypted',
          broadcast_code=None,
      ),
      dict(
          testcase_name='encrypted',
          broadcast_code=b'1234567890abcdef',
      ),
  )
  async def test_assistant_add_local_source(
      self, broadcast_code: bytes | None
  ) -> None:
    """Tests adding DUT's broadcast source over BASS from DUT.

    Test steps:
      1. Start broadcasting on DUT.
      2. Add DUT's broadcast source over BASS from DUT.
      3. Wait for add source operation received on REF.
      4. Verify the add source operation on REF.
      5. Wait for sync info on REF.
      6. Wait for BIG info advertisement on REF.
      7. Wait for set broadcast code operation received on REF.
      8. Wait for add source operation complete on REF.

    Args:
      broadcast_code: The broadcast code of the broadcast.
    """
    if not self._bass_enabled:
      self.skipTest('BASS is not enabled.')
    if not self._broadcast_enabled:
      self.skipTest('Broadcast source is not enabled.')

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      self.logger.info(
          '[REF] Set default periodic advertising sync transfer parameters'
      )
      await self.ref.device.send_command(
          _LeSetDefaultPeriodicAdvertisingSyncTransferParametersCommand(
              mode=0x03, skip=0x00, sync_timeout=0x4000, cte_type=0x00
          ),
          check_result=True,
      )

    await self._prepare_paired_devices()

    self.logger.info('[DUT] Start broadcasting')
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      broadcast = await self.dut.bl4a.start_le_audio_broadcast(
          broadcast_code=broadcast_code,
          subgroups=[
              bl4a_api.LeAudioBroadcastSubgroupSettings(
                  quality=android_constants.LeAudioBroadcastQuality.HIGH
              )
          ],
      )

    self.logger.info('[DUT] Broadcast created, ID: %d', broadcast.broadcast_id)

    # Since 25Q1, broadcast will be started only during audio playback.
    self.logger.info('[DUT] Start audio playback')
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ALL)
    self.dut.bt.audioPlaySine()

    self.logger.info('[DUT] Add broadcast source')
    broadcast_metadata = self.dut.bt.getAllBroadcastMetadata()[0]
    add_source_task = asyncio.create_task(
        asyncio.to_thread(
            lambda: self.dut.bt.bassAddSource(
                self.ref.random_address, broadcast_metadata
            ),
        )
    )

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      self.logger.info('[REF] Wait for add source operation')
      operation = await self._ref_bass_service.operations.get()

    if not isinstance(operation, bass.AddSourceOperation):
      self.fail(f'Expected AddSourceOperation, got {operation}')
    self.assertEqual(operation.broadcast_id, broadcast.broadcast_id)
    self.assertEqual(
        operation.pa_sync,
        bass.PeriodicAdvertisingSyncParams.SYNCHRONIZE_TO_PA_PAST_AVAILABLE,
    )

    # Configure PAST handler.
    past_events = asyncio.Queue[
        hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_Event
    ]()

    # TODO: Remove this when handler is synced to G3.
    def on_past_event(
        event: hci.HCI_LE_Periodic_Advertising_Sync_Transfer_Received_Event,
    ) -> None:
      past_events.put_nowait(event)
      pa_sync = device.PeriodicAdvertisingSync(
          self.ref.device,
          advertiser_address=event.advertiser_address,
          sid=event.advertising_sid,
          skip=0,
          sync_timeout=0.0,
          filter_duplicates=False,
      )
      pa_sync.on_establishment(
          status=event.status,
          sync_handle=event.sync_handle,
          advertiser_phy=event.advertiser_phy,
          periodic_advertising_interval=event.periodic_advertising_interval,
          advertiser_clock_accuracy=event.advertiser_clock_accuracy,
      )
      self.ref.device.periodic_advertising_syncs.append(pa_sync)

    setattr(
        self.ref.device.host,
        'on_hci_le_periodic_advertising_sync_transfer_received_event',
        on_past_event,
    )

    receiver_state = bass.BroadcastReceiveState(
        source_id=0,
        source_address=operation.advertiser_address,
        source_adv_sid=operation.advertising_sid,
        broadcast_id=operation.broadcast_id,
        pa_sync_state=bass.BroadcastReceiveState.PeriodicAdvertisingSyncState.SYNCINFO_REQUEST,
        big_encryption=bass.BroadcastReceiveState.BigEncryption.NOT_ENCRYPTED,
        bad_code=b'',
        subgroups=operation.subgroups,
    )
    self._ref_bass_service.broadcast_receive_state_characteristic.value = bytes(
        receiver_state
    )
    self.logger.info('[REF] Update broadcast receive state')
    await self.ref.device.notify_subscribers(
        self._ref_bass_service.broadcast_receive_state_characteristic
    )
    self.logger.info('[REF] Wait for sync info')
    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      while past_event := await past_events.get():
        if (
            past_event.advertising_sid == operation.advertising_sid
            and past_event.advertiser_address == operation.advertiser_address
        ):
          self.logger.info('[REF] Sync info received')
          break

    pa_sync = self.ref.device.periodic_advertising_syncs[0]
    biginfo_advertisements = asyncio.Queue[device.BIGInfoAdvertisement]()
    pa_sync.on('biginfo_advertisement', biginfo_advertisements.put_nowait)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      self.logger.info('[REF] Wait for BIG info advertisement')
      biginfo_advertisement = await biginfo_advertisements.get()

    encryped = bool(broadcast_code)
    self.assertEqual(biginfo_advertisement.encrypted, encryped)

    receiver_state.pa_sync_state = (
        bass.BroadcastReceiveState.PeriodicAdvertisingSyncState.SYNCHRONIZED_TO_PA
    )
    receiver_state.big_encryption = (
        bass.BroadcastReceiveState.BigEncryption.BROADCAST_CODE_REQUIRED
        if encryped
        else bass.BroadcastReceiveState.BigEncryption.NOT_ENCRYPTED
    )
    self._ref_bass_service.broadcast_receive_state_characteristic.value = bytes(
        receiver_state
    )

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      self.logger.info('[REF] Update broadcast receive state')
      await self.ref.device.notify_subscribers(
          self._ref_bass_service.broadcast_receive_state_characteristic
      )

    if encryped:
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
        self.logger.info('[REF] Wait for set broadcast code operation')
        operation = await self._ref_bass_service.operations.get()

      if not isinstance(operation, bass.SetBroadcastCodeOperation):
        self.fail(f'Expected SetBroadcastCodeOperation, got {operation}')
      self.assertEqual(operation.broadcast_code, broadcast_code)

      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
        self.logger.info('[REF] Update broadcast receive state')
        receiver_state.pa_sync_state = (
            bass.BroadcastReceiveState.BigEncryption.DECRYPTING
        )
        self._ref_bass_service.broadcast_receive_state_characteristic.value = (
            bytes(receiver_state)
        )
        await self.ref.device.notify_subscribers(
            self._ref_bass_service.broadcast_receive_state_characteristic
        )

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      self.logger.info('[DUT] Wait for add source operation complete')
      await add_source_task

  @navi_test_base.named_parameterized(
      dict(
          testcase_name='synced',
          pa_sync_state=bass.BroadcastReceiveState.PeriodicAdvertisingSyncState.SYNCHRONIZED_TO_PA,
      ),
      dict(
          testcase_name='unsynced',
          pa_sync_state=bass.BroadcastReceiveState.PeriodicAdvertisingSyncState.NOT_SYNCHRONIZED_TO_PA,
      ),
  )
  async def test_assistant_remove_source(
      self,
      pa_sync_state: bass.BroadcastReceiveState.PeriodicAdvertisingSyncState,
  ) -> None:
    if not self._bass_enabled:
      self.skipTest('BASS is not enabled.')

    # Pretend the broadcast source is already added.
    receiver_state = bass.BroadcastReceiveState(
        source_id=0,
        source_address=hci.Address('00:11:22:33:44:55'),
        source_adv_sid=0,
        broadcast_id=0x123456,
        pa_sync_state=pa_sync_state,
        big_encryption=bass.BroadcastReceiveState.BigEncryption.NOT_ENCRYPTED,
        bad_code=b'',
        subgroups=[],
    )
    broadcast_receive_state_characteristic = (
        self._ref_bass_service.broadcast_receive_state_characteristic
    )
    broadcast_receive_state_characteristic.value = bytes(receiver_state)
    await self._prepare_paired_devices()

    self.logger.info('[DUT] Remove broadcast source')
    remove_source_task = asyncio.create_task(
        asyncio.to_thread(
            lambda: self.dut.bt.bassRemoveSource(self.ref.random_address, 0)
        )
    )

    if (
        pa_sync_state
        == bass.BroadcastReceiveState.PeriodicAdvertisingSyncState.SYNCHRONIZED_TO_PA
    ):
      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
        self.logger.info('[REF] Wait for modify source operation')
        operation = await self._ref_bass_service.operations.get()
      if not isinstance(operation, bass.ModifySourceOperation):
        self.fail(f'Expected ModifySourceOperation, got {operation}')
      self.assertEqual(operation.source_id, 0)
      self.assertEqual(
          operation.pa_sync,
          bass.PeriodicAdvertisingSyncParams.DO_NOT_SYNCHRONIZE_TO_PA,
      )

      async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
        self.logger.info('[REF] Update broadcast receive state')
        receiver_state.pa_sync_state = (
            bass.BroadcastReceiveState.PeriodicAdvertisingSyncState.NOT_SYNCHRONIZED_TO_PA
        )
        broadcast_receive_state_characteristic.value = bytes(receiver_state)
        await self.ref.device.notify_subscribers(
            broadcast_receive_state_characteristic
        )

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      self.logger.info('[REF] Wait for remove source operation')
      operation = await self._ref_bass_service.operations.get()
    if not isinstance(operation, bass.RemoveSourceOperation):
      self.fail(f'Expected RemoveSourceOperation, got {operation}')
    self.assertEqual(operation.source_id, 0)

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      self.logger.info('[REF] Update broadcast receive state')
      broadcast_receive_state_characteristic.value = b''
      await self.ref.device.notify_subscribers(
          broadcast_receive_state_characteristic
      )

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      self.logger.info('[DUT] Wait for remove source operation complete')
      await remove_source_task


if __name__ == '__main__':
  test_runner.main()
