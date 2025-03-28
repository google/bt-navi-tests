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

from importlib import resources
from typing import Literal, overload


@overload
def GetResource(name: str, mode: Literal["r"]) -> str: ...
@overload
def GetResource(name: str, mode: Literal["rb"]) -> bytes: ...
def GetResource(name: str, mode: Literal["r", "rb"]) -> str | bytes:
    """Get the resource file content."""
    package, filename = name.rsplit("/", 1)
    package = package.replace("/", ".")
    with resources.files(package).joinpath(filename).open(mode) as f:
        return f.read()


def GetResourceFilename(name: str) -> str:
    """Get the resource file path."""
    package, filename = name.rsplit("/", 1)
    package = package.replace("/", ".")
    with resources.path(package, filename) as p:
        return str(p)
