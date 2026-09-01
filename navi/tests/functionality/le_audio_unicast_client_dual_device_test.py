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
import decimal
import functools
import secrets
import struct
from typing import TypeVar, cast
from unittest import mock

from bumble import core
from bumble import data_types
from bumble import device
from bumble import gatt
from bumble import gatt_client
from bumble import hci
from bumble import pairing
from bumble.profiles import bap
from bumble.profiles import cap
from bumble.profiles import csip
from bumble.profiles import le_audio
from bumble.profiles import mcp
from bumble.profiles import vcs
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import ascs
from navi.bumble_ext import ccp
from navi.bumble_ext import gatt_helper
from navi.bumble_ext import pacs
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import pyee_extensions
from navi.utils import retry

_TERMINATION_BOND_STATES = (
    android_constants.BondState.NONE,
    android_constants.BondState.BONDED,
)
_DEFAUILT_ADVERTISING_PARAMETERS = device.AdvertisingParameters(
    own_address_type=hci.OwnAddressType.RANDOM,
    primary_advertising_interval_min=100,
    primary_advertising_interval_max=100,
)
_DEFAULT_STEP_TIMEOUT_SECONDS = 20.0
_PREPARE_TIME_SECONDS = 0.5
_STREAMING_TIME_SECONDS = 1.0
_CALLER_NAME = "Pixel Bluetooth"
_CALLER_NUMBER = "123456789"
_RECORDING_PATH = "/storage/self/primary/Recordings/record.wav"
_GENERAL_DISCOVERABLE_AD_FLAGS = data_types.Flags(
    core.AdvertisingData.Flags.LE_GENERAL_DISCOVERABLE_MODE
)

_ConnectionState = android_constants.ConnectionState
_Direction = constants.Direction
_TestRole = constants.TestRole
_StreamType = android_constants.StreamType
_CallState = android_constants.CallState
_McpOpcode = mcp.MediaControlPointOpcode
_AndroidProperty = android_constants.Property


_SERVICE = TypeVar("_SERVICE", bound=gatt.Service)


def _get_service_from_device(
    bumble_device: device.Device, service_type: type[_SERVICE]
) -> _SERVICE:
  return next(
      service
      for service in bumble_device.gatt_server.services
      if isinstance(service, service_type)
  )


def _get_audio_context_entry(
    ase: ascs.AudioStreamEndpointCharacteristic,
) -> le_audio.Metadata.Entry | None:
  return next(
      (
          entry
          for entry in ase.metadata.entries
          if entry.tag == le_audio.Metadata.Tag.STREAMING_AUDIO_CONTEXTS
      ),
      None,
  )


# The original implementation doesn't handle the case when notifications are
# sent to all clients, we need to override the write_control_point method to
# wait for the response.
class _GenericMediaControlServiceProxy(mcp.GenericMediaControlServiceProxy):
  _media_control_point_result: asyncio.futures.Future[bytes] | None = None

  @override
  async def write_control_point(
      self, opcode: mcp.MediaControlPointOpcode
  ) -> mcp.MediaControlPointResultCode:
    if not self.media_control_point:
      raise core.InvalidOperationError("Peer does not have media control point")

    async with self.lock:
      self._media_control_point_result = (
          asyncio.get_running_loop().create_future()
      )
      await self.media_control_point.write_value(
          bytes([opcode]),
          with_response=False,
      )

      response_opcode, response_code = await self._media_control_point_result
      if response_opcode != opcode:
        raise core.InvalidStateError(
            f"Expected {opcode} notification, but get {response_opcode}"
        )
      return mcp.MediaControlPointResultCode(response_code)

  @override
  def _on_media_control_point(self, data: bytes) -> None:
    if (
        self._media_control_point_result
        and not self._media_control_point_result.done()
    ):
      self._media_control_point_result.set_result(data)


class LeAudioUnicastClientDualDeviceTest(navi_test_base.MultiDevicesTestBase):
  """Tests for LE Audio Unicast client functionality, where the remote device set contains two individual devices.

  When running this test, please make sure the ref device supports CIS
  Peripheral.

  Supported devices are:
  - Pixel 8 and later
  - Pixel 8a and later
  - Pixel Watch 3 and later

  Unsupported devices are:
  - Pixel 7 and earlier
  - Pixel 7a and earlier
  - Pixel Watch 1, 2, Fitbit Ace LTE (P11)
  """

  first_bond_timestamp: datetime.datetime | None = None
  dut_vcp_enabled: bool = False
  dut_mcp_enabled: bool = False
  dut_ccp_enabled: bool = False

  TEST_TIMEOUT_SECONDS = 180.0

  def _setup_unicast_server(
      self,
      ref: device.Device,
      audio_location: bap.AudioLocation,
      sirk: bytes,
      sirk_type: csip.SirkType,
  ) -> None:
    ref.add_services([
        pacs.make_pacs(audio_location),
        ascs.AudioStreamControlService(
            ref,
            sink_ase_id=[1],
            source_ase_id=[2],
        ),
        cap.CommonAudioServiceService(
            csip.CoordinatedSetIdentificationService(
                set_identity_resolving_key=sirk,
                set_identity_resolving_key_type=sirk_type,
                coordinated_set_size=2,
            )
        ),
        vcs.VolumeControlService(),
    ])

  @retry.retry_on_exception()
  async def _pair_major_device(self) -> None:
    ref_address = self.refs[0].random_address
    with (
        self.dut.bl4a.register_callback(
            bl4a_api.Module.ADAPTER
        ) as dut_adapter_cb,
        self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO) as dut_lea_cb,
    ):
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.logger.info("[REF|%s] Start advertising", ref_address)
        csis = _get_service_from_device(
            self.refs[0].device, csip.CoordinatedSetIdentificationService
        )
        await self.refs[0].device.create_advertising_set(
            advertising_parameters=_DEFAUILT_ADVERTISING_PARAMETERS,
            advertising_data=bytes(
                bap.UnicastServerAdvertisingData(
                    announcement_type=bap.AnnouncementType.GENERAL
                )
            )
            + csis.get_advertising_data(),
        )

      self.logger.info("[DUT] Create bond with %s", ref_address)
      self.dut.bt.createBond(
          ref_address,
          android_constants.Transport.LE,
          android_constants.AddressTypeStatus.RANDOM,
      )
      self.logger.info("[DUT] Wait for pairing request")
      await dut_adapter_cb.wait_for_event(
          bl4a_api.PairingRequest,
          lambda e: e.address == ref_address,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )
      self.logger.info("[DUT] Accept pairing request")
      self.assertTrue(self.dut.bt.setPairingConfirmation(ref_address, True))
      self.logger.info("[DUT] Wait for bond state change")
      event = await dut_adapter_cb.wait_for_event(
          bl4a_api.BondStateChanged,
          lambda e: e.address == ref_address
          and e.state in _TERMINATION_BOND_STATES,
      )
      self.assertEqual(event.state, android_constants.BondState.BONDED)
      self.first_bond_timestamp = datetime.datetime.now()

      self.logger.info("[DUT] Wait for UUID Change")
      await dut_adapter_cb.wait_for_event(
          bl4a_api.UuidChanged(address=ref_address, uuids=mock.ANY)
      )
      self.dut.bt.connect(ref_address)

      self.logger.info("[DUT] Wait for LE Audio connected")
      await dut_lea_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=ref_address, state=_ConnectionState.CONNECTED
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

  @retry.retry_on_exception()
  async def _pair_minor_device(self) -> None:
    if not self.first_bond_timestamp:
      self.fail("Major device has not been paired")

    ref_address = self.refs[1].random_address
    with (
        self.dut.bl4a.register_callback(
            bl4a_api.Module.ADAPTER
        ) as dut_adapter_cb,
        self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO) as dut_lea_cb,
    ):
      csis = _get_service_from_device(
          self.refs[1].device, csip.CoordinatedSetIdentificationService
      )
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.logger.info("[REF|%s] Start advertising", ref_address)
        await self.refs[1].device.create_advertising_set(
            advertising_parameters=_DEFAUILT_ADVERTISING_PARAMETERS,
            advertising_data=bytes(
                core.AdvertisingData([
                    (
                        core.AdvertisingData.COMPLETE_LOCAL_NAME,
                        bytes("Bumble Right", "utf-8"),
                    ),
                    (
                        core.AdvertisingData.FLAGS,
                        bytes([
                            core.AdvertisingData.LE_GENERAL_DISCOVERABLE_MODE_FLAG
                        ]),
                    ),
                    (
                        core.AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
                        bytes(csip.CoordinatedSetIdentificationService.UUID),
                    ),
                ])
            )
            + bytes(
                bap.UnicastServerAdvertisingData(
                    announcement_type=bap.AnnouncementType.GENERAL,
                    available_audio_contexts=bap.ContextType(0xFFFF),
                )
            )
            + csis.get_advertising_data(),
        )

      # When CSIS set member is discovered, Settings should automatically pair
      # to it and accept the pairing request.
      # However, Settings may not start discovery automatically, so here we need
      # to discover the CSIS set member manually.
      self.logger.info("[DUT] Start discovery")
      self.dut.bt.startInquiry()
      self.logger.info("[DUT] Wait for 2nd pairing request")
      await dut_adapter_cb.wait_for_event(
          bl4a_api.PairingRequest,
          lambda e: e.address == ref_address,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )
      if (
          elapsed_time := (datetime.datetime.now() - self.first_bond_timestamp)
      ) > datetime.timedelta(seconds=10):
        self.logger.info(
            "Pairing request takes %.2fs > 10s, need to manually accept",
            elapsed_time.total_seconds(),
        )
        self.dut.bt.setPairingConfirmation(ref_address, True)

      self.logger.info("[DUT] Wait for 2nd REF to be bonded")
      event = await dut_adapter_cb.wait_for_event(
          bl4a_api.BondStateChanged,
          lambda e: e.address == ref_address
          and e.state in _TERMINATION_BOND_STATES,
      )
      self.assertEqual(event.state, android_constants.BondState.BONDED)

      self.logger.info("[DUT] Wait for UUID Change")
      await dut_adapter_cb.wait_for_event(
          bl4a_api.UuidChanged(address=ref_address, uuids=mock.ANY)
      )
      self.dut.bt.connect(ref_address)

      self.logger.info("[DUT] Wait for 2nd REF to be connected")
      await dut_lea_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=ref_address, state=_ConnectionState.CONNECTED
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

  _PROXY = TypeVar("_PROXY", bound=gatt_client.ProfileServiceProxy)

  async def _make_service_client(
      self, ref: device.Device, proxy_class: type[_PROXY]
  ) -> _PROXY:
    self.logger.info("[REF] Connect %s", proxy_class.__name__)
    ref_dut_acl = ref.find_connection_by_bd_addr(
        hci.Address(self.dut.address), transport=core.BT_LE_TRANSPORT
    )
    if not ref_dut_acl:
      self.fail("No ACL connection found")
    async with device.Peer(ref_dut_acl) as peer:
      client = peer.create_service_proxy(proxy_class)
      if not client:
        self.fail("Failed to connect %s", proxy_class.__name__)
      return client

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if not self.dut.is_le_audio_supported:
      raise signals.TestAbortClass("[DUT] Device does not support LE Audio.")

    self.dut_vcp_enabled = (
        self.dut.getprop(_AndroidProperty.VCP_CONTROLLER_ENABLED) == "true"
    )
    self.dut_mcp_enabled = (
        self.dut.getprop(_AndroidProperty.MCP_SERVER_ENABLED) == "true"
    )
    self.dut_ccp_enabled = (
        self.dut.getprop(_AndroidProperty.CCP_SERVER_ENABLED) == "true"
    )
    for ref in self.refs:
      ref.config.cis_enabled = True
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await ref.open()
        if not ref.device.supports_le_features(
            hci.LeFeatureMask.CONNECTED_ISOCHRONOUS_STREAM_PERIPHERAL
        ):
          raise signals.TestAbortClass("REF does not support CIS peripheral")

    self.setprop_for_class_context(
        _AndroidProperty.LEAUDIO_BYPASS_ALLOW_LIST, "true"
    )

    # Always repeat audio to avoid audio stopping.
    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    sirk = secrets.token_bytes(csip.SET_IDENTITY_RESOLVING_KEY_LENGTH)
    for ref, audio_location in zip(
        self.refs, (bap.AudioLocation.FRONT_LEFT, bap.AudioLocation.FRONT_RIGHT)
    ):
      self._setup_unicast_server(
          ref=ref.device,
          audio_location=audio_location,
          sirk=sirk,
          sirk_type=csip.SirkType.ENCRYPTED,
      )
      # Override pairing config factory to set identity address type and
      # io capability.
      ref.device.pairing_config_factory = lambda _: pairing.PairingConfig(
          identity_address_type=pairing.PairingConfig.AddressType.RANDOM,
          delegate=pairing.PairingDelegate(),
      )
    self.first_bond_timestamp = None

  @override
  async def async_teardown_test(self) -> None:
    await super().async_teardown_test()
    # Make sure audio is stopped before starting the test.
    await asyncio.to_thread(self.dut.bt.audioStop)

  @navi_test_base.named_parameterized(
      plaintext_sirk=dict(sirk_type=csip.SirkType.PLAINTEXT),
      encrypted_sirk=dict(sirk_type=csip.SirkType.ENCRYPTED),
  )
  async def test_pair_and_connect(self, sirk_type: csip.SirkType) -> None:
    """Tests pairing and connecting to Unicast servers in a CSIP set.

    Test steps:
      1. Override the SIRK type for all refs.
      2. Pair and connect the major device.
      3. Pair and connect the minor device.
      4. Check if both devices are connected and active.

    Args:
      sirk_type: The SIRK type to use.
    """
    # Override the SIRK type for all refs.
    for ref in self.refs:
      csis = _get_service_from_device(
          ref.device, csip.CoordinatedSetIdentificationService
      )
      csis.set_identity_resolving_key_type = sirk_type

    # Pair and connect devices.
    await self._pair_major_device()
    await self._pair_minor_device()

    # Check if both devices are connected and active.
    self.assertCountEqual(
        self.dut.bt.getActiveDevices(android_constants.Profile.LE_AUDIO),
        [self.refs[0].random_address, self.refs[1].random_address],
    )

  @navi_test_base.named_parameterized(
      ("active", True),
      ("passive", False),
  )
  async def test_reconnect(self, is_active: bool) -> None:
    """Tests to reconnect the LE Audio Unicast server.

    Args:
      is_active: True if reconnect is actively initialized by DUT, otherwise TA
        will be used to perform the reconnection passively.
    """
    if self.dut.device.is_emulator and not is_active:
      self.skipTest("Rootcanal doesn't support APCF.")

    # Pair and connect devices.
    await self._pair_major_device()
    await self._pair_minor_device()

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      for ref in self.refs:
        if is_active:
          await self.disconnect_with_check(
              ref.random_address, android_constants.Transport.LE, ref
          )
        else:
          if not (
              ref_dut_acl := ref.device.find_connection_by_bd_addr(
                  hci.Address(self.dut.address), transport=core.BT_LE_TRANSPORT
              )
          ):
            self.fail("Unable to find connection between REF and DUT")
          async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
            self.logger.info("[REF] Disconnect DUT")
            await ref_dut_acl.disconnect()

        self.logger.info("[DUT] Wait for disconnected")
        await dut_cb.wait_for_event(
            bl4a_api.AclDisconnected(
                address=ref.random_address,
                transport=android_constants.Transport.LE,
            )
        )

    with self.dut.bl4a.register_callback(bl4a_api.Module.LE_AUDIO) as dut_cb:
      for ref in self.refs:
        self.logger.info("[REF] Start advertising")
        announcement_type = (
            bap.AnnouncementType.GENERAL
            if is_active
            else bap.AnnouncementType.TARGETED
        )
        bap_announcement = ascs.make_bap_announcement(
            announcement_type=(announcement_type),
            available_audio_contexts=bap.ContextType(0xFFFF),
        )
        cap_announcement = ascs.make_cap_announcement(announcement_type)
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          await ref.device.create_advertising_set(
              advertising_parameters=_DEFAUILT_ADVERTISING_PARAMETERS,
              advertising_data=bytes(
                  core.AdvertisingData([
                      _GENERAL_DISCOVERABLE_AD_FLAGS,
                      bap_announcement,
                      cap_announcement,
                  ])
              ),
          )
        if is_active:
          self.logger.info("[DUT] Reconnect REF")
          self.dut.bt.connect(ref.random_address)

        self.logger.info("[DUT] Wait for LE Audio connected")
        await dut_cb.wait_for_event(
            bl4a_api.ProfileConnectionStateChanged(
                address=ref.random_address, state=_ConnectionState.CONNECTED
            ),
        )

    # Check if both devices are connected and active.
    self.assertCountEqual(
        self.dut.bt.getActiveDevices(android_constants.Profile.LE_AUDIO),
        [self.refs[0].random_address, self.refs[1].random_address],
    )

  async def test_unidirectional_audio_stream(self) -> None:
    """Tests unidirectional audio stream between DUT and REF.

    Test steps:
      1. [Optional] Wait for audio streaming to stop if it is already streaming.
      2. Start audio streaming from DUT.
      3. Wait for audio streaming to start from REF.
      4. Stop audio streaming from DUT.
      5. Wait for audio streaming to stop from REF.
    """
    # Pair and connect devices.
    await self._pair_major_device()
    await self._pair_minor_device()

    sink_ase_states: list[pyee_extensions.EventTriggeredValueObserver] = []
    for ref in self.refs:
      sink_ase_states.extend(
          pyee_extensions.EventTriggeredValueObserver(
              ase,
              ase.EVENT_STATE_CHANGE,
              functools.partial(
                  lambda ase: cast(
                      ascs.AudioStreamEndpointCharacteristic, ase
                  ).state,
                  ase,
              ),
          )
          for ase in _get_service_from_device(
              ref.device, ascs.AudioStreamControlService
          ).ase_state_machines.values()
          if ase.role == ascs.AudioRole.SINK
      )

    # Make sure audio is not streaming.
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      for ase_state in sink_ase_states:
        await ase_state.wait_for_target_value(
            ascs.AudioStreamEndpointCharacteristic.State.IDLE
        )

    self.logger.info("[DUT] Start audio streaming")
    await asyncio.to_thread(self.dut.bt.audioPlaySine)
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      for ase_state in sink_ase_states:
        await ase_state.wait_for_target_value(
            ascs.AudioStreamEndpointCharacteristic.State.STREAMING
        )

    # Streaming for 1 second.
    await asyncio.sleep(_STREAMING_TIME_SECONDS)

    self.logger.info("[DUT] Stop audio streaming")
    await asyncio.to_thread(self.dut.bt.audioStop)
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      for ase_state in sink_ase_states:
        await ase_state.wait_for_target_value(
            ascs.AudioStreamEndpointCharacteristic.State.IDLE
        )

  async def test_bidirectional_audio_stream(self) -> None:
    """Tests bidirectional audio stream between DUT and REF.

    Test steps:
      1. [Optional] Wait for audio streaming to stop if it is already streaming.
      2. Put a call on DUT to make conversational audio context.
      3. Start audio streaming from DUT.
      4. Wait for audio streaming to start from REF.
      5. Stop audio streaming from DUT.
      6. Wait for audio streaming to stop from REF.
    """
    # Pair and connect devices.
    await self._pair_major_device()
    await self._pair_minor_device()

    ase_states: list[pyee_extensions.EventTriggeredValueObserver] = []
    for ref in self.refs:
      ase_states.extend(
          pyee_extensions.EventTriggeredValueObserver(
              ase,
              ase.EVENT_STATE_CHANGE,
              functools.partial(
                  lambda ase: cast(
                      ascs.AudioStreamEndpointCharacteristic, ase
                  ).state,
                  ase,
              ),
          )
          for ase in _get_service_from_device(
              ref.device, ascs.AudioStreamControlService
          ).ase_state_machines.values()
      )

    dut_telecom_cb = self.dut.bl4a.register_callback(bl4a_api.Module.TELECOM)
    self.test_case_context.push(dut_telecom_cb)
    call = self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.OUTGOING,
    )

    with call:
      await dut_telecom_cb.wait_for_event(
          bl4a_api.CallStateChanged,
          lambda e: (e.state in (_CallState.CONNECTING, _CallState.DIALING)),
      )

      # Make sure audio is not streaming.
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        for ase_state in ase_states:
          await ase_state.wait_for_target_value(
              ascs.AudioStreamEndpointCharacteristic.State.IDLE
          )

      self.logger.info("[DUT] Start audio streaming")
      await asyncio.to_thread(self.dut.bt.audioPlaySine)
      recorder = await self.dut.bl4a.start_audio_recording(
          _RECORDING_PATH,
          source=bl4a_api.AudioRecorder.Source.VOICE_COMMUNICATION,
      )
      self.test_case_context.push(recorder)

      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        # With current configuration, all ASEs will be active in bidirectional
        # streaming.
        for ase_state in ase_states:
          await ase_state.wait_for_target_value(
              ascs.AudioStreamEndpointCharacteristic.State.STREAMING
          )

      # Streaming for 1 second.
      await asyncio.sleep(_STREAMING_TIME_SECONDS)

      self.logger.info("[DUT] Stop audio streaming")
      await asyncio.to_thread(self.dut.bt.audioStop)

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      for ase_state in ase_states:
        await ase_state.wait_for_target_value(
            ascs.AudioStreamEndpointCharacteristic.State.IDLE
        )

  async def test_reconfiguration(self) -> None:
    """Tests reconfiguration from media to conversational.

    Test steps:
      1. [Optional] Wait for audio streaming to stop if it is already streaming.
      2. Start audio streaming from DUT.
      3. Wait for audio streaming to start from REF.
      4. Put a call on DUT to trigger reconfiguration.
      5. Wait for ASE to be reconfigured.
    """
    # Pair and connect devices.
    await self._pair_major_device()
    await self._pair_minor_device()

    sink_ases: list[ascs.AudioStreamEndpointCharacteristic] = []
    for ref in self.refs:
      sink_ases.extend(
          ase
          for ase in _get_service_from_device(
              ref.device, ascs.AudioStreamControlService
          ).ase_state_machines.values()
          if ase.role == ascs.AudioRole.SINK
      )
    sink_ase_states = [
        pyee_extensions.EventTriggeredValueObserver(
            ase,
            ase.EVENT_STATE_CHANGE,
            functools.partial(
                lambda ase: cast(
                    ascs.AudioStreamEndpointCharacteristic, ase
                ).state,
                ase,
            ),
        )
        for ase in sink_ases
    ]

    # Make sure audio is not streaming.
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      for ase_state in sink_ase_states:
        await ase_state.wait_for_target_value(
            ascs.AudioStreamEndpointCharacteristic.State.IDLE
        )

    self.logger.info("[DUT] Start audio streaming")
    await asyncio.to_thread(self.dut.bt.audioPlaySine)
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      for ase_state in sink_ase_states:
        await ase_state.wait_for_target_value(
            ascs.AudioStreamEndpointCharacteristic.State.STREAMING
        )
    for sink_ase in sink_ases:
      self.assertIsInstance(sink_ase.metadata, le_audio.Metadata)
      if (entry := _get_audio_context_entry(sink_ase)) is None:
        self.fail("Audio context is not found")
      context_type = struct.unpack_from("<H", entry.data)[0]
      self.assertNotEqual(context_type, bap.ContextType.PROHIBITED)
      self.assertFalse(context_type & bap.ContextType.CONVERSATIONAL)

    # Streaming for 1 second.
    await asyncio.sleep(_STREAMING_TIME_SECONDS)

    call = self.dut.bl4a.make_phone_call(
        _CALLER_NAME,
        _CALLER_NUMBER,
        constants.Direction.OUTGOING,
    )
    with call:
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        self.logger.info("[DUT] Wait for ASE to be released")
        for ase_state in sink_ase_states:
          await ase_state.wait_for_target_value(
              ascs.AudioStreamEndpointCharacteristic.State.IDLE
          )
        self.logger.info("[DUT] Wait for ASE to be reconfigured")
        for ase_state in sink_ase_states:
          await ase_state.wait_for_target_value(
              ascs.AudioStreamEndpointCharacteristic.State.STREAMING
          )
        for sink_ase in sink_ases:
          if (entry := _get_audio_context_entry(sink_ase)) is None:
            self.fail("Audio context is not found")
          context_type = struct.unpack_from("<H", entry.data)[0]
          self.assertTrue(context_type & bap.ContextType.CONVERSATIONAL)

  async def test_streaming_later_join(self) -> None:
    """Tests connecting to devices later during streaming.

    Test steps:
      1. Start audio streaming from DUT.
      2. Start advertising from REF(Left), wait for DUT to connect.
      3. Wait for audio streaming to start from REF(Left).
      4. Start advertising from REF(Right), wait for DUT to connect.
      5. Wait for audio streaming to start from REF(Right).
    """
    if self.dut.device.is_emulator:
      self.skipTest("Rootcanal doesn't support APCF.")

    # Pair and connect the major device.
    await self._pair_major_device()
    await self._pair_minor_device()

    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      for ref in self.refs:
        self.logger.info("[DUT] Disconnect REF")
        self.dut.bt.disconnect(ref.random_address)
        self.logger.info("[DUT] Wait for disconnected")
        await dut_cb.wait_for_event(
            bl4a_api.AclDisconnected(
                address=ref.random_address,
                transport=android_constants.Transport.LE,
            )
        )

    sink_ase_states: list[pyee_extensions.EventTriggeredValueObserver] = []
    for ref in self.refs:
      sink_ase_states.extend(
          pyee_extensions.EventTriggeredValueObserver(
              ase,
              ase.EVENT_STATE_CHANGE,
              functools.partial(
                  lambda ase: cast(
                      ascs.AudioStreamEndpointCharacteristic, ase
                  ).state,
                  ase,
              ),
          )
          for ase in _get_service_from_device(
              ref.device, ascs.AudioStreamControlService
          ).ase_state_machines.values()
          if ase.role == ascs.AudioRole.SINK
      )

    self.logger.info("[DUT] Start audio streaming")
    await asyncio.to_thread(self.dut.bt.audioPlaySine)

    for sink_ase, ref in zip(sink_ase_states, self.refs):
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await ref.device.create_advertising_set(
            advertising_parameters=_DEFAUILT_ADVERTISING_PARAMETERS,
            advertising_data=bytes(
                core.AdvertisingData([
                    ascs.make_bap_announcement(
                        announcement_type=bap.AnnouncementType.TARGETED
                    ),
                    _GENERAL_DISCOVERABLE_AD_FLAGS,
                ])
            ),
        )
        self.logger.info("[REF] Wait for ASE to be streaming")
        await sink_ase.wait_for_target_value(
            ascs.AudioStreamEndpointCharacteristic.State.STREAMING
        )

  async def test_volume_initialization(self) -> None:
    """Makes sure DUT sets the volume correctly after connecting to REF."""
    if not self.dut_vcp_enabled:
      self.skipTest("VCP is not enabled on DUT")

    # Pair and connect the devices.
    await self._pair_major_device()
    await self._pair_minor_device()

    # Wait for the volume to be stable.
    await asyncio.sleep(_PREPARE_TIME_SECONDS)

    ratio = vcs.MAX_VOLUME / self.dut.bt.getMaxVolume(_StreamType.MUSIC)
    dut_volume = self.dut.bt.getVolume(_StreamType.MUSIC)
    ref_expected_volume = decimal.Decimal(dut_volume * ratio).to_integral_exact(
        rounding=decimal.ROUND_HALF_UP
    )
    # The behavior to set volume is not clear, but we can make sure the volume
    # should be correctly synchronized between DUT and all REF devices.
    for ref in self.refs:
      ref_vcs = _get_service_from_device(ref.device, vcs.VolumeControlService)
      self.assertEqual(ref_expected_volume, ref_vcs.volume_setting)

  @navi_test_base.named_parameterized(
      dict(
          testcase_name="from_dut",
          issuer=constants.TestRole.DUT,
      ),
      dict(
          testcase_name="from_ref",
          issuer=constants.TestRole.REF,
      ),
  )
  async def test_set_volume(self, issuer: constants.TestRole) -> None:
    """Tests setting volume over LEA VCP from DUT or REF.

    Test steps:
      1. Set volume from DUT or REF.
      2. Wait for the volume to be set correctly on the other devices.

    Args:
      issuer: The role that issues the volume setting request.
    """
    if not self.dut_vcp_enabled:
      self.skipTest("VCP is not enabled on DUT")

    await self._pair_major_device()
    await self._pair_minor_device()

    dut_max_volume = self.dut.bt.getMaxVolume(_StreamType.MUSIC)

    def dut_to_ref_volume(dut_volume: int) -> int:
      return int(
          decimal.Decimal(
              dut_volume / dut_max_volume * vcs.MAX_VOLUME
          ).to_integral_exact(rounding=decimal.ROUND_HALF_UP)
      )

    def get_volume_setting(service: vcs.VolumeControlService) -> int:
      return service.volume_setting

    # DUT's VCS client might not be stable at the beginning. If we set volume
    # immediately, the volume might not be set correctly.
    await asyncio.sleep(_PREPARE_TIME_SECONDS)

    ref_vcs_services = [
        _get_service_from_device(ref.device, vcs.VolumeControlService)
        for ref in self.refs
    ]

    for dut_volume in range(dut_max_volume + 1):
      ref_volume = dut_to_ref_volume(dut_volume)
      if self.dut.bt.getVolume(_StreamType.MUSIC) == dut_volume:
        # Skip if DUT volume is already set to the target.
        continue
      with self.dut.bl4a.register_callback(
          bl4a_api.Module.AUDIO
      ) as dut_audio_cb:
        if issuer == constants.TestRole.DUT:
          self.logger.info("[DUT] Set volume to %d", dut_volume)
          self.dut.bt.setVolume(_StreamType.MUSIC, dut_volume)
        else:
          self.logger.info("[REF] Set volume to %d", dut_volume)
          ref_vcs_services[0].volume_setting = ref_volume
          await self.refs[0].device.notify_subscribers(
              ref_vcs_services[0].volume_state
          )

        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          for i, ref_vcs_service in enumerate(ref_vcs_services):
            self.logger.info(
                "[REF-%d] Wait for volume to be set to %d", i, ref_volume
            )
            await pyee_extensions.EventTriggeredValueObserver[int](
                ref_vcs_service,
                ref_vcs_service.EVENT_VOLUME_STATE_CHANGE,
                functools.partial(get_volume_setting, ref_vcs_service),
            ).wait_for_target_value(ref_volume)
        # Only when remote device sets volume, DUT can receive the intent.
        if issuer == constants.TestRole.REF:
          self.logger.info("[DUT] Wait for volume to be set")
          await dut_audio_cb.wait_for_event(
              bl4a_api.VolumeChanged(
                  stream_type=_StreamType.MUSIC, volume_value=dut_volume
              ),
          )

  async def test_mcp_play_pause(self) -> None:
    """Tests playing and pausing media playback over MCP.

    Test steps:
      1. Connect MCP.
      2. Subscribe MCP characteristics.
      3. Play media playback on DUT.
      4. Pause media playback over MCP.
      5. Wait for playback to pause.
      6. Resume media playback over MCP.
      7. Wait for playback to start.
    """
    if not self.dut_mcp_enabled:
      self.skipTest("MCP is not enabled on DUT")

    await self._pair_major_device()
    await self._pair_minor_device()

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Connect GMCS")
      ref_mcp_clients = await asyncio.gather(*[
          self._make_service_client(
              ref.device, _GenericMediaControlServiceProxy
          )
          for ref in self.refs
      ])
      self.logger.info("[REF] Subscribe MCP characteristics")
      await asyncio.gather(*[
          ref_mcp_client.subscribe_characteristics()
          for ref_mcp_client in ref_mcp_clients
      ])

    media_states = [
        await gatt_helper.MutableCharacteristicState.create(
            ref_mcp_client.media_state
        )
        for ref_mcp_client in ref_mcp_clients
        if ref_mcp_client.media_state
    ]
    self.assertLen(media_states, self.NUM_REF_DEVICES)

    dut_player_cb = self.dut.bl4a.register_callback(bl4a_api.Module.PLAYER)
    self.test_case_context.push(dut_player_cb)

    self.logger.info("[DUT] Play")
    await asyncio.to_thread(self.dut.bt.audioPlaySine)
    self.logger.info("[DUT] Wait for playback started")
    await dut_player_cb.wait_for_event(
        bl4a_api.PlayerIsPlayingChanged(is_playing=True)
    )

    for i, media_state in enumerate(media_states):
      self.logger.info(
          "[REF-%d] Wait for media state to be PLAYING", i
      )
      await media_state.wait_for_target_value(bytes([mcp.MediaState.PLAYING]))

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS, msg="[REF] Pause"
    ):
      # Pause from the first REF device.
      result = await ref_mcp_clients[0].write_control_point(_McpOpcode.PAUSE)
      self.assertEqual(result, mcp.MediaControlPointResultCode.SUCCESS)
      for i, media_state in enumerate(media_states):
        self.logger.info("[REF-%d] Wait for media state to be PAUSED", i)
        await media_state.wait_for_target_value(bytes([mcp.MediaState.PAUSED]))
    self.logger.info("[DUT] Wait for playback paused")
    await dut_player_cb.wait_for_event(
        bl4a_api.PlayerIsPlayingChanged(is_playing=False),
    )

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS, msg="[REF] Play"
    ):
      # Resume from the second REF device.
      result = await ref_mcp_clients[1].write_control_point(_McpOpcode.PLAY)
      self.assertEqual(result, mcp.MediaControlPointResultCode.SUCCESS)
      for i, media_state in enumerate(media_states):
        self.logger.info("[REF-%d] Wait for media state to be PLAYING", i)
        await media_state.wait_for_target_value(bytes([mcp.MediaState.PLAYING]))
    self.logger.info("[DUT] Wait for playback started")
    await dut_player_cb.wait_for_event(
        bl4a_api.PlayerIsPlayingChanged(is_playing=True)
    )

  async def test_ccp_accept_and_terminate_call(self) -> None:
    """Tests answering and terminating a call over CCP.

    Test steps:
      1. Connect CCP.
      2. Read and subscribe CCP characteristics.
      3. Put an incoming call from DUT.
      4. Accept the call on REF.
      5. Terminate the call on REF.
    """
    if not self.dut_ccp_enabled:
      self.skipTest("CCP is not enabled on DUT")

    await self._pair_major_device()
    await self._pair_minor_device()

    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Connect TBS")
      ref_tbs_clients = await asyncio.gather(*[
          self._make_service_client(
              ref.device, ccp.GenericTelephoneBearerServiceProxy
          )
          for ref in self.refs
      ])
      self.logger.info("[REF] Read and subscribe TBS characteristics")
      await asyncio.gather(*[
          ref_tbs_client.read_and_subscribe_characteristics()
          for ref_tbs_client in ref_tbs_clients
      ])

    expected_call_index = 1
    call = self.dut.bl4a.make_phone_call(
        _CALLER_NAME, _CALLER_NUMBER, _Direction.INCOMING
    )
    self.test_case_context.push(call)
    dut_telecom_cb = self.dut.bl4a.register_callback(bl4a_api.Module.TELECOM)
    self.test_case_context.push(dut_telecom_cb)
    for i, ref_tbs_client in enumerate(ref_tbs_clients):
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg=f"[REF-{i}] Wait for call state change",
      ):
        await ref_tbs_client.call_state.wait_for_target_value(
            bytes(
                [expected_call_index, ccp.CallState.INCOMING, ccp.CallFlag(0)]
            )
        )

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS, msg="[REF-0] Accept call"
    ):
      await ref_tbs_clients[0].accept(expected_call_index)

    self.logger.info("[DUT] Wait for call to be active")
    await dut_telecom_cb.wait_for_event(
        bl4a_api.CallStateChanged(
            state=_CallState.ACTIVE, handle=mock.ANY, name=mock.ANY
        ),
    )

    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS, msg="[REF-1] Terminate call"
    ):
      await ref_tbs_clients[1].terminate(expected_call_index)

    self.logger.info("[DUT] Wait for call to be disconnected")
    await dut_telecom_cb.wait_for_event(
        bl4a_api.CallStateChanged(
            state=_CallState.DISCONNECTED, handle=mock.ANY, name=mock.ANY
        ),
    )
    for i, ref_tbs_client in enumerate(ref_tbs_clients):
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg=f"[REF-{i}] Wait for call state change",
      ):
        await ref_tbs_client.call_state.wait_for_target_value(b"")


if __name__ == "__main__":
  test_runner.main()
