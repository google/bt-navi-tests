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

"""Generic functionality test suite for Bluetooth."""

import inspect
import pkgutil
import importlib
from mobly import base_test
from mobly import base_suite
from mobly import config_parser
from mobly import suite_runner
from typing_extensions import override

from navi.tests import smoke
from navi.tests import functionality


class GenericTestSuite(base_suite.BaseSuite):
    """Generic test suite for Bluetooth."""

    @override
    def setup_suite(self, config: config_parser.TestRunConfig) -> None:
        del config  # Unused.

        for module in (smoke, functionality):
            for submodule_info in pkgutil.iter_modules(
                module.__path__, prefix=module.__name__ + "."
            ):
                submodule = importlib.import_module(submodule_info.name)
                for _, test_class in inspect.getmembers(
                    submodule,
                    lambda x: inspect.isclass(x)
                    and issubclass(x, base_test.BaseTestClass),
                ):
                    self.add_test_class(test_class)


if __name__ == "__main__":
    suite_runner.run_suite_class()
