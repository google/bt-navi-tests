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

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc

/** Other snippet for testing. */
class OtherSnippet : Snippet {
  private val instrumentation = InstrumentationRegistry.getInstrumentation()
  private val context = instrumentation.targetContext
  val broadcastReceivers = mutableMapOf<String, BroadcastReceiver>()

  /** Ping. */
  @Rpc(description = "Ping") fun ping(): String = "pong"

  @Rpc(description = "Get hardware name") fun getHardware(): String = Build.HARDWARE

  @Rpc(description = "Get SDK version") fun getSdkVersion(): Int = Build.VERSION.SDK_INT

  @Rpc(description = "Get full SDK version")
  fun getFullSdkVersion(): Int = Build.VERSION.SDK_INT_FULL

  @AsyncRpc(description = "Register Voice Command Callback")
  fun registerVoiceCommandCallback(callbackId: String) {
    val broadcastReceiver =
      object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
          Log.i(TAG, "onReceive $intent")
          when (intent.action) {
            VoiceCommandActivity.ACTION_COMMAND ->
              Utils.postSnippetEvent(callbackId, SnippetConstants.VOICE_COMMAND) {
                putBoolean(SnippetConstants.FIELD_STATE, true)
              }
            Intent.ACTION_STOP_VOICE_COMMAND ->
              Utils.postSnippetEvent(callbackId, SnippetConstants.VOICE_COMMAND) {
                putBoolean(SnippetConstants.FIELD_STATE, false)
              }
          }
        }
      }
    context.registerReceiver(
      broadcastReceiver,
      IntentFilter().apply {
        addAction(VoiceCommandActivity.ACTION_COMMAND)
        addAction(Intent.ACTION_STOP_VOICE_COMMAND)
      },
      Context.RECEIVER_EXPORTED,
    )
    broadcastReceivers[callbackId] = broadcastReceiver
  }

  @Rpc(description = "Unregister Voice Command Callback")
  fun unregisterVoiceCommandCallback(callbackId: String) {
    broadcastReceivers.remove(callbackId)?.let { context.unregisterReceiver(it) }
  }

  private companion object {
    const val TAG = "OtherSnippet"
  }
}
