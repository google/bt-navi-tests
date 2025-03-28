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

import pathlib
import re

from setuptools.command.build_py import build_py as _build_py


class build_py(_build_py):
    """Custom build command to process non-Python targets."""

    def run(self) -> None:
        # Generate snippet constants.
        with open(
            pathlib.Path("navi", "utils", "snippet_constants.textproto"), "r"
        ) as f:
            src = f.read()
        matches = re.findall(
            r"constants:?\s*\{\s*name:\s*\"(\w+)\"\s*string_value:\s*\"(\w+)\"\s*\}",
            src,
        )
        with open(pathlib.Path("navi", "utils", "snippet_constants.py"), "w") as f:
            for key, value in matches:
                f.write(f'{key} = "{value}"\n')

        return super().run()
