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

"""Tests related to Bluetooth A2DP Source role on Pixel."""

import asyncio
from collections.abc import Callable, Iterable, Sequence
import enum
import functools
import itertools
import time
from typing import Literal

from bumble import a2dp
from bumble import avdtp
from bumble import avrcp
from bumble import core
from bumble import device as bumble_device
from bumble import hci
from bumble import l2cap
from bumble import utils
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import a2dp as a2dp_ext
from navi.bumble_ext import avdtp as avdtp_ext
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api

_A2DP_SERVICE_RECORD_HANDLE = 1
_DEFAULT_STEP_TIMEOUT_SECONDS = 15.0
_SHORT_STEP_TIMEOUT_SECONDS = 5.0
_DEFAULT_STREAM_DURATION_SECONDS = 2.0
_CODEC_CONFIG_MAX_PRIORITY = 1000000


_A2dpCodec = a2dp_ext.A2dpCodec
_Module = bl4a_api.Module
_A2dpState = android_constants.A2dpState


class OptionalCodecsPref(enum.IntEnum):
  DISABLED = 0
  ENABLED = 1
  UNKNOWN = -1


class A2dpSourceTest(navi_test_base.TwoDevicesTestBase):
  """A2DP Source (DUT) tests."""
  dut_supported_codecs: set[int]

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if (
        self.dut.getprop(android_constants.Property.A2DP_SOURCE_ENABLED)
        != "true"
    ):
      raise signals.TestAbortClass("A2DP Source is not enabled on DUT.")

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    # Make sure A2DP profile is connected, or get_a2dp_supported_codec_types
    # will return empty list.
    self.dut.bt.waitForProfileReady(android_constants.Profile.A2DP)
    self.dut_supported_codecs = {
        codec.id
        for codec in self.dut.bl4a.get_a2dp_supported_codec_types()
        if codec.id is not None
    }

  @override
  async def async_teardown_test(self) -> None:
    self.dut.bt.audioStop()
    await super().async_teardown_test()

  def _setup_a2dp_sink_from_ref(
      self,
      codecs: Sequence[_A2dpCodec],
      *,
      protocol_factory: Callable[..., avdtp.Protocol] | None = None,
  ) -> avdtp_ext.Listener:
    """Sets up A2DP Sink profile on REF.

    Args:
      codecs: A2DP codecs supported by REF.
      protocol_factory: Factory function or class for creating the AVDTP
        protocol instance.

    Returns:
      An avdtp_ext.Listener.
    """
    self.logger.info("[REF]setup_a2dp_sink_from_ref")
    return a2dp_ext.setup_sink_server(
        self.ref.device,
        [codec.get_default_capabilities() for codec in codecs],
        _A2DP_SERVICE_RECORD_HANDLE,
        protocol_factory=protocol_factory,
    )

  async def _pair_and_connect_from_dut(
      self,
      codecs: list[_A2dpCodec] | None = None,
      *,
      protocol_factory: Callable[..., avdtp.Protocol] | None = None,
      wait_for_active: bool = True,
  ) -> avdtp.Protocol:
    """Tests A2DP connection establishment right after a pairing session."""
    if codecs is None:
      codecs = [_A2dpCodec.SBC]
    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      listener = self._setup_a2dp_sink_from_ref(
          codecs, protocol_factory=protocol_factory
      )
      protocol_future: asyncio.Future[avdtp.Protocol] = (
          asyncio.get_running_loop().create_future()
      )
      listener.once(listener.EVENT_CONNECTION, protocol_future.set_result)

      self.logger.info("[DUT] Connect and pair REF.")
      await self.classic_connect_and_pair(connect_profiles=True)

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )
      if wait_for_active:
        self.logger.info("[DUT] Wait for A2DP becomes active.")
        await dut_cb.wait_for_event(
            bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
            timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
        )

      return await asyncio.wait_for(
          protocol_future, timeout=_DEFAULT_STEP_TIMEOUT_SECONDS
      )

  async def _find_or_connect_acl_from_ref(
      self, dut_address: str
  ) -> bumble_device.Connection:
    """Finds or creates an ACL connection from REF to DUT."""
    if not (
        dut_ref_acl := self.ref.device.find_connection_by_bd_addr(
            hci.Address(dut_address)
        )
    ):
      dut_ref_acl = await self.ref.device.connect(
          dut_address,
          core.BT_BR_EDR_TRANSPORT,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )
      self.logger.info("[REF] Authenticate and encrypt connection.")
      await dut_ref_acl.authenticate()
      await dut_ref_acl.encrypt()
    return dut_ref_acl

  async def _connect_a2dp_from_dut(self, ref_address: str) -> None:
    """Initiates A2DP connection from DUT to REF."""
    self.logger.info("[DUT] Initiating A2DP connection.")
    self.dut.bt.connect(ref_address)

  async def _connect_a2dp_from_ref(
      self, dut_ref_acl: bumble_device.Connection
  ) -> None:
    """Initiates A2DP (AVDTP) connection from REF to DUT."""
    self.logger.info("[REF] Initiating AVDTP connection.")
    await avdtp.Protocol.connect(dut_ref_acl)

  async def test_avdtp_autoconnect_when_only_avctp_connected(self) -> None:
    """Tests AVDTP auto-connect when only AVCTP is connected.

    Test steps:
      1. Setup pairing and initial A2DP connection between DUT and REF.
      2. Terminate ACL connection from DUT.
      3. Setup AVRCP on REF.
      4. Connect ACL from REF.
      5. Connect AVRCP from REF (only AVCTP connected).
      6. Wait and verify that DUT initiates AVDTP connection.
    """
    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      # Setup pairing and initial A2DP connection
      avdtp_listener = self._setup_a2dp_sink_from_ref([_A2dpCodec.SBC])
      self.logger.info("[DUT] Connect and pair REF.")
      await self.classic_connect_and_pair(connect_profiles=True)

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      # Terminate ACL connection
      self.logger.info("[DUT] Terminate ACL connection.")
      await self.disconnect_with_check(
          self.ref.address, android_constants.Transport.CLASSIC
      )

      # Setup AVRCP on REF
      avrcp_protocol = avrcp.Protocol()
      avrcp_protocol.listen(self.ref.device)

      # Connect ACL from REF
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Find or connect ACL connection from DUT.",
      ):
        dut_ref_acl = await self._find_or_connect_acl_from_ref(self.dut.address)

      # Connect AVRCP from REF (only AVCTP connected)
      self.logger.info("[REF] Connect AVRCP (AVCTP only).")
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Connect AVRCP from REF.",
      ):
        await avrcp_protocol.connect(dut_ref_acl)

      # Wait for AVDTP connection from DUT
      avdtp_future: asyncio.Future[avdtp.Protocol] = (
          asyncio.get_running_loop().create_future()
      )
      avdtp_listener.once(
          avdtp_listener.EVENT_CONNECTION, avdtp_future.set_result
      )

      self.logger.info(
          "[REF] Waiting for incoming AVDTP connection from DUT..."
      )
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg=(
              "[REF] DUT did not initiate AVDTP connection after AVCTP"
              " connection."
          ),
          with_log=False,
      ):
        await avdtp_future

      self.logger.info("[REF] Received incoming AVDTP connection from DUT!")

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

  async def test_paired_connect_a2dp_simultaneous(self) -> None:
    """Tests A2DP connection establishment with simultaneous connection.

    Test steps:
      1. Setup pairing between DUT(A2DP Source) and REF(A2DP Sink).
      2. Terminate ACL connection from DUT.
      3. Setup ACL connection from REF.
      4. Trigger A2DP connection from DUT and REF at same time.
      5. Wait A2DP connected on DUT.
      6. Disconnect from DUT.
      7. Wait A2DP disconnected on DUT.

    Test Results:
      DUT should be able to establish A2DP connection successfully even in
      conflicting scenarios.
    """

    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      # Step 1: Setup pairing and initial A2DP connection
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[DUT] Setup pairing and initial A2DP connection.",
      ):
        await self._pair_and_connect_from_dut()

      # Step 2: Terminate ACL connection
      await self.disconnect_with_check(
          self.ref.address, android_constants.Transport.CLASSIC
      )

      # Step 3: Setup ACL connection from REF
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Find or connect ACL connection from DUT.",
      ):
        dut_ref_acl = await self._find_or_connect_acl_from_ref(self.dut.address)

      # Step 4: Trigger connection from DUT and REF at same time
      self.logger.info(
          "[DUT & REF] Triggering simultaneous A2DP (AVDTP) connection."
      )

      # Use asyncio.gather to run both connection attempts concurrently
      try:
        await asyncio.wait_for(
            asyncio.gather(
                self._connect_a2dp_from_dut(self.ref.address),
                self._connect_a2dp_from_ref(dut_ref_acl),
            ),
            timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
        )
      except (core.BaseBumbleError, TimeoutError):
        self.logger.warning(
            "[REF & DUT] Simultaneous A2DP connection exception.",
            stack_info=True,
        )

      # Step 5: Wait for A2DP to be connected on DUT
      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )
      self.logger.info("[DUT] Wait for A2DP becomes active.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileActiveDeviceChanged(address=self.ref.address),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      # Step 6: Disconnect from DUT
      self.logger.info("Step 6: Disconnect from DUT.")
      self.dut.bt.disconnect(self.ref.address)

      # Step 7: Wait for A2DP to be disconnected on DUT
      self.logger.info("Step 7: Wait for A2DP disconnected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

  # TODO: Remove this skip once the bug is fixed.
  @navi_test_base.TwoDevicesTestBase.require_flag(
      "com.android.bluetooth.flags.a2dp_setconfig_collision_resolution",
  )
  async def test_avdtp_set_configuration_collision_dut_initiator_ref_accept(
      self,
  ) -> None:
    """Tests AVDTP SetConfiguration collision resolution (DUT initiator).

    Simulates a collision where both DUT and REF attempt to configure A2DP
    streams simultaneously. Verifies that the DUT handles the collision
    gracefully without deadlocks or state corruption.

    Test steps:
      1. Setup initial pairing and A2DP connection.
      2. Disconnect A2DP from DUT.
      3. Setup custom AVDTP listener on REF to intercept SetConfiguration.
      4. DUT initiates A2DP connection. REF intercepts the request and fires its
         own SetConfiguration in the background to trigger collision.
      5. Verify DUT gracefully resolves the collision and reaches CONNECTED
         state.
    """
    collision_result = asyncio.get_running_loop().create_future()
    logger = self.logger

    class CollidingStream(avdtp.Stream):

      async def on_set_configuration_command(
          self, configuration: Iterable[avdtp.ServiceCapabilities]
      ) -> avdtp.Message | None:
        """Handles incoming SetConfiguration command during collision.

        Args:
          configuration: The requested service capabilities.

        Returns:
          The SetConfiguration response or reject message.
        """
        try:
          logger.info(
              "[REF] Intercepted SetConfiguration in custom Stream class"
              " initiator=DUT"
          )

          # 1. Fire SetConfiguration in background using high-level API.
          set_config_task = asyncio.create_task(
              self.protocol.set_configuration(
                  self.remote_endpoint.seid,
                  self.local_endpoint.seid,
                  configuration,
              )
          )

          # Start the waiter in background.
          async def wait_for_our_config() -> None:
            try:
              response = await set_config_task
              logger.info(
                  "[REF] Our SetConfiguration was accepted by DUT: %r", response
              )
              if not collision_result.done():
                collision_result.set_result(None)
            except core.ProtocolError as e:
              logger.warning(
                  "[REF] Our SetConfiguration was rejected by DUT (acceptable"
                  " in collision): %r",
                  e,
              )
              if not collision_result.done():
                collision_result.set_result(None)
            except Exception as e:  # pylint: disable=broad-exception-caught
              logger.exception("[REF] Error waiting for our config")
              if not collision_result.done():
                collision_result.set_exception(e)

          utils.cancel_on_event(
              self.protocol, self.protocol.EVENT_CLOSE, wait_for_our_config()
          )

          # Delay to prevent immediate response interruption.
          await asyncio.sleep(0.5)

          # 2. Respond to DUT's command.
          return await super().on_set_configuration_command(configuration)

        except Exception as e:  # pylint: disable=broad-exception-caught
          logger.exception("[REF] Error in custom Stream class handler")
          if not collision_result.done():
            collision_result.set_exception(e)
          return avdtp.Set_Configuration_Reject(
              error_code=avdtp.AVDTP_BAD_STATE_ERROR
          )

    class CollidingProtocol(avdtp_ext.Protocol):
      stream_factory = CollidingStream  # pyrefly: ignore[bad-assignment]

    # Step 1: Setup pairing and initial A2DP connection.
    await self._pair_and_connect_from_dut([_A2dpCodec.SBC])

    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      # Step 2: Disconnect A2DP from DUT.
      self.logger.info("[DUT] Disconnect A2DP.")
      self.dut.bt.disconnect(self.ref.address)

      self.logger.info("[DUT] Wait for A2DP disconnected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      # CRITICAL: Clean up the previous listener to avoid "PSM already in use".
      self.ref.device.l2cap_channel_manager.servers.pop(avdtp.AVDTP_PSM, None)

      # Step 3 & 4: Setup listener and connect from DUT.
      # (REF intercepts DUT's command and triggers collision.)
      self._setup_a2dp_sink_from_ref(
          [_A2dpCodec.SBC], protocol_factory=CollidingProtocol
      )

      self.logger.info("[DUT] Initiating A2DP connection to trigger collision.")
      self.dut.bt.connect(self.ref.address)

      # Ensure our background collision task actually ran (await before final
      # connection state).
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS * 2,
          "[REF] Collision race task did not finish in time!",
          with_log=False,
      ):
        await collision_result

      # Step 5: Wait A2DP connected on DUT.
      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

  @navi_test_base.named_parameterized(
      ref_accepted=dict(accept_config=True),
      ref_rejected=dict(accept_config=False),
  )
  async def test_avdtp_set_configuration_collision_ref_initiator(
      self, accept_config: bool
  ) -> None:
    """Tests AVDTP SetConfiguration collision resolution (REF initiator).

    Simulates a collision where both DUT and REF attempt to configure A2DP
    streams simultaneously. Verifies that the DUT handles the collision
    gracefully without deadlocks or state corruption.
    Args:
      accept_config: Whether the REF should accept or reject the DUT's
        SetConfiguration request during the collision.

    Test steps:
      1. Setup initial pairing and A2DP connection.
      2. Disconnect A2DP from DUT.
      3. Setup custom AVDTP listener on REF.
      4. REF initiates ACL and AVDTP connection to DUT to trigger collision.
      5. Verify DUT gracefully resolves the collision and reaches CONNECTED
         state.
    """
    collision_result = asyncio.get_running_loop().create_future()
    logger = self.logger

    class CollidingStream(avdtp.Stream):

      async def on_set_configuration_command(
          self, configuration: Iterable[avdtp.ServiceCapabilities]
      ) -> avdtp.Message | None:
        """Handles incoming SetConfiguration command during collision.

        Args:
          configuration: The requested service capabilities.

        Returns:
          The SetConfiguration response or reject message.
        """
        try:
          logger.info(
              "[REF] Intercepted SetConfiguration in custom Stream class"
              " initiator=REF"
          )

          # 1. Fire SetConfiguration in background using high-level API.
          set_config_task = asyncio.create_task(
              self.protocol.set_configuration(
                  self.remote_endpoint.seid,
                  self.local_endpoint.seid,
                  configuration,
              )
          )

          # Delay to prevent immediate response interruption.
          await asyncio.sleep(0.5)

          # Start the waiter in background.
          async def wait_for_our_config() -> None:

            response = await set_config_task
            logger.info(
                "[REF] Our SetConfiguration was accepted by DUT: %r", response
            )
            logger.info("[REF] Opening stream as exact Initiator...")
            self.change_state(avdtp.State.CONFIGURED)
            await self.open()
            logger.info("[REF] Stream opened successfully!")
            if not collision_result.done():
              collision_result.set_result(None)

          utils.cancel_on_event(
              self.protocol, self.protocol.EVENT_CLOSE, wait_for_our_config()
          )

          # 2. Respond to DUT's command.
          if accept_config:
            logger.info(
                "[REF] Calling super().on_set_configuration_command (ACCEPT)"
                " back to AOSP."
            )
            return await super().on_set_configuration_command(configuration)

          logger.info(
              "[REF] Returning Set_Configuration_Reject (REJECT) back to AOSP."
          )
          return avdtp.Set_Configuration_Reject(
              error_code=avdtp.AVDTP_BAD_STATE_ERROR
          )

        except Exception as e:  # pylint: disable=broad-exception-caught
          logger.exception("[REF] Error in custom Stream class handler")
          if not collision_result.done():
            collision_result.set_exception(e)
          return avdtp.Set_Configuration_Reject(
              error_code=avdtp.AVDTP_BAD_STATE_ERROR
          )

    class CollidingProtocol(avdtp_ext.Protocol):
      stream_factory = CollidingStream  # pyrefly: ignore[bad-assignment]

    # Step 1: Setup pairing and initial A2DP connection.
    await self._pair_and_connect_from_dut([_A2dpCodec.SBC])

    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      # Step 2: Disconnect A2DP from DUT.
      self.logger.info("[DUT] Disconnect A2DP.")
      self.dut.bt.disconnect(self.ref.address)

      self.logger.info("[DUT] Wait for A2DP disconnected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      # CRITICAL: Clean up the previous listener to avoid "PSM already in use".
      self.ref.device.l2cap_channel_manager.servers.pop(avdtp.AVDTP_PSM, None)

      # Step 3 & 4: Setup listener and initiate ACL/AVDTP connection from REF.
      # (Both sides will initiate SetConfiguration simultaneously.)
      listener = self._setup_a2dp_sink_from_ref(
          [_A2dpCodec.SBC], protocol_factory=CollidingProtocol
      )

      self.logger.info("[REF] Initiate ACL connection.")
      dut_ref_acl = await self._find_or_connect_acl_from_ref(self.dut.address)

      self.logger.info("[REF] Connect AVDTP from REF to trigger collision.")
      avdtp_protocol = await CollidingProtocol.connect(dut_ref_acl)
      avdtp_protocol.add_sink(_A2dpCodec.SBC.get_default_capabilities())
      listener.set_server(dut_ref_acl, avdtp_protocol)
      await avdtp_protocol.discover_remote_endpoints()

      # Ensure our background collision task actually ran (await before final
      # connection state).
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS * 2,
          "[REF] Collision race task did not finish in time!",
          with_log=False,
      ):
        await collision_result

      # Step 5: Wait A2DP connected on DUT.
      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

  @navi_test_base.named_parameterized(
      enabled=dict(pref=OptionalCodecsPref.ENABLED),
      # TODO: because disabled mode is 100% failing on Android,
      # enable this once the bug is fixed.
      # disabled=dict(pref=OptionalCodecsPref.DISABLED),
      unknown=dict(pref=OptionalCodecsPref.UNKNOWN),
  )
  async def test_playback_with_optional_codecs(
      self, pref: OptionalCodecsPref
  ) -> None:
    """Tests A2DP playback with different optional codecs preferences.

    Args:
      pref: The optional codec preference to configure on the DUT (ENABLED,
        DISABLED, or UNKNOWN).

    Test steps:
      1. Setup A2DP Sink on REF supporting both SBC and AAC codecs.
      2. Connect and pair DUT with REF.
      3. Set the optional codecs preference on DUT for REF.
      4. Wait for A2DP connection to complete.
      5. Trigger music playback from DUT.
      6. Wait and verify that playback has successfully started on DUT.

    Test Results:
      DUT should be able to start audio playback successfully regardless of the
      optional codec preference configured.
    """
    if android_constants.BluetoothCodecId.AAC not in self.dut_supported_codecs:
      self.skipTest("DUT does not support AAC codec.")

    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      # Setup A2DP Sink on REF supporting both SBC and AAC
      listener = self._setup_a2dp_sink_from_ref(
          [_A2dpCodec.SBC, _A2dpCodec.AAC]
      )

      protocol_future: asyncio.Future[avdtp.Protocol] = (
          asyncio.get_running_loop().create_future()
      )

      listener.once(listener.EVENT_CONNECTION, protocol_future.set_result)

      self.logger.info("[DUT] Connect and pair REF.")
      await self.classic_connect_and_pair(connect_profiles=True)

      # Configure optional codecs mode on DUT
      self.logger.info("[DUT] Configuring optional codecs: %r", pref)
      self.dut.bt.setA2dpOptionalCodecsEnabled(self.ref.address, pref.value)

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      self.logger.info("[REF] Waiting for AVDTP connection.")
      await asyncio.wait_for(
          protocol_future, timeout=_DEFAULT_STEP_TIMEOUT_SECONDS
      )

      # Trigger playback from DUT
      self.logger.info("[DUT] Triggering music playback via audioPlaySine")
      self.dut.bt.audioPlaySine()

      # Wait for playback to start
      self.logger.info("[DUT] Waiting for playback to start...")
      await dut_cb.wait_for_event(
          bl4a_api.A2dpPlayingStateChanged(
              address=self.ref.address,
              state=android_constants.A2dpState.PLAYING,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
          timeout_msg="DUT reports A2DP is not playing",
      )

      await asyncio.sleep(_DEFAULT_STREAM_DURATION_SECONDS)

      self.assertTrue(
          self.dut.bt.isA2dpPlaying(self.ref.address),
          "[DUT] reports A2DP is not keeping playing",
      )

      self.logger.info("[DUT] Playback started successfully")

  @navi_test_base.named_parameterized(
      sbc_valid=dict(
          codec_type=android_constants.A2dpCodecType.SBC,
          sample_rate=android_constants.A2dpSampleRate.RATE_44100,
      ),
      sbc_invalid=dict(
          codec_type=android_constants.A2dpCodecType.SBC,
          sample_rate=android_constants.A2dpSampleRate.RATE_176400,
      ),
      # TODO: Re-enable this case once the bug is fixed.
      # aac_valid=dict(
      #     codec_type=android_constants.A2dpCodecType.AAC,
      #     sample_rate=android_constants.A2dpSampleRate.RATE_44100,
      # ),
      aac_invalid=dict(
          codec_type=android_constants.A2dpCodecType.AAC,
          sample_rate=android_constants.A2dpSampleRate.RATE_176400,
      ),
  )
  async def test_reconfigure_codec_during_streaming(
      self,
      codec_type: android_constants.A2dpCodecType,
      sample_rate: android_constants.A2dpSampleRate,
  ) -> None:
    """Tests reconfiguring A2DP codec during streaming.

    Args:
      codec_type: The target codec type to configure.
      sample_rate: The target sample rate to configure.

    Test steps:
      1. Setup A2DP Sink on REF supporting both SBC and AAC codecs.
      2. Connect and pair DUT with REF.
      3. Wait for A2DP connection to be established.
      4. Play music and wait for playback to start.
      5. While music is playing, switch codec configuration using
         setA2dpCodecConfig with valid/invalid parameters.
      6. Verify playback is still active.
    """
    if android_constants.BluetoothCodecId.AAC not in self.dut_supported_codecs:
      self.skipTest("DUT does not support AAC codec.")

    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      # Setup A2DP Sink on REF supporting both SBC and AAC
      listener = self._setup_a2dp_sink_from_ref(
          [_A2dpCodec.SBC, _A2dpCodec.AAC]
      )

      protocol_future: asyncio.Future[avdtp.Protocol] = (
          asyncio.get_running_loop().create_future()
      )

      listener.once(listener.EVENT_CONNECTION, protocol_future.set_result)

      # Connect and pair DUT with REF
      self.logger.info("[DUT] Connect and pair REF.")
      await self.classic_connect_and_pair(connect_profiles=True)

      # Wait for A2DP connection to be established
      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      self.logger.info("[REF] Waiting for AVDTP protocol to be established.")
      await asyncio.wait_for(
          protocol_future, timeout=_DEFAULT_STEP_TIMEOUT_SECONDS
      )

      # Play music and wait for playback to start
      self.logger.info("[DUT] Triggering music playback via audioPlaySine")
      self.dut.bt.audioPlaySine()
      self.logger.info("[DUT] Waiting for playback to start...")
      await dut_cb.wait_for_event(
          bl4a_api.A2dpPlayingStateChanged(
              address=self.ref.address,
              state=android_constants.A2dpState.PLAYING,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
          timeout_msg="DUT reports A2DP is not playing",
      )
      self.logger.info("[DUT] Playback started successfully")

      # Reconfigure codec during streaming
      self.logger.info(
          "[DUT] Setting A2DP codec config during streaming: %s, sample"
          " rate: %s",
          codec_type.name,
          sample_rate.name,
      )
      codec_config = bl4a_api.A2dpCodecConfiguration(
          codec_type=codec_type,
          sample_rate=sample_rate,
          priority=_CODEC_CONFIG_MAX_PRIORITY,
      )
      self.dut.bl4a.set_a2dp_codec_config(self.ref.address, codec_config)

      # Wait for reconfiguration to be processed
      await asyncio.sleep(_DEFAULT_STREAM_DURATION_SECONDS)

      # Verify playback is still active
      self.assertTrue(
          self.dut.bt.isA2dpPlaying(self.ref.address),
          "[DUT] reports A2DP is not playing after codec reconfiguration.",
      )
      self.logger.info("[DUT] Playback is still active after reconfiguring.")

  async def test_reconfigure_codec_error_unsupported(self) -> None:
    """Tests DUT tolerance when REF sends invalid codec configuration.

    Test steps:
      1. Setup A2DP Sink on REF and intercept the AVDTP Protocol instance.
      2. Connect and pair DUT with REF.
      3. Wait for A2DP connection to be established.
      4. Close the existing active stream to transition endpoint to IDLE.
      5. Build an invalid SBC codec configuration (invalid sampling frequency).
      6. Discover remote endpoints and send SetConfiguration with the invalid
         codec capabilities from REF to DUT.
      7. Verify that DUT rejects the configuration with a ProtocolError.

    Test Results:
      DUT should gracefully reject the unsupported codec configuration without
      crashing or accepting the invalid state.
    """
    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      # Setup listener, connect and pair without disconnecting later
      listener = self._setup_a2dp_sink_from_ref([_A2dpCodec.SBC])
      protocol_future: asyncio.Future[avdtp.Protocol] = (
          asyncio.get_running_loop().create_future()
      )
      listener.once(listener.EVENT_CONNECTION, protocol_future.set_result)

      self.logger.info("[DUT] Connect and pair REF.")
      await self.classic_connect_and_pair(connect_profiles=True)

      # Wait for connection
      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      self.logger.info("[REF] Waiting for AVDTP protocol to be established.")
      avdtp_protocol = await asyncio.wait_for(
          protocol_future, timeout=_DEFAULT_STEP_TIMEOUT_SECONDS
      )

      # To test SetConfiguration, the remote endpoint must be IDLE.
      # Since DUT auto-started a stream, we close it first.
      if avdtp_protocol.streams:
        self.logger.info("[REF] Closing existing stream to make endpoint IDLE.")
        stream = next(iter(avdtp_protocol.streams.values()))
        async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
          await stream.close()

      # Build an invalid configuration (e.g., invalid sampling frequency)
      invalid_sbc_info = (  # 0x00 for sampling freq/channel mode
          b"\x00\x0f\x02\x35"
      )

      invalid_codec_caps = avdtp.MediaCodecCapabilities(
          media_type=avdtp.MediaType.AUDIO,
          media_codec_type=a2dp.CodecType.SBC,
          media_codec_information=invalid_sbc_info,
      )

      # Discover endpoints
      async with self.assert_not_timeout(
          _SHORT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Discover remote endpoints.",
      ):
        discover_response = await avdtp_protocol.send_command(
            avdtp.Discover_Command()
        )
      assert isinstance(discover_response, avdtp.Discover_Response)

      target_seid = next(
          (
              ep.seid
              for ep in discover_response.endpoints
              if ep.tsep == avdtp.AVDTP_TSEP_SRC
          ),
          None,
      )

      if target_seid is None:
        self.fail("[REF] No remote SRC endpoint found.")

      # Send SetConfiguration with invalid parameters and verify reject
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Sending invalid SetCFG and waiting for DUT to reject.",
      ):
        with self.assertRaises(core.ProtocolError) as cm:
          await avdtp_protocol.send_command(
              avdtp.Set_Configuration_Command(
                  acp_seid=target_seid,
                  int_seid=1,
                  capabilities=[invalid_codec_caps],
              )
          )

      self.logger.info(
          "[REF] DUT correctly rejected invalid SetConfiguration: %s",
          cm.exception,
      )

  @navi_test_base.named_parameterized(
      start_cfm=dict(action="start", wait_for_playing=False),
      suspend_cfm=dict(action="suspend", wait_for_playing=True),
  )
  async def test_avdt_handle_cfm_bad_state_error(
      self, action: Literal["start", "suspend"], wait_for_playing: bool
  ) -> None:
    """Tests AVDTP handling of confirmation BAD_STATE error.

    Args:
      action: The AVDTP action to test ("start" or "suspend").
      wait_for_playing: Whether to wait for A2DP playing state before injecting
        the error.

    Test steps:
      1. Setup A2DP Sink on REF with custom protocol rejecting Start/Suspend.
      2. Connect and pair DUT with REF, wait for AVDTP connection to open.
      3. Trigger music streaming from DUT.
      4. For suspend test, wait for playback to start, then stop music.
      5. Verify DUT gracefully closes A2DP connection upon BAD_STATE reject.
    """
    test_logger = self.logger

    class _RejectStartSink(avdtp.LocalSink):

      async def on_start_command(self) -> avdtp.Message | None:
        """Simulates AVDTP_BAD_STATE reject for Start_Command."""
        test_logger.info(
            "[REF] Simulating AVDTP_BAD_STATE reject for Start_Command."
        )
        return avdtp.Start_Reject(self.seid, avdtp.AVDTP_BAD_STATE_ERROR)

    class _RejectSuspendSink(avdtp.LocalSink):

      async def on_suspend_command(self) -> avdtp.Message | None:
        """Simulates AVDTP_BAD_STATE reject for Suspend_Command."""
        test_logger.info(
            "[REF] Simulating AVDTP_BAD_STATE reject for Suspend_Command."
        )
        return avdtp.Suspend_Reject(self.seid, avdtp.AVDTP_BAD_STATE_ERROR)

    sink_class = _RejectStartSink if action == "start" else _RejectSuspendSink

    # Steps 1 & 2: Setup A2DP Sink on REF and Connect DUT with REF.
    self.logger.info("[Steps 1 & 2] Setup A2DP and Connect DUT with REF.")
    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      _ = await self._pair_and_connect_from_dut(
          [_A2dpCodec.SBC],
          protocol_factory=functools.partial(
              avdtp_ext.Protocol, sink_factory=sink_class
          ),
          wait_for_active=wait_for_playing,
      )

      # Step 3: Trigger streaming from DUT.
      self.logger.info("[Step 3] Trigger music streaming from DUT.")
      self.dut.bt.audioPlaySine()

      # Step 4: For suspend test, wait for playback to start, then stop music.
      if wait_for_playing:
        self.logger.info("[DUT] Waiting for playback to start...")
        if not self.dut.bt.isA2dpPlaying(self.ref.address):
          await dut_cb.wait_for_event(
              bl4a_api.A2dpPlayingStateChanged(
                  address=self.ref.address,
                  state=android_constants.A2dpState.PLAYING,
              ),
              timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
              timeout_msg="DUT reports A2DP is not playing",
          )

        await asyncio.sleep(_DEFAULT_STREAM_DURATION_SECONDS)

        self.assertTrue(
            self.dut.bt.isA2dpPlaying(self.ref.address),
            "[DUT] reports A2DP is not keeping playing",
        )

        self.logger.info("[DUT] Stopping music playback via audioStop")
        self.dut.bt.audioStop()

      # Step 5: Verify that DUT gracefully closes the A2DP connection.
      self.logger.info("[Step 5] Verify DUT closes A2DP connection.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
          timeout_msg=(
              "[DUT] A2DP did not disconnect in time after BAD_STATE reject."
          ),
      )
      self.logger.info("[DUT] A2DP connection disconnected successfully.")

  async def test_avdt_open_after_timeout(self) -> None:
    """Tests AVDTP stream establishment fallback when peer device stalls.

    Test steps:
      1. Establish an ACL connection and pair REF with DUT without connecting
         profiles.
      2. Establish an AVDTP connection initiated by REF.
      3. REF discovers endpoints and sends a SetConfiguration command to DUT.
      4. REF intentionally halts the state machine and does not send an Open
         command.
      5. Wait for DUT to timeout, abort the stalled stream, and re-initiate
         the AVDTP stream setup.
      6. Verify that the A2DP connection is successfully established.

    Test Results:
      DUT should gracefully handle the stalled connection by aborting the
      pending
      stream and automatically re-initiating the stream setup as the initiator,
      eventually reaching the CONNECTED state.
    """
    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      # Setup Listener and SDP so REF can accept the incoming RTP channel later
      avdtp_listener = self._setup_a2dp_sink_from_ref([_A2dpCodec.SBC])

      # Establish an ACL connection and pair without connecting profiles.
      self.logger.info("[REF] Connect and pair DUT without profiles.")
      await self.classic_connect_and_pair(connect_profiles=False)

      dut_ref_acl = await self._find_or_connect_acl_from_ref(self.dut.address)

      # Establish an AVDTP connection initiated by REF.
      self.logger.info("[REF] Connect AVDTP from REF.")
      avdtp_protocol = await avdtp.Protocol.connect(dut_ref_acl)

      # Link our manually created client protocol to the listener
      # so it can process the incoming RTP L2CAP connection.
      avdtp_listener.set_server(dut_ref_acl, avdtp_protocol)

      # Add our test sink endpoint explicitly to the newly created
      # protocol instance.
      sink = avdtp_protocol.add_sink(_A2dpCodec.SBC.get_default_capabilities())

      # Discover remote endpoints and their capabilities
      self.logger.info("[REF] Discovering remote endpoints and capabilities.")
      endpoints = await avdtp_protocol.discover_remote_endpoints()

      target_endpoint = next(
          (ep for ep in endpoints if ep.tsep == avdtp.AVDTP_TSEP_SRC),
          None,
      )
      if target_endpoint is None:
        self.fail("No remote SRC endpoint found.")

      target_seid = target_endpoint.seid
      remote_caps = next(
          (
              cap
              for cap in target_endpoint.capabilities
              if isinstance(cap, avdtp.MediaCodecCapabilities)
              and cap.media_codec_type == _A2dpCodec.SBC.codec_type
          ),
          None,
      )
      if not remote_caps:
        self.fail("Remote does not support SBC codec.")

      ref_config = a2dp_ext.select_configuration(_A2dpCodec.SBC, remote_caps)

      # Create a listener to wait for AVDTP open
      avdtp_future: asyncio.Future[None] = (
          asyncio.get_running_loop().create_future()
      )

      def on_open() -> None:
        self.logger.info("[REF] AVDTP Open received.")
        if not avdtp_future.done():
          avdtp_future.set_result(None)

      sink.once(sink.EVENT_OPEN, on_open)

      # Send SetConfiguration but intentionally stall the state machine
      # and do NOT send Open command.
      self.logger.info("[REF] Sending SetConfiguration.")
      target_proxy = avdtp.StreamEndPointProxy(avdtp_protocol, target_seid)

      # Use create_stream to initiate SetConfiguration
      sink.configuration = ref_config
      await avdtp_protocol.create_stream(sink, target_proxy)

      # Wait for DUT to timeout, abort the stalled stream, and re-initiate
      # the stream setup.
      self.logger.info("[REF] Waiting for DUT to send Open_Command.")
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] DUT did not send Open_Command after SetCFG timeout.",
          with_log=False,
      ):
        await avdtp_future

      # Verify A2DP is connected since stream is opened
      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

  @navi_test_base.named_parameterized(
      no_delay_report_sent=dict(send_delay_report=False),
      delay_report_sent=dict(send_delay_report=True),
  )
  async def test_avdt_wait_before_sending_open_command(
      self, send_delay_report: bool
  ) -> None:
    """Tests if AOSP DUT waits 2 seconds before sending AVDT Open command.

    DUT should wait for that time to allow the sink device to send an AVDT Delay
    Report command. If the REF sends AVDT Delay Report, the Open command should
    be sent immediately after.

    Args:
      send_delay_report: Whether to send a Delay Report from REF upon receiving
        SetConfiguration.

    Test steps:
      1. Setup A2DP Sink on REF with Delay Reporting capability.
      2. Connect and pair DUT with REF.
      3. (If `send_delay_report` is true) send a Delay Report from REF upon
      receiving SetConfiguration.
      4. Measure the time elapsed between AVDTP SetConfiguration and Open
      commands.
      5. Verify the elapsed time is at least 2.0 seconds if Delay Report is
      not sent, or less than 2.0 seconds if it is.
      6. Verify A2DP stream connects and plays successfully.
    """
    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      # Setup Listener and SDP for REF. We manually add the sink capability
      # later.
      avdtp_listener = self._setup_a2dp_sink_from_ref([])

      # Use a queue to capture the incoming AVDTP protocol connection
      avdtp_protocols = asyncio.Queue[avdtp.Protocol]()
      avdtp_listener.on(
          avdtp_listener.EVENT_CONNECTION, avdtp_protocols.put_nowait
      )

      # Setup futures to capture the time when the SetConfiguration and Open
      # commands are sent.
      config_time_future: asyncio.Future[float] = (
          asyncio.get_running_loop().create_future()
      )
      open_time_future: asyncio.Future[float] = (
          asyncio.get_running_loop().create_future()
      )

      self.logger.info("[DUT] Connect and pair REF.")
      await self.classic_connect_and_pair(connect_profiles=True)

      # Wait for the AVDTP connection to be established
      avdtp_protocol = await asyncio.wait_for(
          avdtp_protocols.get(), timeout=_DEFAULT_STEP_TIMEOUT_SECONDS
      )

      # Manually create the sink to avoid race conditions with event dispatching
      sink = avdtp_protocol.add_sink(_A2dpCodec.SBC.get_default_capabilities())

      # Add Delay Reporting capability to the sink.
      sink.capabilities = list(sink.capabilities) + [
          avdtp.ServiceCapabilities(
              service_category=avdtp.AVDTP_DELAY_REPORTING_SERVICE_CATEGORY
          )
      ]

      # Register event handlers directly on the newly created sink
      async def on_config() -> None:
        if not config_time_future.done():
          config_time_future.set_result(time.perf_counter())
        if send_delay_report:
          self.logger.info("[REF] Sending Delay Report.")
          assert sink.stream is not None
          await avdtp_protocol.send_command(
              avdtp.DelayReport_Command(
                  acp_seid=sink.stream.remote_endpoint.seid, delay=100
              )
          )

      def on_open() -> None:
        if not open_time_future.done():
          open_time_future.set_result(time.perf_counter())

      sink.on(sink.EVENT_CONFIGURATION, on_config)
      sink.on(sink.EVENT_OPEN, on_open)

      self.logger.info("[REF] Wait for EVENT_CONFIGURATION.")
      start_time = await asyncio.wait_for(
          config_time_future, timeout=_DEFAULT_STEP_TIMEOUT_SECONDS
      )

      self.logger.info("[REF] Wait for EVENT_OPEN.")
      end_time = await asyncio.wait_for(
          open_time_future, timeout=_SHORT_STEP_TIMEOUT_SECONDS
      )

      # Verify the timing.
      elapsed_time = end_time - start_time
      self.logger.info(
          "[REF] Elapsed time between SetConfiguration and Open: %s",
          elapsed_time,
      )
      if send_delay_report:
        self.assertLess(elapsed_time, 2.0)
      else:
        self.assertGreaterEqual(elapsed_time, 2.0)

      # Verify A2DP connected
      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      # Start streaming and verify playback starts successfully.
      self.logger.info("[DUT] Triggering music playback via audioPlaySine")
      self.dut.bt.audioPlaySine()

      self.logger.info("[DUT] Waiting for playback to start...")
      await dut_cb.wait_for_event(
          bl4a_api.A2dpPlayingStateChanged(
              address=self.ref.address,
              state=android_constants.A2dpState.PLAYING,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

  async def test_avdt_handles_stream_discover_response_in_open_state(
      self,
  ) -> None:
    """Tests AVDTP handling stream discovery response in open state.

    Test steps:
      1. REF initiates ACL and AVDTP signaling connection to DUT.
      2. REF discovers DUT capabilities and configures an AAC stream.
      3. DUT accepts configuration and initiates Discover_Command.
      4. REF intercepts Discover_Command and initiates Open_Command before
      responding.
      5. REF responds to Discover_Command after the media channel is opened.
      6. Verify DUT successfully obtains capabilities from REF in open state.
    """
    test_logger = self.logger
    discover_command_fut: asyncio.Future[avdtp.Discover_Command] = (
        asyncio.get_running_loop().create_future()
    )
    media_opened_fut: asyncio.Future[None] = (
        asyncio.get_running_loop().create_future()
    )
    get_cap_fut: asyncio.Future[None] = (
        asyncio.get_running_loop().create_future()
    )

    class DelayedDiscoverProtocol(avdtp_ext.Protocol):

      async def on_discover_command(
          self, command: avdtp.Discover_Command
      ) -> avdtp.Message | None:
        """Intercepts Discover_Command to delay response until media opens."""
        test_logger.info("[REF] Intercepted Discover_Command from DUT.")
        if not discover_command_fut.done():
          discover_command_fut.set_result(command)

        test_logger.info(
            "[REF] Waiting for media channel to open before responding..."
        )
        await media_opened_fut
        test_logger.info("[REF] Media channel open. Responding to Discover.")
        return await super().on_discover_command(command)

      async def on_get_capabilities_command(
          self, command: avdtp.Get_Capabilities_Command
      ) -> avdtp.Message | None:
        """Intercepts GetCapabilities to verify DUT capability discovery."""
        test_logger.info(
            "[REF] Intercepted GetCapabilities for SEID %d", command.acp_seid
        )
        result = await super().on_get_capabilities_command(command)
        if not get_cap_fut.done():
          get_cap_fut.set_result(None)
        return result

      async def on_get_all_capabilities_command(
          self, command: avdtp.Get_All_Capabilities_Command
      ) -> avdtp.Message | None:
        """Intercepts GetAllCapabilities to verify DUT capability discovery."""
        test_logger.info(
            "[REF] Intercepted GetAllCapabilities for SEID %d", command.acp_seid
        )
        result = await super().on_get_all_capabilities_command(command)
        if not get_cap_fut.done():
          get_cap_fut.set_result(None)
        return result

    dut_cb = self.test_case_context.enter_context(
        self.dut.bl4a.register_callback(_Module.A2DP)
    )
    # Setup listener with custom protocol supporting SBC and AAC.
    listener = self._setup_a2dp_sink_from_ref(
        [_A2dpCodec.SBC, _A2dpCodec.AAC],
        protocol_factory=DelayedDiscoverProtocol,
    )

    # Step 1: REF initiates ACL connection without profiles.
    self.logger.info("[Step 1] Initiate ACL connection without profiles.")
    dut_ref_acl = await self.classic_connect_and_pair(connect_profiles=False)
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[DUT] Connect and pair REF without profiles.",
        with_log=False,
    ):
      self.logger.info("[REF] Connect AVDTP signaling channel.")
      avdtp_protocol = await DelayedDiscoverProtocol.connect(dut_ref_acl)
      avdtp_protocol.add_sink(_A2dpCodec.SBC.get_default_capabilities())
      avdtp_protocol.add_sink(_A2dpCodec.AAC.get_default_capabilities())
      listener.set_server(dut_ref_acl, avdtp_protocol)

      # Step 2: REF discovers DUT capabilities and configures an AAC stream.
      self.logger.info("[Step 2] Discover DUT endpoints.")
      endpoints = await avdtp_protocol.discover_remote_endpoints()

      # pylint: disable=g-complex-comprehension
      aac_endpoint, aac_caps = next(
          (
              (ep, cap)
              for ep in endpoints
              for cap in ep.capabilities
              if isinstance(cap, avdtp.MediaCodecCapabilities)
              and cap.media_codec_type == a2dp.A2DP_MPEG_2_4_AAC_CODEC_TYPE
          ),
          (None, None),
      )
      # pylint: enable=g-complex-comprehension

      self.assertIsNotNone(aac_endpoint, "DUT AAC endpoint not found.")
      assert aac_endpoint is not None and aac_caps is not None

      config_caps = [
          avdtp.ServiceCapabilities(
              service_category=avdtp.AVDTP_MEDIA_TRANSPORT_SERVICE_CATEGORY,
              service_capabilities_bytes=b"",
          ),
          aac_caps,
      ]

      self.logger.info("[REF] Initiate SetConfiguration to DUT.")
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Timeout waiting for DUT to accept SetConfiguration",
          with_log=False,
      ):
        await avdtp_protocol.set_configuration(
            acp_seid=aac_endpoint.seid, int_seid=2, capabilities=config_caps
        )

      # Step 3: DUT accepted SetConfiguration. Discover_Command was intercepted.
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Timeout waiting for DUT to send Discover_Command",
          with_log=False,
      ):
        await discover_command_fut
      self.logger.info("[REF] DUT Discover_Command intercepted successfully.")

      # Step 4: REF intercepts Discover_Command and initiates Open_Command.
      self.logger.info("[Step 4] Initiate Open_Command to DUT.")
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Timeout waiting for DUT to open AVDTP signaling channel",
          with_log=False,
      ):
        await avdtp_protocol.open(aac_endpoint.seid)

      self.logger.info("[REF] Initiate media transport L2CAP channel.")
      await dut_ref_acl.create_l2cap_channel(
          l2cap.ClassicChannelSpec(psm=avdtp.AVDTP_PSM)
      )

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      # Step 5: REF responds to Discover_Command after media channel is opened.
      self.logger.info("[Step 5] Media open. Unblocking Discover_Response.")
      media_opened_fut.set_result(None)

    # Step 6: Verify DUT successfully obtains capabilities from REF in open
    # state.
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[DUT] Waiting for DUT to send GetCapabilities...",
        with_log=False,
    ):
      await get_cap_fut
    self.logger.info(
        "[DUT] DUT successfully obtained capabilities in open state."
    )

  async def test_dut_disconnects_after_no_avdt_start_response(self) -> None:
    """Tests that DUT disconnects L2CAP Channel after no response to AVDT Start.

    Test steps:
      1. Setup A2DP Sink on REF and connect/pair with DUT.
      2. Hook into REF's AVDTP protocol to drop incoming Start_Command messages.
      3. Trigger music playback on DUT to initiate AVDTP Start.
      4. Verify that the Start_Command is received by REF.
      5. Verify that DUT disconnects the AVDTP signaling channel after a
      timeout.
    """
    start_cmd_future: asyncio.Future[None] = (
        asyncio.get_running_loop().create_future()
    )

    logger = self.logger

    class HangingAvdtpProtocol(avdtp_ext.Protocol):

      async def on_start_command(
          self, command: avdtp.Start_Command
      ) -> avdtp.Message | None:
        del self, command
        logger.info(
            "[REF] Dropping Start_Response/hang to simulate timeout."
        )
        if not start_cmd_future.done():
          start_cmd_future.set_result(None)
        # Hang indefinitely so no response is returned to the DUT
        await asyncio.Event().wait()
        return None

    dut_cb = self.test_case_context.enter_context(
        self.dut.bl4a.register_callback(_Module.A2DP)
    )
    # Setup Listener and SDP for REF
    avdtp_listener = self._setup_a2dp_sink_from_ref(
        [_A2dpCodec.SBC], protocol_factory=HangingAvdtpProtocol
    )

    # Use a queue to capture the incoming AVDTP protocol connection
    avdtp_protocols = asyncio.Queue[avdtp.Protocol]()
    avdtp_listener.on(
        avdtp_listener.EVENT_CONNECTION, avdtp_protocols.put_nowait
    )

    self.logger.info("[DUT] Connect and pair REF.")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await self.classic_connect_and_pair(connect_profiles=True)

    self.logger.info("[DUT] Wait for A2DP connected.")
    await dut_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

    # Wait for the AVDTP connection to be established
    avdtp_protocol = await asyncio.wait_for(
        avdtp_protocols.get(), timeout=_DEFAULT_STEP_TIMEOUT_SECONDS
    )

    l2cap_closed_future: asyncio.Future[None] = (
        asyncio.get_running_loop().create_future()
    )

    # Listen for AVDTP protocol close (L2CAP disconnection)
    def on_close() -> None:
      if not l2cap_closed_future.done():
        l2cap_closed_future.set_result(None)

    avdtp_protocol.once(avdtp_protocol.EVENT_CLOSE, on_close)

    # Start streaming to trigger Start_Command
    self.logger.info("[DUT] Triggering music playback via audioPlaySine")
    self.dut.bt.audioPlaySine()

    self.logger.info("[REF] Waiting for Start_Command...")
    await asyncio.wait_for(
        start_cmd_future, timeout=_DEFAULT_STEP_TIMEOUT_SECONDS
    )

    self.logger.info(
        "[REF] Waiting for AVDTP L2CAP channel disconnection (expect ~15s)..."
    )
    # Wait for the DUT to hit its internal timeout and disconnect
    # the L2CAP Channel.
    await asyncio.wait_for(l2cap_closed_future, timeout=20.0)

    # Verify A2DP is disconnected on DUT
    self.logger.info("[DUT] Wait for A2DP disconnected.")
    await dut_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.DISCONNECTED,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )

  @navi_test_base.parameterized(
      *itertools.product(
          [
              android_constants.A2dpSampleRate.RATE_44100,
              android_constants.A2dpSampleRate.RATE_48000,
              android_constants.A2dpSampleRate.RATE_88200,
              android_constants.A2dpSampleRate.RATE_96000,
          ],
          [
              android_constants.A2dpBitsPerSample.BITS_16,
              android_constants.A2dpBitsPerSample.BITS_24,
              android_constants.A2dpBitsPerSample.BITS_32,
          ],
      )
  )
  async def test_ldac_config_switching(
      self,
      sample_rate: android_constants.A2dpSampleRate,
      bits_per_sample: android_constants.A2dpBitsPerSample,
  ) -> None:
    """Tests LDAC sample rate and bits per sample switching.

    Args:
      sample_rate: The target sample rate to configure.
      bits_per_sample: The target bits per sample to configure.

    Test steps:
      1. Set up A2DP connection (negotiates default LDAC 48kHz Stereo).
      2. Set codec config to target LDAC sample rate and bit depth.
      3. Verify config changed to target values.
      4. Start stream.
      5. Stop stream.
    """
    if android_constants.BluetoothCodecId.LDAC not in self.dut_supported_codecs:
      self.skipTest("DUT does not support LDAC.")

    self.dut.bt.audioSetRepeat(android_constants.RepeatMode.ONE)

    with self.dut.bl4a.register_callback(_Module.A2DP) as dut_cb:
      ref_avdtp_connection = await self._pair_and_connect_from_dut(
          [_A2dpCodec.SBC, _A2dpCodec.LDAC]
      )

      self.logger.info(
          "[DUT] Set codec config to LDAC %s %s.",
          sample_rate.name,
          bits_per_sample.name,
      )
      codec_config = bl4a_api.A2dpCodecConfiguration(
          codec_type=android_constants.A2dpCodecType.LDAC,
          sample_rate=sample_rate,
          bits_per_sample=bits_per_sample,
          priority=1_000_000,
      )
      self.dut.bl4a.set_a2dp_codec_config(self.ref.address, codec_config)

      self.logger.info(
          "[DUT] Wait for A2DP codec config changed to %s %s.",
          sample_rate.name,
          bits_per_sample.name,
      )

      def match_event(e: bl4a_api.A2dpCodecConfigChanged) -> bool:
        self.logger.info("Received event: %s", e)
        if e.codec_config is None:
          return False
        return (
            e.codec_config.codec_type == android_constants.A2dpCodecType.LDAC
            and e.codec_config.sample_rate == sample_rate
            and e.codec_config.bits_per_sample == bits_per_sample
        )

      await dut_cb.wait_for_event(
          bl4a_api.A2dpCodecConfigChanged,
          predicate=match_event,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      # Wait for stream to become active if it was suspended during reconfig
      ref_sinks = a2dp_ext.find_local_endpoints_by_codec(
          ref_avdtp_connection,
          _A2dpCodec.LDAC.codec_type,
          avdtp.LocalSink,
          vendor_id=_A2dpCodec.LDAC.vendor_id,
          codec_id=_A2dpCodec.LDAC.codec_id,
      )
      if not ref_sinks:
        self.fail(f"No sink found for codec {_A2dpCodec.LDAC.name}.")
      ref_sink = a2dp_ext.LocalSinkWrapper(ref_sinks[0])
      async with ref_sink.condition:
        if ref_sink.stream_state == avdtp.State.STREAMING:
          self.logger.info("[REF] A2DP is streaming, wait for A2DP stopped.")
          await ref_sink.condition.wait_for(
              lambda: ref_sink.stream_state != avdtp.State.STREAMING
          )

      self.logger.info("[DUT] Start stream.")
      self.dut.bt.audioPlaySine()

      self.logger.info("[DUT] Wait for A2DP started.")
      await dut_cb.wait_for_event(
          bl4a_api.A2dpPlayingStateChanged(
              address=self.ref.address, state=_A2dpState.PLAYING
          )
      )
      async with (
          self.assert_not_timeout(
              _DEFAULT_STEP_TIMEOUT_SECONDS, msg="[REF] Wait for A2DP started."
          ),
          ref_sink.condition,
      ):
        await ref_sink.condition.wait_for(
            lambda: ref_sink.stream_state == avdtp.State.STREAMING
        )

      self.logger.info(
          "[DUT] Stream for %d seconds.", _DEFAULT_STREAM_DURATION_SECONDS
      )
      await asyncio.sleep(_DEFAULT_STREAM_DURATION_SECONDS)

      self.logger.info("[DUT] Stop stream.")
      self.dut.bt.audioPause()

      self.logger.info("[DUT] Wait for A2DP stopped.")
      await dut_cb.wait_for_event(
          bl4a_api.A2dpPlayingStateChanged(
              address=self.ref.address, state=_A2dpState.NOT_PLAYING
          )
      )
      async with (
          self.assert_not_timeout(
              _DEFAULT_STEP_TIMEOUT_SECONDS, msg="[REF] Wait for A2DP stopped."
          ),
          ref_sink.condition,
      ):
        await ref_sink.condition.wait_for(
            lambda: ref_sink.stream_state != avdtp.State.STREAMING
        )

  async def test_sink_as_initiator_no_reconnect_after_acl_disconnect(
      self,
  ) -> None:
    """Tests DUT does not reconnect when REF starts AVDT and disconnects ACL.

    Test steps:
      1. Initial pairing and active ACL connection between DUT and REF.
      2. Initiate AVDTP signaling connection from REF to DUT.
      3. Disconnect ACL connection from REF before AVDTP negotiation completes.
      4. Verify DUT does not retry ACL connection to REF.
    """
    listener = self._setup_a2dp_sink_from_ref([_A2dpCodec.SBC])

    # Step 1: Initial pairing and active ACL connection between DUT and REF.
    self.logger.info("[DUT] Connect and pair REF without connecting profiles.")
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS,
        msg="[DUT] Connect and pair REF without connecting profiles.",
        with_log=False,
    ):
      dut_ref_acl = await self.classic_connect_and_pair(connect_profiles=False)

    # Step 2: REF initiates AVDTP signaling connection to DUT.
    self.logger.info("[REF] Connect AVDTP signaling channel from REF.")
    avdtp_protocol = await avdtp.Protocol.connect(dut_ref_acl)
    listener.set_server(dut_ref_acl, avdtp_protocol)

    # Step 3: REF disconnects ACL connection before AVDTP negotiation completes.
    self.logger.info("[REF] Disconnect ACL connection.")
    with self.dut.bl4a.register_callback(_Module.ADAPTER) as dut_adapter_cb:
      async with self.assert_not_timeout(
          _DEFAULT_STEP_TIMEOUT_SECONDS,
          msg="[REF] Disconnect ACL connection.",
          with_log=False,
      ):
        await dut_ref_acl.disconnect()

      self.logger.info("[DUT] Wait for ACL disconnected.")
      await dut_adapter_cb.wait_for_event(
          bl4a_api.AclDisconnected(
              address=self.ref.address,
              transport=android_constants.Transport.CLASSIC,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

      # Step 4: Verify DUT does not retry ACL connection to REF.
      async with self.assert_timeout(
          10.0,
          msg="[REF] Verify DUT does not retry ACL connection.",
          with_log=False,
      ):
        await self.ref.device.accept(f"{self.dut.address}/P")
      self.logger.info("[DUT] No new connection retry from DUT as expected.")

  @navi_test_base.named_parameterized(
      capability_toggling_from_enabled=dict(
          initial_delay_report=True,
          second_delay_report=False,
          third_delay_report=True,
          reconfigure_codec=False,
      ),
      capability_toggling_from_disabled=dict(
          initial_delay_report=False,
          second_delay_report=True,
          third_delay_report=False,
          reconfigure_codec=False,
      ),
      retention_after_codec_reconfiguration=dict(
          initial_delay_report=False,
          second_delay_report=False,
          third_delay_report=True,
          reconfigure_codec=True,
      ),
  )
  async def test_delay_report(
      self,
      initial_delay_report: bool,
      second_delay_report: bool,
      third_delay_report: bool,
      reconfigure_codec: bool,
  ) -> None:
    """Tests AVDTP delay report capability negotiation and codec reconfiguration.

    Args:
      initial_delay_report: Delay report support for initial connection.
      second_delay_report: Delay report support for second connection.
      third_delay_report: Delay report support for third connection.
      reconfigure_codec: Whether to execute dynamic codec reconfiguration.

    Test steps:
      1. Connect, pair, open AVDTP, and disconnect with remote REF using initial
         delay report capability.
      2. If reconfiguring codec, execute dynamic switch from AAC to SBC and
         disconnect. Otherwise, perform second connection cycle with second
         delay report capability and disconnect.
      3. Perform final connection cycle with third delay report capability and
         disconnect cleanly.
    """
    delay_report_enabled = True

    class DelayReportProtocol(avdtp_ext.Protocol):
      """Custom AVDTP protocol for dynamic Delay Reporting toggling."""

      def add_sink(
          self, codec_capabilities: avdtp.MediaCodecCapabilities
      ) -> avdtp.LocalSink:
        """Adds a local sink endpoint with dynamic delay reporting capability.

        Args:
          codec_capabilities: The media codec capabilities.

        Returns:
          The created local sink endpoint.
        """
        sink = super().add_sink(codec_capabilities)
        if delay_report_enabled:
          sink.capabilities = list(sink.capabilities) + [
              avdtp.ServiceCapabilities(
                  service_category=avdtp.AVDTP_DELAY_REPORTING_SERVICE_CATEGORY,
                  service_capabilities_bytes=b"",
              )
          ]
        return sink

    dut_a2dp_cb = self.test_case_context.enter_context(
        self.dut.bl4a.register_callback(_Module.A2DP)
    )

    async def _setup_and_connect_a2dp(enable_delay_report: bool) -> None:
      """Sets up A2DP sink on REF and connects A2DP from DUT.

      Args:
        enable_delay_report: Whether to enable Delay Reporting capability.
      """
      nonlocal delay_report_enabled
      delay_report_enabled = enable_delay_report
      self.ref.device.l2cap_channel_manager.servers.pop(avdtp.AVDTP_PSM, None)
      self._setup_a2dp_sink_from_ref(
          [_A2dpCodec.SBC, _A2dpCodec.AAC],
          protocol_factory=DelayReportProtocol,
      )
      self.logger.info("[DUT] Connect A2DP.")
      self.dut.bt.connect(self.ref.address)

      self.logger.info("[DUT] Wait for A2DP connected.")
      await dut_a2dp_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )

    async def _disconnect_a2dp() -> None:
      """Disconnects A2DP connection cleanly."""
      self.logger.info("[DUT] Disconnect A2DP.")
      await self.disconnect_with_check(
          self.ref.address, android_constants.Transport.CLASSIC
      )

    # Step 1: Initial connection cycle.
    self.logger.info(
        "[Step 1] Connect with delay report enabled: %s",
        initial_delay_report,
    )
    delay_report_enabled = initial_delay_report
    self.ref.device.l2cap_channel_manager.servers.pop(avdtp.AVDTP_PSM, None)
    self._setup_a2dp_sink_from_ref(
        [_A2dpCodec.SBC, _A2dpCodec.AAC], protocol_factory=DelayReportProtocol
    )
    self.logger.info("[DUT] Connect and pair REF.")
    async with self.assert_not_timeout(
        _DEFAULT_STEP_TIMEOUT_SECONDS, msg="[DUT] Connect and pair REF."
    ):
      await self.classic_connect_and_pair(connect_profiles=True)

    self.logger.info("[DUT] Wait for A2DP connected.")
    await dut_a2dp_cb.wait_for_event(
        bl4a_api.ProfileConnectionStateChanged(
            address=self.ref.address,
            state=android_constants.ConnectionState.CONNECTED,
        ),
        timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
    )
    await _disconnect_a2dp()

    # Step 2: Second connection cycle or dynamic codec reconfiguration.
    if reconfigure_codec:
      self.logger.info("[Step 2] Connect without delay report supported.")
      await _setup_and_connect_a2dp(False)

      self.logger.info(
          "[Step 2] Reconfigure codec AAC to SBC (delay report not supported)."
      )
      codec_config = bl4a_api.A2dpCodecConfiguration(
          codec_type=android_constants.A2dpCodecType.SBC,
          priority=_CODEC_CONFIG_MAX_PRIORITY,
      )
      self.logger.info("[DUT] Setting A2DP codec config to SBC.")
      self.dut.bl4a.set_a2dp_codec_config(self.ref.address, codec_config)

      self.logger.info("[DUT] Wait for codec config change to SBC.")

      def match_sbc_event(e: bl4a_api.A2dpCodecConfigChanged) -> bool:
        if e.codec_config is None:
          return False
        return e.codec_config.codec_type == android_constants.A2dpCodecType.SBC

      codec_event = await dut_a2dp_cb.wait_for_event(
          bl4a_api.A2dpCodecConfigChanged,
          predicate=match_sbc_event,
          timeout=_DEFAULT_STEP_TIMEOUT_SECONDS,
      )
      self.assertEqual(
          codec_event.codec_config.codec_type,
          android_constants.A2dpCodecType.SBC,
      )
      await _disconnect_a2dp()
    else:
      self.logger.info(
          "[Step 2] Connect with delay report enabled: %s",
          second_delay_report,
      )
      await _setup_and_connect_a2dp(second_delay_report)
      await _disconnect_a2dp()

    # Step 3: Final connection cycle to verify pristine state machine health.
    self.logger.info(
        "[Step 3] Final connect with delay report enabled: %s",
        third_delay_report,
    )
    await _setup_and_connect_a2dp(third_delay_report)
    await _disconnect_a2dp()


if __name__ == "__main__":
  test_runner.main()
