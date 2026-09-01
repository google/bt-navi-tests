/*
 * Copyright 2026 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.google.wireless.android.pixel.bluetooth.snippet

import android.telecom.Connection
import android.telecom.DisconnectCause

/**
 * A mock [Connection] that can be used to simulate a call connection in the
 * [MockConnectionService].
 */
class MockConnection : Connection() {
  init {
    connectionCapabilities = CAPABILITY_MUTE or CAPABILITY_SUPPORT_HOLD or CAPABILITY_HOLD
  }

  override fun onAnswer() {
    setActive()
  }

  override fun onDisconnect() {
    setDisconnected(DisconnectCause(DisconnectCause.LOCAL))
    destroy()
  }

  override fun onAbort() {
    setDisconnected(DisconnectCause(DisconnectCause.CANCELED))
    destroy()
  }

  override fun onHold() {
    setOnHold()
  }

  override fun onUnhold() {
    setActive()
  }
}
