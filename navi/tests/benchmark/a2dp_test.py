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

import statistics

from bumble import a2dp
from bumble import avdtp
from bumble import core
from bumble import hci
from mobly import test_runner
from typing_extensions import override

from navi.bumble_ext import a2dp as a2dp_ext
from navi.tests import navi_test_base
from navi.tests.benchmark import performance_tool
from navi.utils import android_constants
from navi.utils import bl4a_api


_A2DP_SERVICE_RECORD_HANDLE = 1
_DEFAULT_REPEAT_TIMES = 30
_PROPERTY_CODEC_PRIORITY = "bluetooth.a2dp.source.%s_priority.config"
_VALUE_CODEC_DISABLED = -1
_DEFAULT_TIMEOUT_SECONDS = 10.0

_Callback = bl4a_api.CallbackHandler
_A2dpCodec = a2dp_ext.A2dpCodec


class A2dpTest(navi_test_base.TwoDevicesTestBase):
  ref_sinks: dict[_A2dpCodec, avdtp.LocalSink]
  dut_supported_codecs: list[_A2dpCodec]

  @override
  async def async_setup_class(self) -> None:
    await super().async_setup_class()
    self.dut_supported_codecs = [
        codec
        for codec in _A2dpCodec
        if int(
            self.dut.getprop(_PROPERTY_CODEC_PRIORITY % codec.name.lower())
            or "0"
        )
        > _VALUE_CODEC_DISABLED
    ]
    self.ref_sinks = {}

  @override
  async def async_setup_test(self) -> None:
    await super().async_setup_test()
    self.ref_sinks.clear()

  def _setup_a2dp_device(self, codecs: list[_A2dpCodec]) -> None:
    self.ref.device.sdp_service_records = {
        _A2DP_SERVICE_RECORD_HANDLE: a2dp.make_audio_sink_service_sdp_records(
            _A2DP_SERVICE_RECORD_HANDLE
        ),
    }

    def on_avdtp_connection(server: avdtp.Protocol) -> None:
      for codec in codecs:
        self.ref_sinks[codec] = server.add_sink(
            codec.get_default_capabilities()
        )

    avdtp_listener = avdtp.Listener.for_device(self.ref.device)
    avdtp_listener.on("connection", on_avdtp_connection)

  async def pair_and_connect(self) -> None:
    """Tests A2DP connection establishment right after a pairing session."""
    with self.dut.bl4a.register_callback(bl4a_api.Module.A2DP) as dut_cb:
      self._setup_a2dp_device([_A2dpCodec.SBC])
      await self.classic_connect_and_pair()
      await dut_cb.wait_for_event(
          bl4a_api.ProfileConnectionStateChanged,
          lambda e: (e.state == android_constants.ConnectionState.CONNECTED),
      )

  async def _terminate_connection_from_ref(self) -> None:
    self.logger.info("[DUT] Terminate connection.")
    with self.dut.bl4a.register_callback(bl4a_api.Module.ADAPTER) as dut_cb:
      ref_acl = self.ref.device.find_connection_by_bd_addr(
          hci.Address(self.dut.address), transport=hci.BT_BR_EDR_TRANSPORT
      )
      if ref_acl is None:
        self.logger.info("[REF] No ACL connection found.")
        return
      await ref_acl.disconnect()
      await dut_cb.wait_for_event(
          bl4a_api.AclDisconnected,
          lambda e: (e.address == self.ref.address),
      )

  async def test_a2dp_connection_outgoing(self) -> None:
    """Test make outgoing A2DP connections."""
    success_count = 0
    latency_list = list[float]()
    await self.pair_and_connect()
    await self._terminate_connection_from_ref()
    for i in range(_DEFAULT_REPEAT_TIMES):
      try:
        with self.dut.bl4a.register_callback(bl4a_api.Module.A2DP) as dut_cb:
          self.logger.info("[DUT] Reconnect.")
          with performance_tool.Stopwatch() as stop_watch:
            self.dut.bt.connect(self.ref.address)
            self.logger.info("[DUT] Wait for A2DP connected.")
            await dut_cb.wait_for_event(
                bl4a_api.ProfileActiveDeviceChanged,
                lambda e: (e.address == self.ref.address),
                timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
          latency_seconds = stop_watch.elapsed_time.total_seconds()
          self.logger.info(
              "Success connection in %.2f seconds", latency_seconds
          )
          self.logger.info("Test%d Success", i + 1)
          latency_list.append(latency_seconds)
        success_count += 1
      except (core.BaseBumbleError, AssertionError):
        self.logger.exception("Failed to make A2DP connection")
      finally:
        await self._terminate_connection_from_ref()
    self.logger.info(
        "[success rate] Passes: %d / Attempts: %d",
        success_count,
        _DEFAULT_REPEAT_TIMES,
    )
    self.logger.info(
        "[connection time] avg: %.2f, min: %.2f, max: %.2f, stdev: %.2f",
        statistics.mean(latency_list),
        min(latency_list),
        max(latency_list),
        statistics.stdev(latency_list),
    )
    self.record_data(
        navi_test_base.RecordData(
            test_name=self.current_test_info.name,
            properties={
                "passes": success_count,
                "attempts": _DEFAULT_REPEAT_TIMES,
                "avg_latency": statistics.mean(latency_list),
                "min_latency": min(latency_list),
                "max_latency": max(latency_list),
                "stdev_latency": statistics.stdev(latency_list),
            },
        )
    )


if __name__ == "__main__":
  test_runner.main()
