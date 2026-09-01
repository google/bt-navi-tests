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

"""Base classes for performance tests."""
import statistics
from navi.tests import navi_test_base


class PerformanceTestBase(navi_test_base.TwoDevicesTestBase):
  """Base class for performance tests."""
  TEST_TIMEOUT_SECONDS = 600.0

  def success_attempt_record(self, test_round: int, latency: float,
                             latency_list: list[float]) -> None:
    """Records a single test attempt.

    Args:
      test_round: The round of the test.
      latency: The latency of the test.
      latency_list: The list of latencies of all test attempts.
    """
    self.logger.info("Success in %.2f seconds", latency)
    self.logger.info("Test %d Success", test_round)
    latency_list.append(latency)

  def record_sponge_data(self,
                         repeat_times: int, latency_list: list[float]) -> None:
    """Records the sponge data."""
    success_count = len(latency_list)
    self.logger.info(
        "[success rate] Passes: %d / Attempts: %d",
        success_count,
        repeat_times,
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
                "attempts": repeat_times,
                "avg_latency": statistics.mean(latency_list),
                "min_latency": min(latency_list),
                "max_latency": max(latency_list),
                "stdev_latency": statistics.stdev(latency_list),
            },
        )
    )
