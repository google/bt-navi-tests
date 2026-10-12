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

"""Tests for HID over GATT Profile(GATT) implementation on Android."""

import asyncio
import contextlib
import dataclasses
import struct
from unittest import mock

from bumble import att
from bumble import core
from bumble import device
from bumble import gatt
from bumble import hci
from bumble import pairing
from bumble.profiles import gatt_service
from mobly import test_runner
from mobly import signals
from typing_extensions import override

from navi.bumble_ext import hid
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api
from navi.utils import constants
from navi.utils import input as input_utils
from navi.utils import pyee_extensions

_VIDEO_SERVICE_NAME = "video"
_DEFAULT_STEP_TIMEOUT_SECONDS = 10.0

_FLAG_HANDLE_SERVICE_CHANGE_DURING_DISCOVERY = (
    "com.android.bluetooth.flags.hogp_handle_service_change_during_discovery"
)
# Peers have been observed indicating Service Changed 13-15 ms after encryption
# completes, which lands while the HID host is still opening the connection.
_SERVICE_CHANGED_DELAY_SECONDS = 0.015
# Affected handle range, covering the whole database.
_SERVICE_CHANGED_VALUE = struct.pack("<HH", 0x0001, 0xFFFF)
# A filler service, inserted ahead of the HOGP service to move its handles.
_FILLER_SERVICE_UUID = core.UUID("f1e4a6c2-0b5d-4f9a-9d3e-7c1b8a2f6e40")
_FILLER_CHARACTERISTIC_UUID = core.UUID("f1e4a6c3-0b5d-4f9a-9d3e-7c1b8a2f6e40")


@dataclasses.dataclass(frozen=True)
class _Reconnection:
  """What DUT did while reconnecting to the moved HOGP service.

  Attributes:
    state_changes: The HID connection state changes DUT reported, from the
      start of the reconnection up to the HID host being connected using the
      new handles.
    wrote_before_service_changed: Whether DUT wrote to REF before confirming
      Service Changed.
  """

  state_changes: list[bl4a_api.ProfileConnectionStateChanged]
  wrote_before_service_changed: bool


class HogpTest(navi_test_base.TwoDevicesTestBase):
  ref_hogp_service: gatt.Service
  ref_report_map_characteristic: gatt.Characteristic
  ref_keyboard_input_report_characteristic: gatt.Characteristic
  ref_keyboard_output_report_characteristic: gatt.Characteristic
  ref_mouse_input_report_characteristic: gatt.Characteristic

  def _setup_hid_service(self) -> None:
    self.ref_keyboard_input_report_characteristic = gatt.Characteristic(
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
    )

    self.ref_keyboard_output_report_characteristic = gatt.Characteristic(
        gatt.GATT_REPORT_CHARACTERISTIC,
        gatt.Characteristic.Properties.READ
        | gatt.Characteristic.Properties.WRITE
        | gatt.Characteristic.WRITE_WITHOUT_RESPONSE,
        gatt.Characteristic.READABLE | gatt.Characteristic.WRITEABLE,
        bytes([0]),
        [
            gatt.Descriptor(
                gatt.GATT_REPORT_REFERENCE_DESCRIPTOR,
                gatt.Descriptor.READABLE,
                bytes([0x01, hid.ReportType.OUTPUT_REPORT.value]),
            )
        ],
    )
    self.ref_mouse_input_report_characteristic = gatt.Characteristic(
        gatt.GATT_REPORT_CHARACTERISTIC,
        gatt.Characteristic.Properties.READ
        | gatt.Characteristic.Properties.WRITE
        | gatt.Characteristic.Properties.NOTIFY,
        gatt.Characteristic.READABLE | gatt.Characteristic.WRITEABLE,
        bytes(6),
        [
            gatt.Descriptor(
                gatt.GATT_REPORT_REFERENCE_DESCRIPTOR,
                gatt.Descriptor.READABLE,
                bytes([0x02, hid.ReportType.INPUT_REPORT.value]),
            )
        ],
    )
    self.ref_report_map_characteristic = gatt.Characteristic(
        gatt.GATT_REPORT_MAP_CHARACTERISTIC,
        gatt.Characteristic.Properties.READ,
        gatt.Characteristic.READABLE,
        hid.DEFAULT_REPORT_MAP,
    )
    self.ref_hogp_service = gatt.Service(
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
                # bcdHID=1.1, bCountryCode=0x00,
                # Flags=RemoteWake|NormallyConnectable
                bytes([0x11, 0x01, 0x00, 0x03]),
            ),
            gatt.Characteristic(
                gatt.GATT_HID_CONTROL_POINT_CHARACTERISTIC,
                gatt.Characteristic.WRITE_WITHOUT_RESPONSE,
                gatt.Characteristic.WRITEABLE,
            ),
            self.ref_report_map_characteristic,
            self.ref_keyboard_input_report_characteristic,
            self.ref_keyboard_output_report_characteristic,
            self.ref_mouse_input_report_characteristic,
        ],
    )
    self.ref.device.add_service(self.ref_hogp_service)

  def _rebuild_attribute_table(self, services: list[gatt.Service]) -> None:
    """Rebuilds REF's attribute table with the given services, in order.

    REF's GATT server only appends, handing out handles as services are added,
    so changing the layout means starting the table over.

    Args:
      services: The services to add, including the ones already present.
    """
    server = self.ref.device.gatt_server
    server.services.clear()
    server.attributes.clear()
    server.attributes_by_handle.clear()
    server.subscribers.clear()
    server.add_services(services)

  def _move_hid_service(self) -> None:
    """Relocates the HOGP service on REF by inserting a filler service.

    REF's attribute table is rebuilt with an extra service placed immediately
    ahead of the HOGP service, so every HOGP handle shifts while the GAP and
    GATT services keep theirs. This is what a peer making a genuine structural
    change looks like: the database hash changes, and the HID handles DUT has
    cached become stale.
    """
    previous_handle = self.ref_hogp_service.handle
    filler_service = gatt.Service(
        _FILLER_SERVICE_UUID,
        [
            gatt.Characteristic(
                _FILLER_CHARACTERISTIC_UUID,
                gatt.Characteristic.Properties.READ,
                gatt.Characteristic.READABLE,
                bytes(1),
            )
        ],
    )

    services: list[gatt.Service] = []
    for service in self.ref.device.gatt_server.services:
      if service is self.ref_hogp_service:
        services.append(filler_service)
      services.append(service)
    self._rebuild_attribute_table(services)

    # Guard against the rebuild silently becoming a no-op.
    self.assertNotEqual(self.ref_hogp_service.handle, previous_handle)
    self.logger.info(
        "[REF] Moved HOGP service from handle 0x%04X to 0x%04X",
        previous_handle,
        self.ref_hogp_service.handle,
    )

  def _disable_database_hash(self) -> None:
    """Replaces REF's GATT service with one that has no Database Hash.

    DUT reads the Database Hash on every reconnection, and invalidates its GATT
    cache as soon as it differs, whether or not a Service Changed indication is
    honored. Removing the characteristic takes that shortcut away: DUT keeps
    using its cached database, so acting on the indication becomes the only way
    for it to notice that the database changed.

    Must be called before DUT discovers the database, so that the cache it
    stores has no Database Hash either.
    """
    previous_service = self.ref.device.gatt_service
    assert previous_service is not None

    replacement = gatt_service.GenericAttributeProfileService(
        server_supported_features=(
            gatt.ServerSupportedFeatures.EATT_SUPPORTED
            if self.ref.device.config.eatt_enabled
            else None
        ),
        database_hash_enabled=False,
    )
    self._rebuild_attribute_table([
        replacement if service is previous_service else service
        for service in self.ref.device.gatt_server.services
    ])
    self.ref.device.gatt_service = replacement
    self.logger.info("[REF] Removed the Database Hash characteristic")

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    if self.dut.device.adb.getprop(hid.PROPERTY_HID_HOST_SUPPORTED) != "true":
      raise signals.TestAbortClass("HID host is not supported on DUT")

    # Stay awake during the test.
    self.dut.shell("svc power stayon true")
    # Dismiss the keyguard.
    self.dut.shell("wm dismiss-keyguard")

  @override
  async def async_teardown_class(self) -> None:
    await super().async_teardown_class()
    # Stop staying awake during the test.
    self.dut.shell("svc power stayon false")

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()

  async def test_connect(self) -> None:
    """Tests establishing the HID connection from DUT to REF.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Verify the HID connection is established.
    """
    self._setup_hid_service()
    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
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

  async def test_reconnect(self) -> None:
    """Tests reconnecting the HID connection with the background scanner.

    Test steps:
      1. Pair with REF.
      2. Terminate the connection.
      3. Start advertising on REF.
      4. Verify the HID connection is re-established by the background scanner.
    """
    await self.test_connect()

    ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
        hci.Address(self.dut.address)
    )
    assert ref_dut_acl is not None
    self.logger.info("[REF] Disconnect")
    await ref_dut_acl.disconnect()

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      self.logger.info("[REF] Restart advertising")
      await self.ref.device.start_advertising(
          own_address_type=hci.OwnAddressType.RANDOM,
      )
      self.logger.info("[DUT] Wait for connected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.random_address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )

  def _watch_report_subscription(
      self,
      watcher: pyee_extensions.EventWatcher,
      characteristic: gatt.Characteristic,
  ) -> asyncio.Event:
    """Returns an event that is set when DUT enables notifications.

    The subscription is the interesting signal rather than the connection state:
    the HID host may report a connection while never having written the CCCD on
    REF, in which case the device looks connected but never delivers any input
    report.

    Args:
      watcher: Watcher owning the event handler.
      characteristic: The report characteristic to watch.

    Returns:
      An event, set the first time DUT enables notifications.
    """
    subscribed = asyncio.Event()

    @watcher.on(characteristic, gatt.Characteristic.EVENT_SUBSCRIPTION)
    def on_subscription(
        bearer: att.Bearer, notify_enabled: bool, indicate_enabled: bool
    ) -> None:
      del bearer, indicate_enabled
      if notify_enabled:
        subscribed.set()

    return subscribed

  async def _send_service_changed(self, connection: device.Connection) -> None:
    """Indicates Service Changed from REF to DUT."""
    ref_gatt_service = self.ref.device.gatt_service
    if (
        ref_gatt_service is None
        or ref_gatt_service.service_changed_characteristic is None
    ):
      raise signals.TestAbortClass("REF has no Service Changed characteristic")

    self.logger.info("[REF] Send Service Changed")
    # DUT has to confirm the indication even if nothing on it is interested in
    # Service Changed, so a missing confirmation fails the test.
    await asyncio.wait_for(
        self.ref.device.indicate_subscriber(
            connection,
            ref_gatt_service.service_changed_characteristic,
            _SERVICE_CHANGED_VALUE,
            # DUT might not have written the CCCD on this connection yet,
            # which is exactly the case under test.
            force=True,
        ),
        _DEFAULT_STEP_TIMEOUT_SECONDS,
    )

  def _indicate_service_changed_on_encryption(
      self,
      watcher: pyee_extensions.EventWatcher,
      task_group: asyncio.TaskGroup,
      indicated: asyncio.Event,
  ) -> None:
    """Makes REF indicate Service Changed right after the link is encrypted.

    This reproduces peers observed indicating Service Changed 13-15 ms after
    encryption. If the HID host has to discover the HOGP service on that
    connection, the indication lands while it is still opening; if it restores
    the HOGP service from its cache, it is usually connected already.

    Args:
      watcher: Watcher owning the event handlers. It must be closed before
        task_group exits, so that no task is created afterwards.
      task_group: Task group running the indications.
      indicated: Event set once the indication is confirmed.
    """
    notified_connections = set[int]()

    @watcher.on(self.ref.device, device.Device.EVENT_CONNECTION)
    def on_connection(connection: device.Connection) -> None:

      async def send_after_delay() -> None:
        await asyncio.sleep(_SERVICE_CHANGED_DELAY_SECONDS)
        await self._send_service_changed(connection)
        indicated.set()

      @watcher.on(
          connection, device.Connection.EVENT_CONNECTION_ENCRYPTION_CHANGE
      )
      def on_encryption_change() -> None:
        # Encryption may be updated more than once on a connection.
        if (
            not connection.is_encrypted
            or connection.handle in notified_connections
        ):
          return
        notified_connections.add(connection.handle)
        task_group.create_task(send_after_delay())

  async def _pair_without_completing_hid_open(self) -> None:
    """Pairs with REF, then disconnects while the HID host opens the connection.

    REF disconnects as soon as DUT reads the Report Map. By then, GATT has
    discovered and cached REF's database, but the HID host has not completed the
    connection, so it never stores anything about the HID device. DUT is left
    bonded with REF and holding a GATT cache, but without any HID record.
      - the HID host has to discover the HOGP service on the next connection,
        while GATT uses its cache right away;
      - the HID host does not know the HID device, so it neither reconnects in
        the background nor accepts a connection from it, unless DUT requests
        one.
    """
    report_map = self.ref_report_map_characteristic
    report_map_value = report_map.value
    assert isinstance(report_map_value, bytes)
    report_map_read = asyncio.Event()

    async def disconnect_on_read(connection: device.Connection) -> bytes:
      report_map_read.set()
      self.logger.info("[REF] Disconnect on Report Map read")
      await connection.disconnect()
      # Never delivered, as the connection is gone.
      return report_map_value

    report_map.value = gatt.CharacteristicValue(read=disconnect_on_read)
    try:
      with self.dut.bl4a.register_callback(
          bl4a_api.Module.HID_HOST
      ) as dut_hid_cb:
        self.logger.info("[DUT] Pair with REF")
        await self.le_connect_and_pair(
            hci.OwnAddressType.RANDOM, connect_profiles=True
        )
        self.logger.info("[REF] Wait for Report Map read")
        await asyncio.wait_for(
            report_map_read.wait(), _DEFAULT_STEP_TIMEOUT_SECONDS
        )
        self.logger.info("[DUT] Wait for HID disconnected")
        await dut_hid_cb.wait_for_event(
            bl4a_api.ProfileConnectionStateChanged(
                address=self.ref.random_address,
                state=android_constants.ConnectionState.DISCONNECTED,
            ),
        )
    finally:
      report_map.value = report_map_value

  async def _reconnect_to_moved_hogp_service(
      self,
      *,
      connect_from_dut: bool = False,
  ) -> _Reconnection:
    """Reconnects to REF after moving its HOGP service while disconnected.

    REF indicates Service Changed right after encryption on reconnection, and
    records whether DUT wrote anything to it before confirming the indication.

    Must be called while disconnected.

    Steps:
      1. Move the HOGP service on REF, changing all of its handles.
      2. Arrange REF to indicate Service Changed right after encryption.
      3. If connect_from_dut is set, request the connection from DUT.
      4. Start advertising on REF.
      5. Verify DUT subscribes to the mouse input report at its new handle, so
         that input reports would be delivered.
      6. Verify the HID connection is established, so that it is connected
         when returning.

    Args:
      connect_from_dut: Whether DUT requests the connection. Otherwise, it is
        left to the HID host to reconnect in the background.

    Returns:
      What DUT did while reconnecting.
    """
    service_changed_indicated = asyncio.Event()
    wrote_before_service_changed = False

    # Bumble offers no event for every write a client makes: the attributes
    # only report writes made to them, and the handles DUT writes to before
    # rediscovering are stale, i.e. land on whatever attribute is there now.
    # Writes are thus observed where REF's GATT server handles them all. Only
    # Write Requests are, as that is how the HID host writes the CCCDs.
    gatt_server = self.ref.device.gatt_server
    # Bound before the server is patched below, so this keeps pointing at
    # Bumble's own handler rather than at the wrapper.
    original_on_att_write_request = gatt_server.on_att_write_request

    def record_write_then_handle(
        bearer: att.Bearer, request: att.ATT_Write_Request
    ) -> None:
      # Assigning a bool, so it has to be declared as the enclosing one.
      nonlocal wrote_before_service_changed
      if not service_changed_indicated.is_set():
        self.logger.info(
            "[REF] Write to 0x%04X before Service Changed",
            request.attribute_handle,
        )
        wrote_before_service_changed = True
      # Let Bumble carry out the write and respond as usual.
      original_on_att_write_request(bearer, request)

    async with asyncio.TaskGroup() as task_group:
      with pyee_extensions.EventWatcher() as watcher:
        # Watch from here, so that only the subscription made after the
        # reconnection is observed.
        mouse_subscribed = self._watch_report_subscription(
            watcher, self.ref_mouse_input_report_characteristic
        )

        self._move_hid_service()
        self._indicate_service_changed_on_encryption(
            watcher, task_group, service_changed_indicated
        )

        with (
            # The server looks its handler up by name on every request, so
            # setting it on this instance routes DUT's writes through the
            # wrapper. The class's own handler is used again on exit.
            mock.patch.object(
                gatt_server, "on_att_write_request", record_write_then_handle
            ),
            self.dut.bl4a.register_callback(
                bl4a_api.Module.HID_HOST
            ) as dut_hid_cb,
        ):
          if connect_from_dut:
            # Before REF advertises, so that the request is pending whichever
            # connection attempt reaches REF.
            self.logger.info("[DUT] Connect to REF")
            await asyncio.to_thread(
                self.dut.bt.connect, self.ref.random_address
            )
          self.logger.info("[REF] Restart advertising")
          await self.ref.device.start_advertising(
              own_address_type=hci.OwnAddressType.RANDOM,
          )

          self.logger.info("[REF] Wait for mouse input report subscription")
          await asyncio.wait_for(
              mouse_subscribed.wait(), _DEFAULT_STEP_TIMEOUT_SECONDS
          )

          # The HID host only subscribes at the new handle after rediscovering
          # the HOGP service, and only reports the connection after
          # subscribing, so the last connection is still to come or already
          # reported. Record every change on the way, not only the CONNECTED
          # one.
          self.logger.info("[DUT] Wait for connected")
          state_changes = [
              state_change
              for state_change in dut_hid_cb.get_all_events(
                  bl4a_api.ProfileConnectionStateChanged
              )
              if state_change.address == self.ref.random_address
          ]
          while (
              not state_changes
              or state_changes[-1].state
              != android_constants.ConnectionState.CONNECTED
          ):
            state_changes.append(
                await dut_hid_cb.wait_for_event(
                    bl4a_api.ProfileConnectionStateChanged,
                    lambda e: e.address == self.ref.random_address,
                )
            )

    return _Reconnection(
        state_changes=state_changes,
        wrote_before_service_changed=wrote_before_service_changed,
    )

  @navi_test_base.TwoDevicesTestBase.require_flag(
      _FLAG_HANDLE_SERVICE_CHANGE_DURING_DISCOVERY
  )
  async def test_reconnect_with_db_hash_mismatch(self) -> None:
    """Tests reconnecting to a REF whose Database Hash changed while apart.

    REF keeps its Database Hash characteristic, so DUT reconnects to a database
    whose hash no longer matches the one it cached. DUT notices on reading the
    hash and rediscovers, telling the HID host that the database changed; REF's
    Service Changed indication right after encryption arrives later on.

    DUT's cached database is left as it was after the first connection, so that
    the mismatch is detected. The hash is read alongside encryption, so the HID
    host may or may not have connected from its own cache by the time the
    mismatch is detected: either way, it has to end up connected.

    Test steps:
      1. Pair with REF and establish the HID connection.
      2. Terminate the connection.
      3. Go through _reconnect_to_moved_hogp_service().
    """
    # Leverage the test_connect() to establish the connection.
    await self.test_connect()

    ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
        hci.Address(self.dut.address)
    )
    assert ref_dut_acl is not None
    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      self.logger.info("[REF] Disconnect")
      await ref_dut_acl.disconnect()
      self.logger.info("[DUT] Wait for disconnected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.random_address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
      )

    await self._reconnect_to_moved_hogp_service()

  @navi_test_base.TwoDevicesTestBase.require_flag(
      _FLAG_HANDLE_SERVICE_CHANGE_DURING_DISCOVERY
  )
  async def test_reconnect_with_service_changed_during_open(self) -> None:
    """Tests REF indicating Service Changed while the HID host is opening.

    DUT is bonded with REF and has cached its database, but has no HID record
    for it, e.g. because it was lost. When DUT requests the connection, the HID
    host has to discover the HOGP service, while GATT is ready right away and
    acts on REF's indication. The HID host must carry on opening the
    connection: not knowing the HID device, DUT would not accept the connection
    back if the HID host gave up on it.

    REF has no Database Hash characteristic, so DUT uses its cached database as
    is, and the indication is the only signal that the HOGP service moved.

    Test steps:
      1. Remove the Database Hash characteristic from REF.
      2. Go through _pair_without_completing_hid_open().
      3. Go through _reconnect_to_moved_hogp_service(), with DUT requesting the
         connection.
      4. Check DUT wrote nothing to REF before confirming Service Changed. The
         HID host writes the report CCCDs as the last step of opening, and
         having no HID record, it cannot report the connection before that:
         it was thus still opening when the indication arrived. Otherwise,
         report the result as inconclusive.
      5. Verify DUT never reported the HID device as disconnected.
    """
    self._disable_database_hash()
    self._setup_hid_service()
    await self._pair_without_completing_hid_open()

    reconnection = await self._reconnect_to_moved_hogp_service(
        connect_from_dut=True
    )

    # Not a failure of DUT, but of the test to set up the case under test.
    if reconnection.wrote_before_service_changed:
      raise signals.TestError(
          "Inconclusive: DUT wrote to REF before confirming Service Changed,"
          " so the HID host may have opened the connection before the"
          " indication"
      )
    self.assertNotIn(
        android_constants.ConnectionState.DISCONNECTED,
        [state_change.state for state_change in reconnection.state_changes],
        "HID host gave up on the connection when Service Changed was indicated",
    )

  async def _change_hogp_service_while_connected(self) -> None:
    """Changes the HOGP service on REF while the HID connection is up.

    REF moves its HOGP service while connected, then indicates Service Changed.
    DUT is expected to report a disconnection, let GATT rediscover the database,
    and then resume the connection on its own using the new handles.

    Steps:
      1. Pair with REF and establish the HID connection.
      2. Move the HOGP service on REF, changing its handles and database hash.
      3. Indicate Service Changed from REF.
      4. Verify DUT reports the HID device as disconnected.
      5. Verify DUT re-establishes the HID connection.
      6. Verify DUT subscribes to the mouse input report at its new handle.
    """
    # Leverage the test_connect() to establish the connection.
    await self.test_connect()

    with pyee_extensions.EventWatcher() as watcher:
      mouse_subscribed = self._watch_report_subscription(
          watcher, self.ref_mouse_input_report_characteristic
      )

      ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
          hci.Address(self.dut.address)
      )
      assert ref_dut_acl is not None

      with self.dut.bl4a.register_callback(
          bl4a_api.Module.HID_HOST
      ) as dut_hid_cb:
        self._move_hid_service()
        await self._send_service_changed(ref_dut_acl)

        self.logger.info("[DUT] Wait for disconnected")
        await dut_hid_cb.wait_for_event(
            bl4a_api.ProfileConnectionStateChanged(
                address=self.ref.random_address,
                state=android_constants.ConnectionState.DISCONNECTED,
            ),
        )
        self.logger.info("[DUT] Wait for reconnected")
        await dut_hid_cb.wait_for_event(
            bl4a_api.ProfileConnectionStateChanged(
                address=self.ref.random_address,
                state=android_constants.ConnectionState.CONNECTED,
            ),
        )

      self.logger.info("[REF] Wait for mouse input report subscription")
      await asyncio.wait_for(
          mouse_subscribed.wait(), _DEFAULT_STEP_TIMEOUT_SECONDS
      )

  @navi_test_base.TwoDevicesTestBase.require_flag(
      _FLAG_HANDLE_SERVICE_CHANGE_DURING_DISCOVERY
  )
  async def test_reconnect_after_bt_restart_following_service_changed(
      self,
  ) -> None:
    """Tests reconnecting across a BT restart after a service change.

    Handling the service change must not lose what DUT stored about the HID
    device. When Bluetooth starts again, e.g. on reboot or when switching user,
    the HID host is rebuilt from that storage alone; if it is gone, the device
    is not known to the HID host, and DUT never reconnects to it. Until then,
    the HID host still knows the device from memory, so the loss only shows
    after the restart.

    Test steps:
      1. Go through _change_hogp_service_while_connected().
      2. Turn Bluetooth off and on again on DUT.
      3. Start advertising on REF.
      4. Verify the HID connection is re-established by the background scanner.
    """
    await self._change_hogp_service_while_connected()

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      self.logger.info("[DUT] Turn off BT")
      self.assertTrue(self.dut.bt.disable())
      self.dut.bt.waitForAdapterState(android_constants.AdapterState.OFF)
      self.logger.info("[DUT] Wait for HID disconnected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.random_address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
      )

      self.logger.info("[DUT] Turn on BT")
      self.assertTrue(self.dut.bt.enable())
      self.dut.bt.waitForAdapterState(android_constants.AdapterState.ON)

    with self.dut.bl4a.register_callback(
        bl4a_api.Module.HID_HOST
    ) as dut_hid_cb:
      # No connect request from DUT: reconnecting relies solely on what the HID
      # host restored from storage.
      self.logger.info("[REF] Restart advertising")
      await self.ref.device.start_advertising(
          own_address_type=hci.OwnAddressType.RANDOM,
      )
      self.logger.info("[DUT] Wait for connected")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.random_address,
              state=android_constants.ConnectionState.CONNECTED,
          ),
      )

  async def test_keyboard_input(self) -> None:
    """Tests the HID keyboard input.

    Test steps:
      1. Establish the HID connection between DUT and REF.
      2. Press each key on the keyboard and verify the key down and up events
         on DUT.
    """
    # Leverage the test_connect() to establish the connection.
    await self.test_connect()
    report_characteristic = self.ref_keyboard_input_report_characteristic

    input_monitor = await input_utils.InputMonitor.create(
        self.dut.device.serial
    )
    self.test_case_context.push(input_monitor)

    self.logger.info("[DUT] Wait for input ready")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["Bumble Keyboard"])

    for hid_key in range(
        constants.UsbHidKeyCode.A, constants.UsbHidKeyCode.Z + 1
    ):
      hid_key_code = constants.UsbHidKeyCode(hid_key)
      android_key_code = android_constants.KeyCode[hid_key_code.name]
      self.logger.info("[REF] Press HID key %s", hid_key_code.name)
      report_characteristic.value = bytes(
          [0x00, 0x00, hid_key, 0x00, 0x00, 0x00, 0x00, 0x00]
      )
      await self.ref.device.notify_subscribers(report_characteristic)
      self.logger.info("[DUT] Wait for key %s down", android_key_code.name)
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await input_monitor.wait_for_event(
            ["EV_KEY", f"KEY_{hid_key_code.name}", "DOWN"]
        )

      self.logger.info("[REF] Release HID key %s", hid_key_code.name)
      report_characteristic.value = bytes(8)

      self.logger.info("[DUT] Wait for key %s up", android_key_code.name)
      await self.ref.device.notify_subscribers(report_characteristic)
      async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
        await input_monitor.wait_for_event(
            ["EV_KEY", f"KEY_{hid_key_code.name}", "UP"]
        )

  async def test_mouse_click(self) -> None:
    """Tests the HID mouse click.

    Test steps:
      1. Leverage the test_connect() to establish the connection.
      2. Press primary button and wait for button press.
      3. Release primary button and wait for button down.
    """
    # Leverage the test_connect() to establish the connection.
    await self.test_connect()
    report_characteristic = self.ref_mouse_input_report_characteristic

    input_monitor = await input_utils.InputMonitor.create(
        self.dut.device.serial
    )
    self.test_case_context.push(input_monitor)

    self.logger.info("[DUT] Wait for input ready")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["Bumble Mouse"])

    self.logger.info("[REF] Press Primary button")
    report_characteristic.value = struct.pack("<BhhB", 0x01, 0, 0, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for button press")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_KEY", "BTN_MOUSE", "DOWN"])

    self.logger.info("[REF] Release Primary button")
    report_characteristic.value = struct.pack("<BhhB", 0x00, 0, 0, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for button up")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_KEY", "BTN_MOUSE", "UP"])

  async def test_mouse_movement(self) -> None:
    """Tests the HID mouse movement.

    Test steps:
      1. Leverage the test_connect() to establish the connection.
      2. Move on X axis and wait for hover movement.
      3. Move on Y axis and wait for hover movement.
    """
    # Leverage the test_connect() to establish the connection.
    await self.test_connect()
    report_characteristic = self.ref_mouse_input_report_characteristic

    input_monitor = await input_utils.InputMonitor.create(
        self.dut.device.serial
    )
    self.test_case_context.push(input_monitor)

    self.logger.info("[DUT] Wait for input ready")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["Bumble Mouse"])

    self.logger.info("[REF] Move on X axis")
    report_characteristic.value = struct.pack("<BhhB", 0, 1, 0, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for hover movement")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_REL", " REL_X"])

    self.logger.info("[REF] Move on Y axis")
    report_characteristic.value = struct.pack("<BhhB", 0x00, 0, 1, 0)
    await self.ref.device.notify_subscribers(report_characteristic)

    self.logger.info("[DUT] Wait for hover movement")
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      await input_monitor.wait_for_event(["EV_REL", " REL_Y"])

  async def test_set_preferred_transport(self) -> None:
    """Tests setting the preferred transport for HID Host.

    Test steps:
      1. Establish the HID connection over Classic.
      2. Set the preferred transport to LE.
      3. Verify the transport switch to LE.
    """
    self._setup_hid_service()

    ref_classic_hid_device = hid.Device(self.ref.device, delegate=None)
    self.ref.device.sdp_service_records = {
        1: hid.make_device_sdp_record(1, hid.DEFAULT_REPORT_MAP)
    }
    condition = asyncio.Condition()
    mouse_characteristic_subscribers: list[att.Bearer | None] = [None]

    @ref_classic_hid_device.on(hid.Device.EVENT_CONNECTION)
    @ref_classic_hid_device.on(hid.Device.EVENT_DISCONNECTION)
    async def on_ref_hid_connection_state_changed() -> None:
      async with condition:
        condition.notify_all()

    @self.ref_mouse_input_report_characteristic.on(
        gatt.Characteristic.EVENT_SUBSCRIPTION
    )
    async def on_ref_hid_control_channel_state_changed(
        bearer: att.Bearer, notify_enabled: bool, indicate_enabled: bool
    ) -> None:
      del notify_enabled, indicate_enabled
      async with condition:
        mouse_characteristic_subscribers[0] = bearer
        condition.notify_all()

    with (
        self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST) as dut_hid_cb,
        self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as adapter_cb,
    ):
      self.logger.info("[DUT] Pair and connect LE")
      key_distribution = (
          pairing.PairingDelegate.KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY
          | pairing.PairingDelegate.KeyDistribution.DISTRIBUTE_IDENTITY_KEY
          | pairing.PairingDelegate.KeyDistribution.DISTRIBUTE_LINK_KEY
      )
      await self.le_connect_and_pair(
          hci.OwnAddressType.PUBLIC,
          connect_profiles=False,
          delegate=pairing.PairingDelegate(
              io_capability=pairing.PairingDelegate.IoCapability.DISPLAY_OUTPUT_AND_KEYBOARD_INPUT,
              local_initiator_key_distribution=key_distribution,
              local_responder_key_distribution=key_distribution,
          ),
      )
      self.logger.info("[DUT] Wait for HID service discovered")
      discovered_uuids = set[core.UUID]()
      while not discovered_uuids.issuperset({
          core.BT_HUMAN_INTERFACE_DEVICE_SERVICE,
          gatt.GATT_HUMAN_INTERFACE_DEVICE_SERVICE,
      }):
        uuid_changed_event = await adapter_cb.wait_for_event(
            bl4a_api.UuidChanged(address=self.ref.address, uuids=mock.ANY)
        )
        discovered_uuids.update(map(core.UUID, uuid_changed_event.uuids or []))
        self.logger.info("[DUT] uuids: %r", discovered_uuids)

      self.logger.info("[DUT] Connect to REF")
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
      self.logger.info(
          "[DUT] Initial preferred transport: %s", initial_transport
      )
      self.assertEqual(
          initial_transport,
          android_constants.Transport.CLASSIC,
          msg="Initial preferred transport should be CLASSIC",
      )
      async with (
          self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS),
          condition,
      ):
        self.logger.info("[REF] Wait for HID connected over Classic")
        await condition.wait_for(
            lambda: ref_classic_hid_device.control_channel is not None
        )

      self.logger.info("[DUT] Set preferred transport to LE")
      self.assertTrue(
          self.dut.bt.setHidHostPreferredTransport(
              self.ref.address, android_constants.Transport.LE
          ),
          msg="Failed to set preferred transport to LE",
      )

      updated_transport = self.dut.bt.getHidHostPreferredTransport(
          self.ref.address
      )
      self.logger.info(
          "[DUT] Updated preferred transport: %s", updated_transport
      )
      self.assertEqual(
          updated_transport,
          android_constants.Transport.LE,
          msg="Updated preferred transport should be LE",
      )

      self.logger.info("[DUT] Wait for transport switch (DISCONNECTED Classic)")
      await dut_hid_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged(
              address=self.ref.address,
              state=android_constants.ConnectionState.DISCONNECTED,
          ),
      )
      async with (
          self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS),
          condition,
      ):
        self.logger.info("[REF] Wait for HID disconnected over Classic")
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
      async with (
          self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS),
          condition,
      ):
        self.logger.info("[REF] Wait for HID connected over LE")
        await condition.wait_for(
            lambda: mouse_characteristic_subscribers[0] is not None
        )

  async def test_get_report(self) -> None:
    """Tests HOGP get report.

    Test steps:
      1. Establish the HID connection.
      2. Get the report with report type INPUT_REPORT and report ID 1
      (Keyboard).
      3. Verify the report is retrieved successfully.
    """
    await self.test_connect()

    report_id = 1

    dut_hid_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    self.test_case_context.push(dut_hid_cb)

    self.logger.info("[DUT] Get HOGP report for Keyboard (ID 1)")
    self.dut.bt.getHidHostReport(
        self.ref.random_address,
        hid.ReportType.INPUT_REPORT,
        report_id,
        0,
    )

    self.logger.info("[DUT] Wait for HidHostReport event")
    event = await dut_hid_cb.wait_for_event(bl4a_api.HidHostReport)

    self.logger.info("[DUT] Verify report data")
    self.assertEqual(event.address, self.ref.random_address)
    self.assertSequenceEqual(event.report, [report_id] + [0] * 8)

  async def test_set_report(self) -> None:
    """Tests HOGP set report.

    Test steps:
      1. Establish the HID connection.
      2. Set the report with report type INPUT_REPORT and report ID 1.
      3. Verify the handshake status is successful.
    """
    await self.test_connect()

    report_id = 1
    data = [0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09]
    report_hex = bytes([report_id] + data).hex()

    dut_hid_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    self.test_case_context.push(dut_hid_cb)

    self.logger.info("[DUT] Set HOGP report")
    self.dut.bt.setHidHostReport(
        self.ref.random_address,
        hid.ReportType.INPUT_REPORT,
        report_hex,
    )

    self.logger.info("[DUT] Wait for handshake")
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostHandshake(
            address=self.ref.random_address,
            status=hid.HandshakeMessage.ResultCode.SUCCESSFUL,
        )
    )

    # Verify on Bumble side that the characteristic value was updated
    async with self.assert_not_timeout(_DEFAULT_STEP_TIMEOUT_SECONDS):
      self.logger.info("[REF] Verify characteristic value")
      condition = asyncio.Condition()

      @self.ref_keyboard_input_report_characteristic.on(
          self.ref_keyboard_input_report_characteristic.EVENT_WRITE
      )
      async def on_write(*args, **kwargs) -> None:
        del args, kwargs
        async with condition:
          condition.notify_all()

      async with condition:
        await condition.wait_for(
            lambda: self.ref_keyboard_input_report_characteristic.value
            == bytes(data)
        )

  async def test_get_protocol_mode(self) -> None:
    """Tests HOGP get protocol mode.

    Test steps:
      1. Establish the HID connection.
      2. Get the protocol mode.
      3. Verify the protocol mode is REPORT_PROTOCOL.
    """
    await self.test_connect()

    dut_hid_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    self.test_case_context.push(dut_hid_cb)

    self.logger.info("[DUT] Get HOGP protocol mode")
    self.dut.bt.getHidHostProtocolMode(self.ref.random_address)

    self.logger.info("[DUT] Wait for protocol mode event")
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostProtocolModeChanged(
            address=self.ref.random_address,
            protocol_mode=android_constants.HidHostProtocolMode.REPORT,
        )
    )

  async def test_set_protocol_mode(self) -> None:
    """Tests HOGP set protocol mode.

    Test steps:
      1. Establish the HID connection.
      2. Set the protocol mode to BOOT_PROTOCOL.
      3. Verify the handshake is successful.
      4. Get the protocol mode and verify it is BOOT_PROTOCOL.
      5. Set it back to REPORT_PROTOCOL.
      6. Verify the handshake is successful.
      7. Get the protocol mode and verify it is REPORT_PROTOCOL.
    """
    await self.test_connect()

    dut_hid_cb = self.dut.bl4a.register_callback(bl4a_api.Module.HID_HOST)
    self.test_case_context.push(dut_hid_cb)

    self.logger.info("[DUT] Set HOGP protocol mode to BOOT_PROTOCOL")
    self.dut.bt.setHidHostProtocolMode(
        self.ref.random_address,
        android_constants.HidHostProtocolMode.BOOT,
    )

    self.logger.info("[DUT] Wait for handshake")
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostHandshake(
            address=self.ref.random_address,
            status=hid.HandshakeMessage.ResultCode.SUCCESSFUL,
        )
    )

    self.logger.info("[DUT] Get protocol mode")
    self.dut.bt.getHidHostProtocolMode(self.ref.random_address)
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostProtocolModeChanged(
            address=self.ref.random_address,
            protocol_mode=android_constants.HidHostProtocolMode.BOOT,
        )
    )

    self.logger.info("[DUT] Set HOGP protocol mode to REPORT_PROTOCOL")
    self.dut.bt.setHidHostProtocolMode(
        self.ref.random_address,
        android_constants.HidHostProtocolMode.REPORT,
    )

    self.logger.info("[DUT] Wait for handshake")
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostHandshake(
            address=self.ref.random_address,
            status=hid.HandshakeMessage.ResultCode.SUCCESSFUL,
        )
    )

    self.logger.info("[DUT] Get protocol mode")
    self.dut.bt.getHidHostProtocolMode(self.ref.random_address)
    await dut_hid_cb.wait_for_event(
        bl4a_api.HidHostProtocolModeChanged(
            address=self.ref.random_address,
            protocol_mode=android_constants.HidHostProtocolMode.REPORT,
        )
    )


if __name__ == "__main__":
  test_runner.main()
