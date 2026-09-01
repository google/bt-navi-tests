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
import android.telecom.ConnectionRequest
import android.telecom.ConnectionService
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager

/**
 * A mock [ConnectionService] that can be used to simulate a call connection in the
 * [MockConnectionService].
 */
class MockConnectionService : ConnectionService() {
  var latestConnection: MockConnection? = null

  override fun onCreateOutgoingConnection(
    connectionManagerPhoneAccount: PhoneAccountHandle?,
    request: ConnectionRequest?,
  ): Connection? {
    if (request == null) return null
    val connection = MockConnection()
    connection.setAddress(request.address, TelecomManager.PRESENTATION_ALLOWED)
    connection.setDialing()
    latestConnection = connection
    return connection
  }

  override fun onCreateOutgoingConnectionFailed(
    connectionManagerPhoneAccount: PhoneAccountHandle?,
    request: ConnectionRequest?,
  ) {
    latestConnection = null
  }
}
