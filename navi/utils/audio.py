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

"""Utilities for audio processing."""

from collections.abc import Iterable, Iterator, Sequence
import io
import itertools
from typing import TypeVar
import numpy as np
import pydub
import pydub.generators

_T = TypeVar('_T')


def get_dominant_frequency(
    buffer: bytes | bytearray | memoryview,
    codec: str | None = None,
    format: str | None = None,  # pylint: disable=redefined-builtin
    sample_width: int | None = None,
    frame_rate: int | None = None,
    channels: int | None = None,
) -> float:
  """Gets the dominant frequency of the audio buffer.

  Args:
    buffer: The audio buffer.
    codec: The audio codec.
    format: The audio format.
    sample_width: [Optional for pcm/raw] The sample width.
    frame_rate: [Optional for pcm/raw] The frame rate.
    channels: [Optional for pcm/raw] The number of channels.

  Returns:
    The dominant frequency of the audio buffer.
  """
  segment = pydub.AudioSegment.from_file(
      io.BytesIO(buffer),
      format=format.lower() if format else None,
      codec=codec.lower() if codec else None,
      sample_width=sample_width,
      frame_rate=frame_rate,
      channels=channels,
  ).set_channels(1)
  samples = segment.get_array_of_samples()
  sp = np.fft.rfft(samples)
  freqs = np.fft.rfftfreq(len(samples), 1 / segment.frame_rate)
  return float(freqs[np.argmax(np.abs(sp))])


def generate_sine_tone(
    frequency: float,
    duration: float = 1.0,
    sample_rate: int = 48000,
    data_type: np.typing.DTypeLike = np.int16,
) -> bytes:
  """Generates a sine tone PCM buffer.

  Args:
    frequency: The frequency of the sine tone.
    duration: The duration of the sine tone in seconds.
    sample_rate: The sample rate of the sine tone.
    data_type: The data type of the sine tone.

  Returns:
    The sine tone PCM buffer.
  """
  t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
  return (
      (np.sin(2 * np.pi * frequency * t) * np.iinfo(data_type).max)
      .astype(data_type)
      .tobytes()
  )


def batched(iterable: Iterable[_T], n: int) -> Iterator[Sequence[_T]]:
  """Batches an iterator into batches of the given size.

  Similar to Python 3.12 `itertools.batched`, but it's not available in google3
  yet.

  Args:
    iterable: The iterable to batch.
    n: The size of each batch.

  Yields:
    Batches of the given size.
  """
  iterator = iter(iterable)
  while batch := tuple(itertools.islice(iterator, n)):
    yield batch
