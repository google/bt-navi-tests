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
import struct

from bumble import core
from bumble import device
from bumble import gatt
from bumble import hci
from mobly import test_runner
from mobly import signals

from navi.bumble_ext import rap
from navi.tests import navi_test_base
from navi.utils import android_constants
from navi.utils import bl4a_api

_DEFAULT_TIMEOUT_SECEONDS = 10.0
_DEFAULT_ADVERTISING_INTERVAL = 100


class _RangingService(gatt.TemplateService):
  """Ranging Service."""

  UUID = rap.GATT_RANGING_SERVICE

  def __init__(self, ras_features: rap.RasFeatures) -> None:
    self.control_point_operations = asyncio.Queue[
        rap.RasControlPointOperation
    ]()
    self.real_time_ranging_data_subscrptions = dict[
        device.Connection, tuple[bool, bool]
    ]()
    self.ras_features_characteristic = gatt.Characteristic(
        rap.GATT_RAS_FEATURES_CHARACTERISTIC,
        properties=gatt.Characteristic.Properties.READ,
        permissions=gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION,
        value=struct.pack('<I', ras_features),
    )
    self.real_time_ranging_data_characteristic = gatt.Characteristic(
        rap.GATT_REAL_TIME_RANGING_DATA_CHARACTERISTIC,
        properties=(
            gatt.Characteristic.Properties.INDICATE
            | gatt.Characteristic.Properties.NOTIFY
        ),
        permissions=gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION,
    )

    self.on_demand_ranging_data_characteristic = gatt.Characteristic(
        rap.GATT_ON_DEMAND_RANGING_DATA_CHARACTERISTIC,
        properties=(
            gatt.Characteristic.Properties.INDICATE
            | gatt.Characteristic.Properties.NOTIFY
        ),
        permissions=gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION,
    )

    self.ras_control_point_characteristic = gatt.Characteristic(
        rap.GATT_RAS_CONTROL_POINT_CHARACTERISTIC,
        properties=(
            gatt.Characteristic.Properties.INDICATE
            | gatt.Characteristic.Properties.WRITE
            | gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE
        ),
        permissions=gatt.Characteristic.Permissions.WRITE_REQUIRES_ENCRYPTION,
        value=gatt.CharacteristicValue(
            write=lambda _connection, data: self.control_point_operations.put_nowait(
                rap.RasControlPointOperation.from_bytes(data)
            )
        ),
    )

    self.ranging_data_ready_characteristic = gatt.Characteristic(
        rap.GATT_RANGING_DATA_READY_CHARACTERISTIC,
        properties=(
            gatt.Characteristic.Properties.INDICATE
            | gatt.Characteristic.Properties.NOTIFY
            | gatt.Characteristic.Properties.READ
        ),
        permissions=gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION,
        value=struct.pack('<H', 0),
    )

    self.ranging_data_overwritten_characteristic = gatt.Characteristic(
        rap.GATT_RANGING_DATA_OVERWRITTEN_CHARACTERISTIC,
        properties=(
            gatt.Characteristic.Properties.INDICATE
            | gatt.Characteristic.Properties.NOTIFY
            | gatt.Characteristic.Properties.READ
        ),
        permissions=gatt.Characteristic.Permissions.READ_REQUIRES_ENCRYPTION,
        value=struct.pack('<H', 0),
    )

    self.real_time_ranging_data_characteristic.on(
        'subscription', self.on_real_time_ranging_data_subscription
    )

    super().__init__([
        self.ras_features_characteristic,
        self.real_time_ranging_data_characteristic,
        self.on_demand_ranging_data_characteristic,
        self.ras_control_point_characteristic,
        self.ranging_data_ready_characteristic,
        self.ranging_data_overwritten_characteristic,
    ])

  def on_real_time_ranging_data_subscription(
      self,
      connection: device.Connection,
      notify_enabled: bool,
      indicate_enabled: bool,
  ) -> None:
    self.real_time_ranging_data_subscrptions[connection] = (
        notify_enabled,
        indicate_enabled,
    )

  async def send_real_time_ranging_data(
      self,
      connection: device.Connection,
      data: bytes,
  ) -> None:
    mps = connection.att_mtu - 6
    characteristic = self.real_time_ranging_data_characteristic
    notify_enabled, indicate_enabled = (
        self.real_time_ranging_data_subscrptions.get(connection, (False, False))
    )
    if notify_enabled:
      method = connection.device.notify_subscriber
    elif indicate_enabled:
      method = connection.device.indicate_subscriber
    else:
      raise RuntimeError('No subscription method found.')

    for index, offset in enumerate(range(0, len(data), mps)):
      fragment = data[offset : offset + mps]
      header = rap.SegmentationHeader(
          is_first=(offset == 0),
          is_last=(offset + len(fragment) >= len(data)),
          segment_index=index,
      )
      await method(
          connection=connection,
          attribute=characteristic,
          value=bytes(header) + fragment,
          force=True,
      )


class DistanceMeasurementTest(navi_test_base.TwoDevicesTestBase):
  dut_supported_methods = list[android_constants.DistanceMeasurementMethodId]()
  active_procedure_counter = dict[int, int]()
  ranging_data_table = dict[int, rap.RangingData]()
  completed_ranging_data = asyncio.Queue[rap.RangingData]()

  def _on_subevent_result(
      self, event: hci.HCI_LE_CS_Subevent_Result_Event
  ) -> None:
    if not (
        connection := self.ref.device.lookup_connection(event.connection_handle)
    ):
      return
    procedure_counter = event.procedure_counter
    if not (ranging_data := self.ranging_data_table.get(procedure_counter)):
      ranging_data = self.ranging_data_table[procedure_counter] = (
          rap.RangingData(
              ranging_header=rap.RangingHeader(
                  event.config_id,
                  selected_tx_power=connection.cs_procedures[
                      event.config_id
                  ].selected_tx_power,
                  antenna_paths_mask=(1 << (event.num_antenna_paths + 1)) - 1,
                  ranging_counter=procedure_counter,
              )
          )
      )

    subevent = rap.Subevent(
        start_acl_connection_event=event.start_acl_conn_event_counter,
        frequency_compensation=event.frequency_compensation,
        ranging_abort_reason=event.procedure_done_status,
        ranging_done_status=event.procedure_done_status,
        subevent_done_status=event.subevent_done_status,
        subevent_abort_reason=event.abort_reason,
        reference_power_level=event.reference_power_level,
    )
    ranging_data.subevents.append(subevent)
    self.active_procedure_counter[event.config_id] = procedure_counter
    self._post_subevent_result(event)

  def _post_subevent_result(
      self,
      event: (
          hci.HCI_LE_CS_Subevent_Result_Event
          | hci.HCI_LE_CS_Subevent_Result_Continue_Event
      ),
  ) -> None:
    procedure_counter = self.active_procedure_counter[event.config_id]
    ranging_data = self.ranging_data_table[procedure_counter]
    subevent = ranging_data.subevents[-1]
    subevent.ranging_done_status = event.procedure_done_status
    subevent.subevent_done_status = event.subevent_done_status
    subevent.steps.extend([
        rap.Step(mode, data)
        for mode, data in zip(event.step_mode, event.step_data)
    ])

    if event.procedure_done_status == hci.CsDoneStatus.ALL_RESULTS_COMPLETED:
      self.completed_ranging_data.put_nowait(ranging_data)

  async def async_setup_class(self) -> None:
    await super().async_setup_class()

    if int(self.dut.getprop('ro.build.version.sdk')) <= 35:
      # Unable to receive the supported methods from the API, use the hardcoded
      # list instead.
      self.dut_supported_methods = [
          android_constants.DistanceMeasurementMethodId.AUTO,
          android_constants.DistanceMeasurementMethodId.RSSI,
      ]
    else:
      self.dut_supported_methods = [
          android_constants.DistanceMeasurementMethodId(method_id)
          for method_id in self.dut.bt.getSupportedDistanceMeasurementMethods()
      ]
    self.logger.info('DUT supported methods: %s', self.dut_supported_methods)
    if not self.dut_supported_methods:
      raise signals.TestAbortClass(
          'DUT does not support any distance measurement method.'
      )
    self.ref.config.channel_sounding_enabled = True

  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    self.active_procedure_counter = dict[int, int]()
    self.ranging_data_table = dict[int, rap.RangingData]()
    self.completed_ranging_data = asyncio.Queue[rap.RangingData]()

  async def test_rssi_ranging(self) -> None:
    """Test RSSI ranging."""
    if (
        android_constants.DistanceMeasurementMethodId.RSSI
        not in self.dut_supported_methods
    ):
      self.skipTest('RSSI ranging is not supported, skip the test.')

    async with self.assert_not_timeout(_DEFAULT_TIMEOUT_SECEONDS):
      self.logger.info('[REF] Start advertising')
      await self.ref.device.start_advertising(
          own_address_type=hci.OwnAddressType.RANDOM,
          advertising_interval_min=_DEFAULT_ADVERTISING_INTERVAL,
          advertising_interval_max=_DEFAULT_ADVERTISING_INTERVAL,
      )

    # Devices must be connected before starting distance measurement.
    self.logger.info('[DUT] Connect to REF')
    await self.dut.bl4a.connect_gatt_client(
        address=self.ref.random_address,
        transport=android_constants.Transport.LE,
        address_type=android_constants.AddressTypeStatus.RANDOM,
    )
    self.logger.info('[DUT] Start distance measurement')
    distance_measurement = self.dut.bl4a.start_distance_measurement(
        bl4a_api.DistanceMeasurementParameters(
            device=self.ref.random_address,
            method_id=android_constants.DistanceMeasurementMethodId.RSSI,
        ),
    )
    self.logger.info('[DUT] Wait for distance measurement result')
    result = await distance_measurement.wait_for_event(
        bl4a_api.DistanceMeasurementResult
    )
    self.logger.info('Distance: %.2fm', result.result_meters)

  async def test_cs_ranging_outgoing(self) -> None:
    """Test outgoing Channel Sounding ranging.

    Test steps:
      1. Setup Ranging Service on the REF device.
      2. Setup connection and pairing.
      3. Start advertising on the REF device.
      4. Connect to the REF device.
      5. Set default CS settings on the REF device.
      6. Start distance measurement on the DUT device.
      7. Wait for real-time ranging data subscription on the REF device.
      8. Wait for ranging data on the REF device.
      9. Send ranging data from the REF device.
      10. Wait for distance measurement ready on the DUT device.
      11. Wait for distance measurement result on the DUT device.
    """
    if (
        android_constants.DistanceMeasurementMethodId.CHANNEL_SOUNDING
        not in self.dut_supported_methods
    ):
      self.skipTest('Channel Sounding is not supported, skip the test.')

    ras = _RangingService(ras_features=rap.RasFeatures.REAL_TIME_RANGING_DATA)
    self.ref.device.gatt_server.add_service(ras)

    # Setup connection and pairing.
    await self.le_connect_and_pair(ref_address_type=hci.OwnAddressType.RANDOM)

    await self.ref.device.start_advertising(
        own_address_type=hci.OwnAddressType.RANDOM,
        advertising_interval_min=_DEFAULT_ADVERTISING_INTERVAL,
        advertising_interval_max=_DEFAULT_ADVERTISING_INTERVAL,
    )

    # Devices must be connected before starting distance measurement.
    self.logger.info('[DUT] Connect to REF')
    await self.dut.bl4a.connect_gatt_client(
        address=self.ref.random_address,
        transport=android_constants.Transport.LE,
        address_type=android_constants.AddressTypeStatus.RANDOM,
    )

    ref_dut_acl = self.ref.device.find_connection_by_bd_addr(
        hci.Address(self.dut.address),
        transport=core.BT_LE_TRANSPORT,
    )
    if not ref_dut_acl:
      self.fail('Failed to find ACL connection between DUT and REF.')
    self.logger.info('[REF] Set default CS settings')
    await self.ref.device.set_default_cs_settings(connection=ref_dut_acl)

    subscriptions = asyncio.Queue[None]()
    ras.real_time_ranging_data_characteristic.on(
        'subscription', lambda *_: subscriptions.put_nowait(None)
    )
    self.ref.device.host.on('cs_subevent_result', self._on_subevent_result)
    self.ref.device.host.on(
        'cs_subevent_result_continue', self._post_subevent_result
    )

    self.logger.info('[DUT] Start distance measurement')
    distance_measurement_task = asyncio.create_task(
        asyncio.to_thread(
            lambda: self.dut.bl4a.start_distance_measurement(
                bl4a_api.DistanceMeasurementParameters(
                    device=self.ref.random_address,
                    method_id=android_constants.DistanceMeasurementMethodId.CHANNEL_SOUNDING,
                ),
            )
        )
    )
    async with self.assert_not_timeout(
        _DEFAULT_TIMEOUT_SECEONDS,
        msg='[DUT] Wait for real-time ranging data subscription',
    ):
      await subscriptions.get()

    async with self.assert_not_timeout(
        _DEFAULT_TIMEOUT_SECEONDS,
        msg='[REF] Wait for ranging data',
    ):
      ranging_data = await self.completed_ranging_data.get()

    async with self.assert_not_timeout(
        _DEFAULT_TIMEOUT_SECEONDS,
        msg='[REF] Send ranging data',
    ):
      await ras.send_real_time_ranging_data(
          connection=next(iter(self.ref.device.connections.values())),
          data=bytes(ranging_data),
      )

    self.logger.info('[DUT] Wait for distance measurement ready')
    distance_measurement = await distance_measurement_task

    self.logger.info('[DUT] Wait for distance measurement result')
    result = await distance_measurement.wait_for_event(
        bl4a_api.DistanceMeasurementResult
    )
    self.logger.info('Distance: %.2fm', result.result_meters)


if __name__ == '__main__':
  test_runner.main()
